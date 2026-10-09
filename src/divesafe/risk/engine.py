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
from typing import Final

from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskAssessment,
    RiskFactorKind,
    RuleResult,
    effective_quality,
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


# (kind -> categories) whose existence matters whether or not they span the dive window: a
# warning or a piece of guidance is relevant on its own. This is a reviewed allow-list: a
# connector cannot dodge coverage by labelling weather or wave data as a notice.
WINDOW_EXEMPT: dict[DataKind, frozenset[DataCategory]] = {
    DataKind.NOTICE: frozenset({DataCategory.MARINE_WARNINGS}),
    DataKind.KNOWLEDGE: frozenset(
        {
            DataCategory.SITE_INFORMATION,
            DataCategory.LOCAL_GUIDANCE,
            DataCategory.HISTORICAL_OBSERVATIONS,
        }
    ),
}


def _exempt(item: EvidenceItem, window_start: datetime) -> bool:
    """Exempt from window coverage, provided it has not already ended before the dive."""
    if item.category not in WINDOW_EXEMPT.get(item.data_kind, frozenset()):
        return False
    return item.valid_until is None or item.valid_until >= window_start


def _covers(items: Sequence[EvidenceItem], start: datetime, end: datetime) -> bool:
    """Do these items cover [start, end]?

    Intervals (valid_until set) must chain without a gap, which needs no invented number. Instants
    can only be bracketed: a hole inside a series of instants is NOT detected (docs/adr/0008).
    """
    intervals = sorted(
        ((e.valid_at, e.valid_until) for e in items if e.valid_until is not None),
        key=lambda pair: pair[0],
    )
    if intervals:
        reach = None
        for begin, finish in intervals:
            if reach is None:
                if begin > start:
                    break
                reach = finish
            elif begin <= reach:
                reach = max(reach, finish)
            else:
                break
        if reach is not None and reach >= end:
            return True
    instants = [e.valid_at for e in items if e.valid_until is None]
    return bool(instants) and min(instants) <= start and max(instants) >= end


DEFAULT_ACCEPTED_QUALITY: Final = frozenset({DataQuality.VALIDATED, DataQuality.DEGRADED})

# Rule ids the engine itself emits, reserved so the API can recognise them by prefix.
EVIDENCE_REQUIRED_PREFIX: Final = "evidence.required."
EVIDENCE_QUALITY_PREFIX: Final = "evidence.quality"
CITES_UNUSABLE_PREFIX: Final = "rule.cites_unusable_evidence."
DUPLICATE_IDS_RULE: Final = "evidence.duplicate_ids"
ENGINE_RULE_PREFIXES: Final = (
    EVIDENCE_REQUIRED_PREFIX,
    EVIDENCE_QUALITY_PREFIX,
    CITES_UNUSABLE_PREFIX,
    DUPLICATE_IDS_RULE,
)


@dataclass(frozen=True)
class EvidencePolicy:
    """What counts as usable evidence for a required category.

    Usable means: fresh (`retrieved_at`), of an accepted EFFECTIVE quality, and, unless exempt,
    bracketing the dive window (see `RiskRulesEngine`). `degraded_may_support_go` is a risk
    decision for the product owner (docs/adr/0008); the default is the conservative answer.
    """

    required_categories: frozenset[DataCategory]
    max_age: timedelta
    accepted_quality: frozenset[DataQuality] = DEFAULT_ACCEPTED_QUALITY
    require_window_coverage: bool = True
    degraded_may_support_go: bool = False

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        if DataQuality.REJECTED in self.accepted_quality:
            raise ValueError("rejected data can never be accepted")


def _covers_window(items: Sequence[EvidenceItem], start: datetime, end: datetime) -> bool:
    """Exempt items need no cover; everything else must cover [start, end] together."""
    spans = [e for e in items if not _exempt(e, start)]
    return not spans or _covers(spans, start, end)


