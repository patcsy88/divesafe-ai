"""RiskAssessment (the deterministic engine's structured output) and ConfidenceAssessment."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from divesafe.domain.base import Frozen
from divesafe.domain.models import Recommendation, RuleResult, most_severe
from divesafe.domain.risk_types import THRESHOLD_FACTORS, RiskFactorKind, ThresholdStatus


class RiskAssessment(Frozen):
    """What the deterministic engine concluded, and what it could not evaluate.

    `rule_results` are the risk factors. The recommendation must equal the most severe result
    (INSUFFICIENT EVIDENCE when there are none), so a result cannot be built that relaxes it.
    `unevaluated_factors` lists every threshold-dependent factor with no VALIDATED rule: those
    were NOT assessed, whatever the recommendation says.
    """

    recommendation: Recommendation
    rule_results: tuple[RuleResult, ...]
    unevaluated_factors: tuple[RiskFactorKind, ...] = ()
    ruleset_version: str = Field(min_length=1)
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def _recommendation_is_the_most_severe_result(self) -> RiskAssessment:
        expected = most_severe(tuple(r.outcome for r in self.rule_results))
        if self.recommendation != expected:
            raise ValueError("recommendation does not match the most severe rule result")
        return self


def unevaluated_factors(
    results: tuple[RuleResult, ...], required: frozenset[RiskFactorKind] = THRESHOLD_FACTORS
) -> tuple[RiskFactorKind, ...]:
    """Required factors for which no rule with a VALIDATED limit produced a result."""
    covered = {
        r.factor
        for r in results
        if r.factor is not None and r.threshold_status == ThresholdStatus.VALIDATED
    }
    return tuple(sorted(required - covered, key=lambda k: k.value))


class ConfidenceAssessment(Frozen):
    """Reliability of an assessment (not dive safety). Every part is None until a method exists;
    a value without a documented method is rejected, so no number can look calibrated by default."""

    value: float | None = Field(default=None, ge=0.0, le=1.0)
    data_freshness: float | None = Field(default=None, ge=0.0, le=1.0)
    source_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    rule_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str | None = None
    note: str = "Reliability of this assessment, not a measure of dive safety. None = not computed."

    @model_validator(mode="after")
    def _a_number_needs_a_method(self) -> ConfidenceAssessment:
        parts = (self.value, self.data_freshness, self.source_agreement, self.rule_coverage)
        if any(p is not None for p in parts) and not (self.method or "").strip():
            raise ValueError("a confidence value requires a documented method")
        return self
