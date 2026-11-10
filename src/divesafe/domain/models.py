"""Domain models: evidence, plans, recommendations, scenarios and the audit record.

`AssessmentRecord` validates its own consistency, so a record that relaxes the deterministic
result, mislabels an override or cites unknown evidence cannot be constructed.
"""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from divesafe.domain.base import Frozen as _Frozen
from divesafe.domain.provenance import (
    NOT_MEASURED,
    DataKind,
    DataQuality,
    GeoPoint,
    TransformationStep,
)
from divesafe.domain.risk_types import THRESHOLD_FACTORS, RiskFactorKind, ThresholdStatus
from divesafe.domain.stages import (
    MAX_EVIDENCE_ITEMS,
    STAGE_ORDER,
    StageName,
    StageTrace,
)


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
        if self.data_kind == DataKind.KNOWLEDGE:
            # ADR 0022 decision 5. `text` is not required, so the record form validates.
            if self.category not in KNOWLEDGE_CATEGORIES:
                raise ValueError("knowledge is valid only as site_information or local_guidance")
            if self.valid_until is None:
                raise ValueError("knowledge requires valid_until (its review-by date)")
        return self


# ADR 0022 decision 5: the only categories a KNOWLEDGE item may have. `historical_observations`
# is reserved for a future typed history service with its own ADR.
KNOWLEDGE_CATEGORIES: frozenset[DataCategory] = frozenset(
    {DataCategory.SITE_INFORMATION, DataCategory.LOCAL_GUIDANCE}
)


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


def _check_flagged_positions(flagged: tuple[int, ...], signals: tuple[str, ...]) -> None:
    if len(set(flagged)) != len(flagged) or any(not 0 <= i < len(signals) for i in flagged):
        raise ValueError("flagged_signals must be distinct positions within conflicting_signals")


class Scenario(_Frozen):
    """One bounded Tree-of-Thought branch. A summary with references, not a reasoning trace."""

    kind: ScenarioKind
    summary: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    risk_factors: tuple[str, ...] = ()
    conflicting_signals: tuple[str, ...] = ()
    flagged_signals: tuple[StrictInt, ...] = Field(
        default=(),
        description="Positions in conflicting_signals the code's wording screen flagged.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Uncalibrated self-estimate of this scenario's reliability, not a measure of dive "
            "safety and not comparable with a calibrated risk-model confidence."
        ),
    )

    @model_validator(mode="after")
    def _flags_within_signals(self) -> Scenario:
        _check_flagged_positions(self.flagged_signals, self.conflicting_signals)
        return self


class Finding(_Frozen):
    """One specialist agent's evidence-referenced reading of its domain. Not a recommendation."""

    agent: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    risk_factors: tuple[str, ...] = ()
    conflicting_signals: tuple[str, ...] = ()
    flagged_signals: tuple[StrictInt, ...] = Field(
        default=(),
        description="Positions in conflicting_signals the code's wording screen flagged.",
    )
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
        _check_flagged_positions(self.flagged_signals, self.conflicting_signals)
        if self.no_evidence and (self.evidence_ids or self.confidence is not None):
            raise ValueError("a no-evidence finding cannot cite evidence or claim confidence")
        if not self.no_evidence and not self.evidence_ids:
            raise ValueError("a finding must cite evidence or be marked no_evidence")
        return self


MAX_REASSIGNMENTS = 10


class Reassignment(_Frozen):
    """One move of a still-pending assessment to another decider (ADR 0026). Appended by an
    `admin`, never edited. Names are the configured actor names; `key_id` labels the admin's key."""

    from_decider: str = Field(min_length=1, max_length=100)
    to_decider: str = Field(min_length=1, max_length=100)
    reassigned_by: str = Field(min_length=1, max_length=100)
    reassigned_at: AwareDatetime
    key_id: str = Field(pattern=r"^(?:[0-9a-f]{12}|dev)$")

    @model_validator(mode="after")
    def _moves_to_someone_else(self) -> Reassignment:
        if self.from_decider.strip().casefold() == self.to_decider.strip().casefold():
            raise ValueError("a reassignment must name a different decider")
        return self


