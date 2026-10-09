"""Applies a signed-off `RuleDefinition` to evidence (ADR 0009). Pure and deterministic.

Fail-closed behaviour:
- A plan outside the definition's scope is not evaluated by it (`FactorScopeRule` reports when no
  definition for the factor applies).
- Anything unusable in scope (no evidence, a different unit, a non-numeric value) gives
  INSUFFICIENT EVIDENCE.
- The worst value over the dive window, plus the nearest instantaneous sample on each side (a
  dive between two samples is exposed to both), plus the forecast margin is what is compared, so
  the rule errs towards caution.
- A GO needs an explicit signed-off GO range.

The rule only exists for a VALIDATED definition, and the engine refuses it after `expires_at`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

from divesafe.domain import (
    SUPPORTED_METRICS,
    Comparison,
    DivePlan,
    EvidenceItem,
    Limit,
    Recommendation,
    RiskFactorKind,
    RuleDefinition,
    RuleResult,
    ThresholdStatus,
    WorseWhen,
)

NOT_EVALUATED_MARKER = "Not evaluated by this rule:"

_COMPARE = {
    Comparison.GREATER_THAN: lambda a, b: a > b,
    Comparison.GREATER_OR_EQUAL: lambda a, b: a >= b,
    Comparison.LESS_THAN: lambda a, b: a < b,
    Comparison.LESS_OR_EQUAL: lambda a, b: a <= b,
}


def _matches(limit: Limit | None, value: float) -> bool:
    return limit is not None and _COMPARE[limit.comparison](value, limit.value)


class DefinitionRule:
    def __init__(self, definition: RuleDefinition) -> None:
        if definition.status != ThresholdStatus.VALIDATED:
            raise ValueError("only a VALIDATED definition can be turned into a rule")
        assert definition.expires_on is not None  # guaranteed by the definition's validator
        self._d = definition
        self.rule_id = f"definition.{definition.id}"
        self.factor: RiskFactorKind = definition.factor
        self.expires_at: datetime = definition.expires_on
        source = definition.source
        self.citation = (
            f"{source.document}, {source.version_or_date}, {source.location_in_document}; "
            f"signed-off document version {definition.document_version}"
        )

    def _result(
        self, outcome: Recommendation, rationale: str, ids: Sequence[str] = ()
    ) -> RuleResult:
        aspects = SUPPORTED_METRICS[self._d.metric].not_evaluated
        if aspects:
            rationale += f" {NOT_EVALUATED_MARKER} {', '.join(aspects)}."
        return RuleResult(
            rule_id=self.rule_id,
            outcome=outcome,
            rationale=rationale,
            evidence_ids=tuple(ids),
            factor=self._d.factor,
            threshold_status=ThresholdStatus.VALIDATED,
        )

    def applies(self, plan: DivePlan) -> bool:
        scope = self._d.scope
        if plan.site_id not in scope.site_ids:
            return False
        return scope.max_depth_m is None or plan.max_depth_m <= scope.max_depth_m

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult | None:
        d = self._d
        if not self.applies(plan):
            return None
        metric = SUPPORTED_METRICS[d.metric]
        start = plan.planned_start
        end = start + timedelta(minutes=plan.planned_duration_minutes)
        carriers = [
            e
            for e in evidence
            if e.category == metric.category and d.metric in e.value.get("variables", {})
        ]
        items = [
            e for e in carriers if e.valid_at <= end and (e.valid_until or e.valid_at) >= start
        ]
        # A dive between two samples is exposed to both: include the nearest instant on each side.
        instants = [e for e in carriers if e.valid_until is None]
        before = [e for e in instants if e.valid_at < start]
        after = [e for e in instants if e.valid_at > end]
        if before:
            items.append(max(before, key=lambda e: e.valid_at))
        if after:
            items.append(min(after, key=lambda e: e.valid_at))
        label = f"'{d.id}' ({d.metric})"
        if not items:
            return self._result(
                Recommendation.INSUFFICIENT_EVIDENCE,
                f"No '{d.metric}' evidence overlaps the dive window for rule {label}.",
            )
        values: list[float] = []
        for item in items:
            unit = item.value.get("units", {}).get(d.metric)
            raw = item.value["variables"][d.metric]
            if unit != metric.unit or isinstance(raw, bool) or not isinstance(raw, int | float):
                return self._result(
                    Recommendation.INSUFFICIENT_EVIDENCE,
                    f"Evidence for rule {label} has an unexpected unit or a non-numeric value.",
                    [e.id for e in items],
                )
            if not math.isfinite(raw):
                return self._result(
                    Recommendation.INSUFFICIENT_EVIDENCE,
                    f"Evidence for rule {label} has a non-finite value.",
                    [e.id for e in items],
                )
            values.append(float(raw))

        higher = d.worse_when == WorseWhen.HIGHER
        worst = max(values) if higher else min(values)
        adjusted = worst + d.forecast_margin if higher else worst - d.forecast_margin
        ids = sorted(e.id for e in items)
        detail = (
            f"worst {d.metric} in the dive window {worst:g} {metric.unit}, "
            f"{adjusted:g} {metric.unit} after the signed-off forecast margin of "
            f"{d.forecast_margin:g} {metric.unit}"
        )
        if _matches(d.no_go_when, adjusted):
            return self._result(Recommendation.NO_GO, f"Rule {label}: {detail}: NO-GO range.", ids)
        if _matches(d.caution_when, adjusted):
            return self._result(
                Recommendation.CAUTION, f"Rule {label}: {detail}: CAUTION range.", ids
            )
        if _matches(d.go_when, adjusted):
            return self._result(
                Recommendation.GO, f"Rule {label}: {detail}: inside the signed-off GO range.", ids
            )
        return self._result(
            Recommendation.INSUFFICIENT_EVIDENCE,
            f"Rule {label}: {detail}: outside every signed-off range, so no recommendation.",
            ids,
        )


class FactorScopeRule:
    """One per factor that has definitions: says so when none of them applies to this plan.

    Several definitions for a factor (different sites or depth tiers) are normal, so a single
    definition being out of scope must not block the others. This result carries no VALIDATED
    status, so the factor stays uncovered and `GO` stays blocked.
    """

    citation = "derived from the scopes of the signed-off definitions"

    def __init__(self, factor: RiskFactorKind, definitions: Sequence[DefinitionRule]) -> None:
        self.factor = factor
        self.rule_id = f"definition-scope.{factor.value}"
        self._definitions = tuple(definitions)

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult | None:
        if any(d.applies(plan) for d in self._definitions):
            return None
        return RuleResult(
            rule_id=self.rule_id,
            outcome=Recommendation.INSUFFICIENT_EVIDENCE,
            rationale=(
                f"No signed-off rule for {self.factor.value} applies to this site or depth, "
                "so it is not evaluated."
            ),
            factor=self.factor,
        )
