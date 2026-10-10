"""Domain models: evidence, plans, recommendations, scenarios and the audit record.

`AssessmentRecord` validates its own consistency, so a record that relaxes the deterministic
result, mislabels an override or cites unknown evidence cannot be constructed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from divesafe.domain.base import Frozen as _Frozen
from divesafe.domain.provenance import (
    NOT_MEASURED,
    DataKind,
    DataQuality,
    GeoPoint,
    TransformationStep,
)
from divesafe.domain.risk_types import THRESHOLD_FACTORS, RiskFactorKind, ThresholdStatus


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


class EvidenceItem(_Frozen):
    """One traceable fact. Every claim in a recommendation must point at one of these."""

    id: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$")
    category: DataCategory
    source: str = Field(
        min_length=1, max_length=200, description="Connector or document the fact came from."
    )
    source_version: str | None = None
    retrieved_at: AwareDatetime
    valid_at: AwareDatetime = Field(description="Start of the time the fact refers to.")
    valid_until: AwareDatetime | None = Field(
        default=None, description="End of the validity window; None means instantaneous."
    )
    is_forecast: bool = Field(description="True for any value that was not measured.")
    data_kind: DataKind
    location: GeoPoint | None = Field(
        default=None, description="Where the data is for (e.g. the model grid cell), if known."
    )
    upstream: tuple[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:._/-]{1,120}$")], ...
    ] = Field(
        default=(),
        max_length=20,
        description=(
            "Upstream data products or models this value derives from, as the provider states "
            "them. Two sources that share one are NOT independent evidence of each other."
        ),
    )
    upstream_known: bool = Field(
        default=False,
        description="True only when `upstream` is the complete list. Unknown never corroborates.",
    )
    quality: DataQuality = DataQuality.UNASSESSED
    quality_notes: tuple[Annotated[str, StringConstraints(max_length=500)], ...] = Field(
        default=(), max_length=20
    )
    transformations: tuple[TransformationStep, ...] = ()
    value: dict[str, Any]

    @model_validator(mode="after")
    def _consistent_provenance(self) -> EvidenceItem:
        if self.valid_until is not None and self.valid_until < self.valid_at:
            raise ValueError("valid_until is before valid_at")
        if self.data_kind == DataKind.OBSERVATION and (
            (self.valid_until or self.valid_at) > self.retrieved_at
        ):
            raise ValueError("an observation cannot be valid after the time it was retrieved")
        if self.is_forecast != (self.data_kind in NOT_MEASURED):
            raise ValueError(
                f"is_forecast={self.is_forecast} contradicts data_kind={self.data_kind.value}"
            )
        if self.quality == DataQuality.REJECTED:
            raise ValueError("rejected data must not be stored as evidence")
        if self.upstream_known and not self.upstream:
            raise ValueError("upstream_known requires at least one named upstream product")
        if self.upstream and len(set(self.upstream)) != len(self.upstream):
            raise ValueError("upstream products must be unique")
        return self


class DivePlan(_Frozen):
    site_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    planned_start: AwareDatetime
    planned_duration_minutes: int = Field(gt=0)
    max_depth_m: float = Field(gt=0)


class RuleResult(_Frozen):
    """The evaluation of one risk factor by one rule (also exported as `RiskFactor`)."""

    rule_id: str = Field(min_length=1)
    outcome: Recommendation
    rationale: str
    evidence_ids: tuple[str, ...] = ()
    factor: RiskFactorKind | None = None
    threshold_status: ThresholdStatus | None = Field(
        default=None,
        description="None when the rule needs no numeric limit; otherwise where the limit stands.",
    )

    @model_validator(mode="after")
    def _unvalidated_thresholds_cannot_support_go(self) -> RuleResult:
        if self.outcome not in (Recommendation.GO, Recommendation.CAUTION):
            return self
        if self.threshold_status in (
            ThresholdStatus.TBD,
            ThresholdStatus.REQUIRES_DOMAIN_VALIDATION,
        ):
            raise ValueError("a rule with an unvalidated threshold cannot return GO or CAUTION")
        if (
            self.factor is not None
            and self.factor in THRESHOLD_FACTORS
            and self.threshold_status != ThresholdStatus.VALIDATED
        ):
            raise ValueError(
                f"GO or CAUTION on '{self.factor.value}' requires a VALIDATED threshold"
            )
        return self


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
        description=(
            "Uncalibrated self-estimate of this scenario's reliability, not a measure of dive "
            "safety and not comparable with a calibrated risk-model confidence."
        ),
    )


class Finding(_Frozen):
    """One specialist agent's evidence-referenced reading of its domain. Not a recommendation."""

    agent: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    risk_factors: tuple[str, ...] = ()
    conflicting_signals: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Uncalibrated self-estimate; not a measure of dive safety.",
    )
    no_evidence: bool = False

    @model_validator(mode="after")
    def _evidence_matches_flag(self) -> Finding:
        if self.no_evidence and (self.evidence_ids or self.confidence is not None):
            raise ValueError("a no-evidence finding cannot cite evidence or claim confidence")
        if not self.no_evidence and not self.evidence_ids:
            raise ValueError("a finding must cite evidence or be marked no_evidence")
        return self


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
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Reliability of this assessment, not a measure of dive safety. None means it was "
            "not computed (no risk model yet); it is never defaulted to a number."
        ),
    )
    unevaluated_factors: tuple[RiskFactorKind, ...] = Field(
        default=(),
        description="Threshold-dependent factors with no validated rule: NOT assessed.",
    )
    confidence_method: str | None = Field(
        default=None, description="How `confidence` was computed. Required whenever it is set."
    )
    evidence_issues: tuple[str, ...] = Field(
        default=(), description="Sources or categories that failed or were rejected."
    )
    findings: tuple[Finding, ...] = ()
    scenarios: tuple[Scenario, ...] = ()
    proposal_evidence_ids: tuple[str, ...] = Field(
        default=(), description="Evidence the LLM proposal cited. Empty when there is no proposal."
    )
    explanation: str = Field(
        default="",
        description=(
            "LLM-generated prose. Unverified and untrusted: render as plain text. It never feeds "
            "a decision except through the proposal, which can only tighten the result."
        ),
    )
    model_version: str | None = None
    agent_issues: tuple[str, ...] = Field(
        default=(), description="LLM agent calls that failed or were rejected."
    )
    ruleset_version: str = Field(min_length=1)
    data_versions: dict[str, str] = Field(default_factory=dict)
    created_by: str | None = Field(default=None, description="Actor who requested the assessment.")
    assigned_decider: str | None = Field(
        default=None,
        description=(
            "The only actor who may record the human decision and actual conditions. New "
            "assessments always have one. None (a record made without one) can be decided by "
            "nobody."
        ),
        max_length=100,
    )
    human_decision: HumanDecision | None = None
    actual_conditions: ActualConditions | None = None

    @property
    def status(self) -> Literal["PENDING_HUMAN", "DECIDED"]:
        return "DECIDED" if self.human_decision is not None else "PENDING_HUMAN"

    @property
    def llm_tightened(self) -> bool:
        """True when an unverified LLM proposal made the result more severe than the rules."""
        proposed = self.proposed_recommendation
        return proposed is not None and severity(proposed) > severity(
            self.deterministic_recommendation
        )

    @property
    def overrides_to_less_severe(self) -> bool:
        """True when the human chose a less severe outcome than the system recommended."""
        d = self.human_decision
        return d is not None and severity(d.decision) < severity(self.final_recommendation)

    @model_validator(mode="after")
    def _check_consistency(self) -> AssessmentRecord:
        if self.confidence is not None and not (self.confidence_method or "").strip():
            raise ValueError("a confidence value requires a documented method")
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence ids must be unique")

        cited = {i for r in self.rule_results for i in r.evidence_ids}
        cited |= {i for s in self.scenarios for i in s.evidence_ids}
        cited |= {i for f in self.findings for i in f.evidence_ids}
        cited |= set(self.proposal_evidence_ids)
        if self.proposal_evidence_ids and self.proposed_recommendation is None:
            raise ValueError("proposal_evidence_ids require a proposal")
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

        if self.evidence_issues and severity(self.deterministic_recommendation) < severity(
            Recommendation.INSUFFICIENT_EVIDENCE
        ):
            raise ValueError("evidence issues require at least INSUFFICIENT EVIDENCE")

        if self.human_decision is not None:
            if self.human_decision.decided_at < self.created_at:
                raise ValueError("decided_at is before the assessment was created")
            is_override = self.human_decision.decision != self.final_recommendation
            if self.human_decision.is_override != is_override:
                raise ValueError("is_override must equal (human decision != recommendation)")
            if self.actual_conditions is not None:
                reported = self.actual_conditions.reported_at
                if reported < self.human_decision.decided_at:
                    raise ValueError("actual conditions are dated before the human decision")
                if reported < self.plan.planned_start:
                    raise ValueError("actual conditions are dated before the planned dive")
        elif self.actual_conditions is not None:
            raise ValueError("actual conditions require a recorded human decision")
        return self
