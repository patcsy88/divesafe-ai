"""Deterministic risk rules engine and the reconciliation that keeps it authoritative.

Safety invariants (protected by tests/safety):

1. Severity order is GO < CAUTION < INSUFFICIENT EVIDENCE < NO-GO.
2. The deterministic result is a floor. Agent or LLM proposals can only keep it or make it
   more conservative, never less.
3. The engine fails closed: it returns GO only when at least one rule explicitly returned GO
   and nothing more severe applies. No rules, no applicable rules, a rule that raises, and
   missing, stale or future-dated required evidence all give INSUFFICIENT EVIDENCE.

This module ships no numeric safety thresholds. Concrete rules are supplied by the caller,
must carry a source citation, and must come from reviewed sources (docs/risk-model.md).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from divesafe.domain import (
    DataCategory,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskAssessment,
    RiskFactorKind,
    RuleResult,
    most_severe,
    severity,
    unevaluated_factors,
)
from divesafe.domain.risk_types import THRESHOLD_FACTORS
from divesafe.risk.interfaces import Rule

__all__ = [
    "EvidencePolicy",
    "Reconciliation",
    "RiskAssessment",
    "RiskRulesEngine",
    "Rule",
    "reconcile",
    "severity",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidencePolicy:
    required_categories: frozenset[DataCategory]
    max_age: timedelta

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("max_age must be positive")


@dataclass(frozen=True)
class Reconciliation:
    final: Recommendation
    deterministic: Recommendation
    proposed: Recommendation | None
    llm_attempted_downgrade: bool


class RiskRulesEngine:
    def __init__(
        self,
        rules: Sequence[Rule],
        policy: EvidencePolicy,
        ruleset_version: str,
        required_factors: frozenset[RiskFactorKind] = THRESHOLD_FACTORS,
    ) -> None:
        if not ruleset_version.strip():
            raise ValueError("ruleset_version must be set")
        for rule in rules:
            if not rule.citation.strip():
                raise ValueError(f"rule '{rule.rule_id}' has no source citation")
        self._rules = tuple(rules)
        self._required_factors = required_factors
        self._policy = policy
        self._ruleset_version = ruleset_version

    @property
    def policy(self) -> EvidencePolicy:
        return self._policy

    def assess(
        self, plan: DivePlan, evidence: Sequence[EvidenceItem], now: datetime
    ) -> RiskAssessment:
        results = [*self._sufficiency_results(evidence, now)]
        for rule in self._rules:
            results.extend(self._run_rule(rule, plan, evidence))

        if not results:
            results.append(
                RuleResult(
                    rule_id="ruleset.no_applicable_result",
                    outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                    rationale="No rule produced a result, so safety cannot be assessed.",
                )
            )

        uncovered = unevaluated_factors(tuple(results), self._required_factors)
        if uncovered and most_severe(tuple(r.outcome for r in results)) in (
            Recommendation.GO,
            Recommendation.CAUTION,
        ):
            results.append(
                RuleResult(
                    rule_id="ruleset.threshold_factors_uncovered",
                    outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                    rationale=(
                        "GO and CAUTION need a VALIDATED rule for every required factor; "
                        "not evaluated: " + ", ".join(f.value for f in uncovered)
                    ),
                )
            )

        recommendation = most_severe(tuple(r.outcome for r in results))
        return RiskAssessment(
            recommendation=recommendation,
            rule_results=tuple(results),
            unevaluated_factors=uncovered,
            ruleset_version=self._ruleset_version,
            evaluated_at=now,
        )

    @staticmethod
    def _run_rule(rule: Rule, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> list[RuleResult]:
        try:
            result = rule.evaluate(plan, evidence)
        except Exception:  # fail closed on ANY rule failure, so this catch is deliberately broad
            logger.exception("rule raised", extra={"rule_id": rule.rule_id})
            return [
                RuleResult(
                    rule_id=f"rule.error.{rule.rule_id}",
                    outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                    rationale="The rule failed to evaluate.",
                )
            ]
        return [] if result is None else [result]

    def _sufficiency_results(
        self, evidence: Sequence[EvidenceItem], now: datetime
    ) -> list[RuleResult]:
        fresh = {
            e.category
            for e in evidence
            if timedelta(0) <= now - e.retrieved_at <= self._policy.max_age
        }
        return [
            RuleResult(
                rule_id=f"evidence.required.{category.value}",
                outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                rationale=f"No fresh '{category.value}' evidence is available.",
                factor=RiskFactorKind.DATA_FRESHNESS,
            )
            for category in sorted(self._policy.required_categories, key=lambda c: c.value)
            if category not in fresh
        ]


def reconcile(deterministic: Recommendation, proposed: Recommendation | None) -> Reconciliation:
    """Combine the deterministic result with an optional agent/LLM proposal.

    The more conservative of the two wins, so a proposal can never relax a hard constraint.
    """
    if proposed is None:
        return Reconciliation(deterministic, deterministic, None, False)
    final = most_severe((deterministic, proposed))
    attempted_downgrade = severity(proposed) < severity(deterministic)
    return Reconciliation(final, deterministic, proposed, attempted_downgrade)