class HumanDecision(_Frozen):
    """The human's decision. Any override is allowed (ADR 0004); a less severe one needs the
    `overrider` role (ADR 0026, enforced by the API). It needs an identified decision-maker and a
    non-blank rationale.
    """

    decided_by: str
    decision: Recommendation
    decided_at: AwareDatetime
    is_override: bool
    override_rationale: str | None = None
    # Set by code, never by a caller (ADR 0026). None = not recorded (a decision made before
    # these fields existed). `key_id` is a non-secret label of the key used, or "dev".
    key_id: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{12}|dev)$")
    decided_by_creator: bool | None = None

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


class SupervisorCheck(StrEnum):
    """The code-only Supervise stage's checks (ADR 0025 item 2). Fixed names, no free text."""

    RECONCILE_TIGHTEN_ONLY = "reconcile_tighten_only"
    SEVERITY_MONOTONIC = "severity_monotonic"
    CITATIONS_VALID = "citations_valid"
    PROMPT_VERSION_PINNED = "prompt_version_pinned"
    REQUIRED_CATEGORIES_PRESENT = "required_categories_present"


# A breach of these is an invariant failure: the run raises and nothing is stored, so a stored
# record can never show one of them as failed.
INVARIANT_CHECKS: frozenset[SupervisorCheck] = frozenset(
    {SupervisorCheck.RECONCILE_TIGHTEN_ONLY, SupervisorCheck.SEVERITY_MONOTONIC}
)
SUPERVISOR_RULE_PREFIX = "supervisor."


def supervisor_rule_id(check: SupervisorCheck) -> str:
    """The rule id Supervise appends for a failed tightening check."""
    return f"{SUPERVISOR_RULE_PREFIX}{check.value}"


class SupervisorVerdict(_Frozen):
    """What the Supervise stage found. `tightened_to` is the final recommendation `reconcile`
    produced after a failed check added its rule result, or None when nothing changed."""

    passed: tuple[SupervisorCheck, ...]
    failed: tuple[SupervisorCheck, ...] = ()
    tightened_to: Recommendation | None = None

    @model_validator(mode="after")
    def _each_check_once(self) -> SupervisorVerdict:
        seen = (*self.passed, *self.failed)
        if len(seen) != len(set(seen)):
            raise ValueError("each supervisor check appears at most once")
        if INVARIANT_CHECKS & set(self.failed):
            raise ValueError("an invariant check cannot be recorded as failed")
        if self.tightened_to is not None and not self.failed:
            raise ValueError("tightened_to requires a failed check")
        return self


class RecordEgressBasis(StrEnum):
    """Why the run's model server could receive a prompt, as stored on the record (ADR 0022
    decision 15). A superset of `models.egress.EgressBasis`: `no_agent_stage` comes only from the
    no-provider case and never from `check_egress`. Knowledge may reach a prompt only under
    `declared_local`."""

    DECLARED_LOCAL = "declared_local"
    LOOPBACK_EXTERNAL_OPT_IN = "loopback_external_opt_in"
    REMOTE_EXTERNAL_OPT_IN = "remote_external_opt_in"
    NONE = "none"
    NO_AGENT_STAGE = "no_agent_stage"


# The bases with no model server, so no host (ADR 0022 decision 15).
_NO_HOST_BASES: frozenset[RecordEgressBasis] = frozenset(
    {RecordEgressBasis.NONE, RecordEgressBasis.NO_AGENT_STAGE}
)
KNOWLEDGE_ID_PREFIX = "kb:"
# ADR 0022 decision 6: the strict document id grammar, and version / chunk 1..999999.
KB_DOCUMENT_ID_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+){0,2}$"
_KB_DOCUMENT_ID = re.compile(KB_DOCUMENT_ID_PATTERN)
_KB_NUMBER = re.compile(r"^[1-9][0-9]{0,5}$")
# ADR 0022 decision 12: at most 20 retrieved chunks per run.
MAX_KNOWLEDGE_ITEMS = 20
# A DNS name (as `config.settings._host_set` accepts) or an IP literal, lowercase, no brackets.
_HOST_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class KnowledgeRef(_Frozen):
    """One knowledge chunk by id only (document, version, chunk), never by title."""

    document_id: str = Field(max_length=40, pattern=KB_DOCUMENT_ID_PATTERN)
    version: StrictInt = Field(ge=1, le=999999)
    chunk: StrictInt = Field(ge=1, le=999999)


