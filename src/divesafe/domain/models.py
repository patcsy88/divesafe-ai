"""Domain models: evidence, plans, recommendations, scenarios and the audit record.

`AssessmentRecord` validates its own consistency, so a record that relaxes the deterministic
result, mislabels an override or cites unknown evidence cannot be constructed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Recommendation(StrEnum):
    GO = "GO"
    CAUTION = "CAUTION"
    NO_GO = "NO-GO"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"


_SEVERITY: dict[Recommendation, int] = {
    Recommendation.GO: 0,
    Recommendation.CAUTION: 1,
    Recommendation.INSUFFICIENT_EVIDENCE: 2,
    Recommendation.NO_GO: 3,
}


def severity(recommendation: Recommendation) -> int:
    """GO < CAUTION < INSUFFICIENT EVIDENCE < NO-GO."""
    return _SEVERITY[recommendation]


def most_severe(outcomes: list[Recommendation] | tuple[Recommendation, ...]) -> Recommendation:
    """Most severe outcome; INSUFFICIENT EVIDENCE when there are none (fail closed)."""
    return max(outcomes, key=severity) if outcomes else Recommendation.INSUFFICIENT_EVIDENCE


class DataCategory(StrEnum):
    WEATHER = "weather"
    WIND = "wind"
    WAVES_SWELL = "waves_swell"
    CURRENTS = "currents"
    TIDES = "tides"
    SEA_TEMPERATURE = "sea_temperature"
    MARINE_WARNINGS = "marine_warnings"
    HISTORICAL_OBSERVATIONS = "historical_observations"
    SITE_INFORMATION = "site_information"
    LOCAL_GUIDANCE = "local_guidance"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceItem(_Frozen):
    """One traceable fact. Every claim in a recommendation must point at one of these."""

    id: str = Field(min_length=1)
    category: DataCategory
    source: str = Field(min_length=1, description="Connector or document the fact came from.")
    source_version: str | None = None
    retrieved_at: AwareDatetime
    valid_at: AwareDatetime = Field(description="Time the observation or forecast refers to.")
    is_forecast: bool
    value: dict[str, Any]


class DivePlan(_Frozen):
    site_id: str = Field(min_length=1)
    planned_start: AwareDatetime
    planned_duration_minutes: int = Field(gt=0)
    max_depth_m: float = Field(gt=0)


class RuleResult(_Frozen):
    rule_id: str = Field(min_length=1)
    outcome: Recommendation
    rationale: str
    evidence_ids: tuple[str, ...] = ()


class ScenarioKind(StrEnum):
    FAVOURABLE = "favourable"
    MARGINAL = "marginal"
    DETERIORATING = "deteriorating"


class Scenario(_Frozen):
    """One bounded Tree-of-Thought branch. A summary with references, not a reasoning trace."""

    kind: ScenarioKind
    summary: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    risk_factors: tuple[str, ...] = ()
    conflicting_signals: tuple[str, ...] = ()
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Reliability of this assessment. It is not a measure of dive safety.",
    )


class HumanDecision(_Frozen):
    """The human's decision. Any override, including to a less severe outcome, is allowed
    (ADR 0004) but needs an identified decision-maker and a non-blank rationale.
    """

    decided_by: str
    decision: Recommendation
    decided_at: AwareDatetime
    is_override: bool
    override_rationale: str | None = None

    @model_validator(mode="after")
    def _require_rationale_for_override(self) -> HumanDecision:
        if not self.decided_by.strip():
            raise ValueError("decided_by must identify the human decision-maker")
        if self.is_override and not (self.override_rationale or "").strip():
            raise ValueError("an override requires a non-blank rationale")
        return self


class ActualConditions(_Frozen):
    reported_at: AwareDatetime
    reported_by: str = Field(min_length=1)
    observations: dict[str, Any]


class AssessmentRecord(_Frozen):
    """Immutable audit record of one assessment and what the human did with it.

    `final_recommendation` is the system's recommendation, not an action. Until a
    `HumanDecision` is attached the record is `PENDING_HUMAN` and must not be acted on.
    """

    id: str = Field(min_length=1)
    created_at: AwareDatetime
    plan: DivePlan
    evidence: tuple[EvidenceItem, ...]
    rule_results: tuple[RuleResult, ...]
    deterministic_recommendation: Recommendation
    proposed_recommendation: Recommendation | None
    final_recommendation: Recommendation
    llm_attempted_downgrade: bool
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Reliability of this assessment. It is not a measure of dive safety.",
    )
    scenarios: tuple[Scenario, ...] = ()
    explanation: str = ""
    model_version: str | None = None
    ruleset_version: str = Field(min_length=1)
    data_versions: dict[str, str] = Field(default_factory=dict)
    human_decision: HumanDecision | None = None
    actual_conditions: ActualConditions | None = None

    @property
    def status(self) -> Literal["PENDING_HUMAN", "DECIDED"]:
        return "DECIDED" if self.human_decision is not None else "PENDING_HUMAN"

    @property
    def overrides_to_less_severe(self) -> bool:
        """True when the human chose a less severe outcome than the system recommended."""
        d = self.human_decision
        return d is not None and severity(d.decision) < severity(self.final_recommendation)

    @model_validator(mode="after")
    def _check_consistency(self) -> AssessmentRecord:
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence ids must be unique")

        cited = {i for r in self.rule_results for i in r.evidence_ids}
        cited |= {i for s in self.scenarios for i in s.evidence_ids}
        unknown = cited - set(ids)
        if unknown:
            raise ValueError(f"cited evidence ids not present in the record: {sorted(unknown)}")

        expected_deterministic = most_severe(tuple(r.outcome for r in self.rule_results))
        if self.deterministic_recommendation != expected_deterministic:
            raise ValueError("deterministic_recommendation does not match the rule results")

        proposed = self.proposed_recommendation
        expected_final = (
            self.deterministic_recommendation
            if proposed is None
            else most_severe((self.deterministic_recommendation, proposed))
        )
        if self.final_recommendation != expected_final:
            raise ValueError("final_recommendation must be the most severe of rules and proposal")

        expected_downgrade = proposed is not None and severity(proposed) < severity(
            self.deterministic_recommendation
        )
        if self.llm_attempted_downgrade != expected_downgrade:
            raise ValueError("llm_attempted_downgrade is inconsistent with the proposal")

        if self.human_decision is not None:
            is_override = self.human_decision.decision != self.final_recommendation
            if self.human_decision.is_override != is_override:
                raise ValueError("is_override must equal (human decision != recommendation)")
        elif self.actual_conditions is not None:
            raise ValueError("actual conditions require a recorded human decision")
        return self