def _relevant_to_window(item: EvidenceItem, start: datetime, end: datetime) -> bool:
    """Does the item say anything about the window (overlap), or is it exempt?"""
    if _exempt(item, start):
        return True
    return item.valid_at <= end and (item.valid_until or item.valid_at) >= start


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
        results, usable = self._sufficiency(plan, evidence, now)
        ids = [e.id for e in evidence]
        if len(ids) != len(set(ids)):
            results.append(
                RuleResult(
                    rule_id=DUPLICATE_IDS_RULE,
                    outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                    rationale="Evidence ids are not unique, so citations are ambiguous.",
                    factor=RiskFactorKind.DATA_QUALITY,
                )
            )
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

        results.extend(self._citation_guard(results, plan, evidence, now))
        if most_severe(tuple(r.outcome for r in results)) in (
            Recommendation.GO,
            Recommendation.CAUTION,
        ):
            guard = self._quality_guard(plan, usable)
            if guard is not None:
                results.append(guard)

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

    @staticmethod
    def _window(plan: DivePlan) -> tuple[datetime, datetime]:
        start = plan.planned_start
        return start, start + timedelta(minutes=plan.planned_duration_minutes)

    def _usable(
        self,
        category: DataCategory,
        plan: DivePlan,
        evidence: Sequence[EvidenceItem],
        now: datetime,
    ) -> tuple[list[EvidenceItem], str | None, RiskFactorKind]:
        """Usable evidence for one category, or the reason there is none and the factor at fault."""
        policy = self._policy
        freshness = RiskFactorKind.DATA_FRESHNESS
        items = [e for e in evidence if e.category == category]
        if not items:
            return [], f"No '{category.value}' evidence is available.", freshness
        fresh = [e for e in items if timedelta(0) <= now - e.retrieved_at <= policy.max_age]
        if not fresh:
            return [], f"No fresh '{category.value}' evidence is available.", freshness
        accepted = [e for e in fresh if effective_quality(e) in policy.accepted_quality]
        if not accepted:
            return (
                [],
                f"'{category.value}' evidence is not of an accepted quality.",
                RiskFactorKind.DATA_QUALITY,
            )
        if policy.require_window_coverage and not _covers_window(accepted, *self._window(plan)):
            return [], f"'{category.value}' evidence does not cover the dive window.", freshness
        return accepted, None, freshness

    def _sufficiency(
        self, plan: DivePlan, evidence: Sequence[EvidenceItem], now: datetime
    ) -> tuple[list[RuleResult], dict[DataCategory, list[EvidenceItem]]]:
        results: list[RuleResult] = []
        usable: dict[DataCategory, list[EvidenceItem]] = {}
        for category in sorted(self._policy.required_categories, key=lambda c: c.value):
            items, problem, factor = self._usable(category, plan, evidence, now)
            if problem is None:
                usable[category] = items
                continue
            results.append(
                RuleResult(
                    rule_id=f"{EVIDENCE_REQUIRED_PREFIX}{category.value}",
                    outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                    rationale=problem,
                    factor=factor,
                )
            )
        return results, usable

    def _quality_guard(
        self, plan: DivePlan, usable: dict[DataCategory, list[EvidenceItem]]
    ) -> RuleResult | None:
        """GO and CAUTION need VALIDATED evidence, which must cover the window on its own, so a
        validated item elsewhere in time cannot launder a degraded one that covers the dive."""
        if self._policy.degraded_may_support_go:
            return None
        window = self._window(plan)
        degraded: list[str] = []
        for category, items in sorted(usable.items(), key=lambda kv: kv[0].value):
            validated = [e for e in items if effective_quality(e) == DataQuality.VALIDATED]
            covered = bool(validated) and (
                not self._policy.require_window_coverage or _covers_window(validated, *window)
            )
            if not covered:
                degraded.append(category.value)
        if not degraded:
            return None
        return RuleResult(
            rule_id=f"{EVIDENCE_QUALITY_PREFIX}_insufficient_for_go",
            outcome=Recommendation.INSUFFICIENT_EVIDENCE,
            rationale=(
                "GO and CAUTION need VALIDATED evidence covering the dive window for every "
                "required category; missing for: " + ", ".join(degraded)
            ),
            factor=RiskFactorKind.DATA_QUALITY,
        )

    def _citation_guard(
        self,
        results: list[RuleResult],
        plan: DivePlan,
        evidence: Sequence[EvidenceItem],
        now: datetime,
    ) -> list[RuleResult]:
        """A GO or CAUTION may not rest on evidence that is stale, of unaccepted quality, or
        unrelated to the dive window."""
        policy = self._policy
        start, end = self._window(plan)
        pool = {
            e.id
            for e in evidence
            if timedelta(0) <= now - e.retrieved_at <= policy.max_age
            and effective_quality(e) in policy.accepted_quality
            and (not policy.require_window_coverage or _relevant_to_window(e, start, end))
        }
        return [
            RuleResult(
                rule_id=f"{CITES_UNUSABLE_PREFIX}{r.rule_id}",
                outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                rationale=(
                    "The rule cites evidence that is stale, of unaccepted quality or outside "
                    "the dive window."
                ),
                factor=RiskFactorKind.DATA_QUALITY,
            )
            for r in results
            if r.outcome in (Recommendation.GO, Recommendation.CAUTION)
            and not set(r.evidence_ids) <= pool
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