class KnowledgeVersionRef(_Frozen):
    """One document version by id only (document, version), never by title."""

    document_id: str = Field(max_length=40, pattern=KB_DOCUMENT_ID_PATTERN)
    version: StrictInt = Field(ge=1, le=999999)


class KnowledgeNoteCode(StrEnum):
    """Why knowledge is absent or partial without being an evidence issue (ADR 0022 decisions 3,
    9 and 14). RECORD AND HUMAN VIEW ONLY: a note code never enters a prompt. The model gets only
    the prompt layer's fixed absence line, never these codes or their wording. Notes never force
    an outcome; anything that should is an evidence issue instead."""

    # No knowledge connector at all (a documented gap: a note, not an issue).
    NOT_CONFIGURED = "knowledge_not_configured"
    # Families whose governing version is withdrawn (decision 3 table, withdrawn row).
    WITHDRAWN = "knowledge_withdrawn"
    # Families with only future-effective, draft or never-approved versions.
    NOT_IN_FORCE = "knowledge_not_in_force"
    # Families past review-by beside live ones (all expired is an evidence issue instead).
    EXPIRED = "knowledge_expired"
    # The catch-all: zero items returned and no issue.
    NONE_RETURNED = "knowledge_none_returned"


# Codes that count document families, and those that carry no count (always 0).
FAMILY_NOTE_CODES: frozenset[KnowledgeNoteCode] = frozenset(
    {KnowledgeNoteCode.WITHDRAWN, KnowledgeNoteCode.NOT_IN_FORCE, KnowledgeNoteCode.EXPIRED}
)
# A capacity bound on a count, not a safety threshold (the version range of decision 6).
MAX_NOTE_COUNT = 999999


class KnowledgeNote(_Frozen):
    """One typed note: a fixed code and a count, never connector text. For the record and the
    human view only; never placed in a prompt (see `KnowledgeNoteCode`)."""

    code: KnowledgeNoteCode
    count: StrictInt = Field(
        default=0,
        ge=0,
        le=MAX_NOTE_COUNT,
        description="Document families concerned; 0 for codes that do not count families.",
    )

    @model_validator(mode="after")
    def _count_matches_code(self) -> KnowledgeNote:
        if (self.code in FAMILY_NOTE_CODES) != (self.count > 0):
            raise ValueError("a family note needs a positive count; other notes carry none")
        return self


def is_knowledge_id(evidence_id: str) -> bool:
    """Does the id use the reserved `kb:` prefix (compared case-insensitively)? Says nothing
    about whether it follows the grammar; `knowledge_ref` checks that."""
    return evidence_id[: len(KNOWLEDGE_ID_PREFIX)].casefold() == KNOWLEDGE_ID_PREFIX


def knowledge_ref(evidence_id: str) -> KnowledgeRef | None:
    """The chunk a `kb:<document_id>:<version>:<chunk>` evidence id names, or None for an id
    outside the reserved `kb:` prefix (compared case-insensitively). Raises `ValueError` for a
    reserved id that does not follow the decision 6 grammar."""
    if not is_knowledge_id(evidence_id):
        return None
    parts = evidence_id.split(":")
    if (
        len(parts) != 4
        or parts[0] != "kb"
        or len(parts[1]) > 40
        or not _KB_DOCUMENT_ID.fullmatch(parts[1])
        or not _KB_NUMBER.fullmatch(parts[2])
        or not _KB_NUMBER.fullmatch(parts[3])
    ):
        raise ValueError("a kb: evidence id must be kb:<document_id>:<version>:<chunk>")
    return KnowledgeRef(document_id=parts[1], version=int(parts[2]), chunk=int(parts[3]))


# Mirrors `models.loopback.is_loopback`, which `domain` may not import.
_LOOPBACK_NAMES = frozenset({"localhost"})


def _valid_host(host: str) -> bool:
    """A lowercase DNS name or an IP literal: no scheme, port, userinfo, path, brackets or zone
    id, and no `0x` label or all-digit last label (an address disguised as a name)."""
    if len(host) > 253 or "%" in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host == host.lower()
    return (
        bool(_HOST_NAME.fullmatch(host))
        and not host.rsplit(".", 1)[-1].isdigit()
        and not any(label.startswith("0x") or len(label) > 63 for label in host.split("."))
    )


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


# A basis `check_egress` gives only for a loopback server (`external` false), and the one it gives
# only for a remote one.
_LOOPBACK_BASES: frozenset[RecordEgressBasis] = frozenset(
    {RecordEgressBasis.DECLARED_LOCAL, RecordEgressBasis.LOOPBACK_EXTERNAL_OPT_IN}
)


class AssessmentRecord(_Frozen):
    """Immutable audit record of one assessment and what the human did with it.

    `final_recommendation` is the system's recommendation, not an action. Until a
    `HumanDecision` is attached the record is `PENDING_HUMAN` and must not be acted on.
    """

    # A validation error must not carry record content (decision text, model output) into a log.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

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
    withheld_evidence: tuple[EvidenceItem, ...] = Field(
        default=(),
        description=(
            "Items dropped because different items shared one id. For the human reviewer only: "
            "Not in the main result, NOT citable, never given to agents or prompts; only the "
            "deterministic variant search reads them, to add severity (ADR 0026). If that "
            "search was not possible they may hold a more severe result than anything evaluated."
        ),
    )
    findings: tuple[Finding, ...] = ()
    cross_examination_findings: tuple[Finding, ...] = Field(
        default=(),
        description=(
            "Second-round specialist findings (ADR 0025 item 5), only when cross-examination is "
            "on. They feed the proposal and never the scenarios; each cites evidence ids only."
        ),
    )
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
    reassignments: tuple[Reassignment, ...] = Field(
        default=(),
        max_length=MAX_REASSIGNMENTS,
        description=(
            "Moves of this assessment between deciders before it was decided, oldest first "
            "(ADR 0026). `assigned_decider` is the last one's target."
        ),
    )
    human_decision: HumanDecision | None = None
    actual_conditions: ActualConditions | None = None
    supervisor_verdict: SupervisorVerdict | None = Field(
        default=None,
        description=(
            "The code-only Supervise stage's checks (ADR 0025). None on records made before it "
            "existed. A failed check is also a `supervisor.<check>` rule result."
        ),
    )
    egress_basis: RecordEgressBasis | None = Field(
        default=None,
        description=(
            "Why the model server could receive a prompt (ADR 0022 decision 15, ADR 0023). None "
            "on records made before it existed. Knowledge (`kb:` evidence) may reach a prompt "
            "only under `declared_local`; under any other value, or None, every knowledge item "
            "is listed in `withheld_knowledge`."
        ),
    )
    model_host: str | None = Field(
        default=None,
        max_length=253,
        description=(
            "Host name of the model server only: no scheme, port, path or userinfo. None when "
            "the basis is `none` or `no_agent_stage`, or the basis is absent."
        ),
    )
    withheld_knowledge: tuple[KnowledgeRef, ...] = Field(
        default=(),
        max_length=MAX_KNOWLEDGE_ITEMS,
        description=(
            "Knowledge items in the evidence that were NOT sent to a model because of the egress "
            "basis, by id only (ADR 0022 decision 15). Not `withheld_evidence` (ADR 0008)."
        ),
    )
    notes: tuple[KnowledgeNote, ...] = Field(
        default=(),
        max_length=len(KnowledgeNoteCode),
        description=(
            "Knowledge notes (ADR 0022 decision 14): typed codes and counts, never connector "
            "text. Shown to the human in fixed wording; never sent to a model; never force an "
            "outcome. Empty on records made before they existed."
        ),
    )
    superseded_knowledge: tuple[KnowledgeVersionRef, ...] = Field(
        default=(),
        max_length=MAX_KNOWLEDGE_ITEMS,
        description=(
            "Document versions replaced by a newer one in force, by id only (ADR 0022 decisions "
            "10 and 14). Shown to the human; never sent to a model."
        ),
    )

    stages: tuple[StageTrace, ...] = Field(
        default=(),
        description=(
            "One entry per executed stage, in graph order (ADR 0025 item 3): outcome, "
            "evidence ids used and fixed issue codes. Empty on records made before it existed. "
            "Not a reasoning trace. started_at/finished_at are coarse bounds (the run's now and "
            "one later clock reading taken in Rejudge), not per-stage measurements; duration_ms "
            "is the stage's own monotonic wall time."
        ),
        max_length=len(STAGE_ORDER),
    )

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

        if {e.id for e in self.withheld_evidence} & set(ids):
            raise ValueError("withheld evidence ids must not appear in the evidence")
        if self.withheld_evidence:
            if not any(s.startswith("evidence.conflict:") for s in self.evidence_issues):
                raise ValueError("withheld evidence requires an evidence.conflict issue")
            by_id: dict[str, list[EvidenceItem]] = {}
            for held in self.withheld_evidence:
                by_id.setdefault(held.id, []).append(held)
            if any(all(x == items[0] for x in items) for items in by_id.values()):
                raise ValueError("each withheld id must appear on at least two different items")

        cited = {i for r in self.rule_results for i in r.evidence_ids}
        cited |= {i for s in self.scenarios for i in s.evidence_ids}
        cited |= {i for f in self.findings for i in f.evidence_ids}
        cited |= {i for f in self.cross_examination_findings for i in f.evidence_ids}
        cited |= set(self.proposal_evidence_ids)
        cited |= {i for t in self.stages for i in t.evidence_ids}
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

        self._check_supervisor()
        self._check_stages()
        self._check_egress()
        self._check_notes()

        self._check_reassignments()
        if self.human_decision is not None:
            if self.human_decision.decided_at < self.created_at:
                raise ValueError("decided_at is before the assessment was created")
            is_override = self.human_decision.decision != self.final_recommendation
            if self.human_decision.is_override != is_override:
                raise ValueError("is_override must equal (human decision != recommendation)")
            flag = self.human_decision.decided_by_creator
            if flag is not None and (
                self.created_by is None
                or flag
                != (
                    self.created_by.strip().casefold()
                    == self.human_decision.decided_by.strip().casefold()
                )
            ):
                raise ValueError("decided_by_creator must equal (decided_by == created_by)")
            if self.actual_conditions is not None:
                reported = self.actual_conditions.reported_at
                if reported < self.human_decision.decided_at:
                    raise ValueError("actual conditions are dated before the human decision")
                if reported < self.plan.planned_start:
                    raise ValueError("actual conditions are dated before the planned dive")
        elif self.actual_conditions is not None:
            raise ValueError("actual conditions require a recorded human decision")
        return self

    def _check_egress(self) -> None:
        """ADR 0022 decision 15, checkable from the record alone: knowledge in a prompt implies
        `declared_local`. Knowledge here means evidence with a `kb:` id (owner decision
        2026-11-06). Since the owner decision of 2026-11-10 (reversing condition 7) the record
        refuses a KNOWLEDGE item without a `kb:` id and a `kb:` id on any other kind, in both
        directions as `services.evidence.screen_knowledge` does, so the two meanings coincide.
        Fixed messages: no id or content."""
        # Withheld items too: they are shown to the human and read by the variant search.
        # The messages for `evidence` are unchanged; withheld ones name the field (no id).
        for items, where in ((self.evidence, "evidence"), (self.withheld_evidence, "withheld")):
            for e in items:
                if e.data_kind is DataKind.KNOWLEDGE and not is_knowledge_id(e.id):
                    raise ValueError(f"a knowledge {where} item must have a kb: id")
                if e.data_kind is not DataKind.KNOWLEDGE and is_knowledge_id(e.id):
                    raise ValueError(f"a kb: id is reserved for knowledge {where} items")
        basis, host = self.egress_basis, self.model_host
        if basis is None or basis in _NO_HOST_BASES:
            if host is not None:
                raise ValueError("model_host requires an egress basis with a model server")
        elif host is None or not _valid_host(host):
            raise ValueError("model_host must be the model server's host name only")
        elif (basis in _LOOPBACK_BASES) != _is_loopback(host):
            raise ValueError("model_host does not match the egress basis")

        knowledge = {ref: e.id for e in self.evidence if (ref := knowledge_ref(e.id)) is not None}
        if len(knowledge) > MAX_KNOWLEDGE_ITEMS:
            raise ValueError("a record holds at most 20 knowledge items")
        if knowledge and basis is None:
            raise ValueError("a record with knowledge evidence needs an egress basis")
        withheld = self.withheld_knowledge
        if len(withheld) != len(set(withheld)):
            raise ValueError("each withheld knowledge item appears at most once")
        if set(withheld) - knowledge.keys():
            raise ValueError("withheld knowledge must name knowledge evidence in the record")
        if basis is not RecordEgressBasis.DECLARED_LOCAL and knowledge.keys() - set(withheld):
            raise ValueError("knowledge may reach a prompt only under egress basis declared_local")
        held_ids = {knowledge[ref] for ref in withheld if ref in knowledge}
        model_cited = {
            i for f in (*self.findings, *self.cross_examination_findings) for i in f.evidence_ids
        }
        model_cited |= {i for s in self.scenarios for i in s.evidence_ids}
        model_cited |= set(self.proposal_evidence_ids)
        model_cited |= {
            i for t in self.stages if t.name is StageName.REASON for i in t.evidence_ids
        }
        if held_ids & model_cited:
            raise ValueError("withheld knowledge cannot be cited by model output")

    def _check_notes(self) -> None:
        """ADR 0022 decision 14: one note per code; "not configured" and "none returned" mean no
        knowledge in the record; "not configured" stands alone; a superseded version is not one
        whose chunks were used."""
        codes = [n.code for n in self.notes]
        if len(codes) != len(set(codes)):
            raise ValueError("each knowledge note code appears at most once")
        if len(self.superseded_knowledge) != len(set(self.superseded_knowledge)):
            raise ValueError("each superseded knowledge version appears at most once")
        used = {
            KnowledgeVersionRef(document_id=ref.document_id, version=ref.version)
            for e in self.evidence
            if (ref := knowledge_ref(e.id)) is not None
        }
        held = any(is_knowledge_id(e.id) for e in self.withheld_evidence)
        not_configured = KnowledgeNoteCode.NOT_CONFIGURED in codes
        if not_configured and (len(codes) > 1 or self.superseded_knowledge):
            raise ValueError("knowledge_not_configured cannot appear with other knowledge notes")
        if (not_configured or KnowledgeNoteCode.NONE_RETURNED in codes) and (used or held):
            raise ValueError("a note saying no knowledge was returned contradicts kb: evidence")
        if used & set(self.superseded_knowledge):
            raise ValueError("a superseded knowledge version cannot be in the evidence")

    @property
    def original_decider(self) -> str | None:
        """Who the assessment was assigned to when it was created."""
        return self.reassignments[0].from_decider if self.reassignments else self.assigned_decider

    def _check_reassignments(self) -> None:
        """A chain: each move starts where the last ended, in time order, and the current
        assignee is where the last one ended. Nothing moves after the decision."""
        previous_to: str | None = None
        previous_at = self.created_at
        for move in self.reassignments:
            if previous_to is not None and move.from_decider != previous_to:
                raise ValueError("reassignments must form a chain")
            if move.reassigned_at < previous_at:
                raise ValueError("a reassignment is dated before the one before it")
            previous_to, previous_at = move.to_decider, move.reassigned_at
        if previous_to is not None:
            if self.assigned_decider != previous_to:
                raise ValueError("assigned_decider must be where the last reassignment ended")
            if self.human_decision is not None and previous_at > self.human_decision.decided_at:
                raise ValueError("a reassignment is dated after the decision")

    def _check_stages(self) -> None:
        """At most one trace per stage, in graph order, each pointing at the next entry (None
        only on the last). Evidence ids are checked with the other cited ids above, which also
        keeps withheld ids out (they may not appear in the evidence)."""
        names = [t.name for t in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("at most one stage trace per stage")
        positions = [STAGE_ORDER.index(n) for n in names]
        if positions != sorted(positions):
            raise ValueError("stage traces must be in graph order")
        expected = [*(t.name for t in self.stages[1:]), None]
        if [t.next_stage for t in self.stages] != expected[: len(self.stages)]:
            raise ValueError("each stage trace must name the following entry as next_stage")
        by_name = {t.name: t for t in self.stages}
        # ADR 0022: Observe lists the kept non-knowledge ids (cut at the bound, as the pipeline
        # cuts them); Retrieve lists exactly the kept `kb:` ids. A record with `kb:` evidence and
        # a trace must have a Retrieve entry, so no knowledge id goes unlisted.
        kept_ids = [e.id for e in self.evidence]
        knowledge = tuple(i for i in kept_ids if is_knowledge_id(i))
        observe = by_name.get(StageName.OBSERVE)
        if observe is not None:
            live = tuple(i for i in kept_ids if not is_knowledge_id(i))[:MAX_EVIDENCE_ITEMS]
            if observe.evidence_ids != live:
                # The message is unchanged since ADR 0025; tests match on it.
                raise ValueError("the observe trace must list the record's kept evidence ids")
        retrieve = by_name.get(StageName.RETRIEVE)
        if retrieve is not None:
            if retrieve.evidence_ids != knowledge:
                raise ValueError("the retrieve trace must list exactly the record's kb: ids")
        elif knowledge and self.stages:
            raise ValueError("kb: evidence needs a retrieve trace that lists it")
        supervise = by_name.get(StageName.SUPERVISE)
        if supervise is not None:
            tightened = {
                code.partition(":")[2]
                for code in supervise.issues
                if code.startswith("supervise.tightened:")
            }
            failed = set() if self.supervisor_verdict is None else self.supervisor_verdict.failed
            if tightened != {check.value for check in failed}:
                raise ValueError("supervise.tightened codes must match the verdict's failed checks")
        propose = by_name.get(StageName.PROPOSE)
        if propose is not None:
            downgrade_codes = {
                "propose.llm_attempted_downgrade",
                "supervise.llm_attempted_downgrade",
            }
            later = supervise.issues if supervise is not None else ()
            flagged = bool(downgrade_codes & {*propose.issues, *later})
            if flagged != self.llm_attempted_downgrade:
                raise ValueError(
                    "a downgrade code must appear exactly when a downgrade was flagged"
                )

    def _check_supervisor(self) -> None:
        """Every failed check is exactly one `supervisor.<check>` INSUFFICIENT EVIDENCE rule
        result, and no such result exists without its failed check. The verdict's own validator
        already refuses a failed invariant check."""
        supervisor_results = [
            r for r in self.rule_results if r.rule_id.startswith(SUPERVISOR_RULE_PREFIX)
        ]
        failed = () if self.supervisor_verdict is None else self.supervisor_verdict.failed
        expected = {supervisor_rule_id(check) for check in failed}
        found = [r.rule_id for r in supervisor_results]
        if len(found) != len(set(found)):
            raise ValueError("each supervisor rule result must appear once")
        if set(found) != expected:
            raise ValueError("supervisor rule results must match the verdict's failed checks")
        for result in supervisor_results:
            if result.outcome is not Recommendation.INSUFFICIENT_EVIDENCE:
                raise ValueError("a supervisor rule result must be INSUFFICIENT EVIDENCE")
        verdict = self.supervisor_verdict
        tightened = None if verdict is None else verdict.tightened_to
        if tightened is not None and tightened is not self.final_recommendation:
            raise ValueError("tightened_to must be the final recommendation")
