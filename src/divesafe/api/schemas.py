"""Request and response bodies. Requests forbid unknown fields, so `decided_by` and similar
identity fields can never be supplied by a client."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from divesafe.domain import AssessmentRecord, Recommendation

DISCLAIMER = (
    "Decision support only. The final decision belongs to the diver, dive master or dive leader. "
    "This is not a safety authority and does not replace training, briefings or local knowledge."
)


_OUTCOME_NOTES: dict[Recommendation, str] = {
    Recommendation.GO: "No hard constraint applied, but this is not a guarantee of safety.",
    Recommendation.CAUTION: "Conditions need careful human review.",
    Recommendation.NO_GO: "A hard constraint applies. A human may override only with a rationale.",
    Recommendation.INSUFFICIENT_EVIDENCE: (
        "INSUFFICIENT EVIDENCE is not a green light: the system cannot assess this dive. "
        "Do not treat it as safe."
    ),
}


_MAX_DEPTH = 6
_MAX_NODES = 500


def _shape_exceeds(root: Any, *, max_depth: int, max_nodes: int) -> bool:
    """Iterative walk, so hostile nesting cannot cause recursion errors."""
    stack: list[tuple[Any, int]] = [(root, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return True
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list | tuple):
            stack.extend((v, depth + 1) for v in item)
    return False


def _agent_note(record: AssessmentRecord) -> str:
    if record.model_version is None:
        if record.explanation or record.scenarios or record.proposed_recommendation is not None:
            return "Agent output of unknown origin was attached; treat it as unverified."
        return "No LLM agents were used for this assessment."
    note = (
        "Findings, scenarios and the explanation are LLM-generated summaries of the evidence. "
        "They can add caution but never relax a rule result, and they may be wrong."
    )
    if record.agent_issues:
        note += f" {len(record.agent_issues)} agent call(s) failed or were rejected."
        names = sorted(
            {i.split(":")[1] for i in record.agent_issues if i.startswith("specialist:")}
        )
        if names:
            note += f" Missing specialist findings: {', '.join(names)}."
    return note


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateAssessmentRequest(_Request):
    site_id: str = Field(min_length=1, max_length=100)
    planned_start: AwareDatetime
    planned_duration_minutes: int = Field(gt=0, le=24 * 60)
    max_depth_m: float = Field(gt=0, le=1000)


class DecisionRequest(_Request):
    decision: Recommendation
    rationale: str | None = Field(default=None, max_length=5000)


class ActualConditionsRequest(_Request):
    observations: dict[str, Any]

    @field_validator("observations")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _shape_exceeds(value, max_depth=_MAX_DEPTH, max_nodes=_MAX_NODES):
            raise ValueError("observations are too deeply nested or too large")
        try:
            size = len(json.dumps(value, default=str))
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError("observations are not simple JSON") from exc
        if size > 20_000:
            raise ValueError("observations are too large")
        return value


class AssessmentView(BaseModel):
    record: AssessmentRecord
    status: Literal["PENDING_HUMAN", "DECIDED"]
    overrides_to_less_severe: bool
    llm_tightened: bool
    confidence_note: str
    outcome_note: str
    status_note: str
    agent_note: str
    attributions: tuple[str, ...]
    notice: str = DISCLAIMER

    @classmethod
    def of(cls, record: AssessmentRecord) -> AssessmentView:
        attributions = sorted(
            {str(e.value["attribution"]) for e in record.evidence if "attribution" in e.value}
        )
        missing = sorted(
            r.rule_id.removeprefix("evidence.required.")
            for r in record.rule_results
            if r.rule_id.startswith("evidence.required.")
        )
        outcome_note = _OUTCOME_NOTES[record.final_recommendation]
        if record.llm_tightened:
            outcome_note = (
                f"Rules alone: {record.deterministic_recommendation.value}. An unverified LLM "
                f"proposal raised this to {record.final_recommendation.value}. "
            ) + outcome_note
        if missing:
            outcome_note += f" Missing or stale evidence: {', '.join(missing)}."
        if record.unevaluated_factors:
            names = ", ".join(k.value for k in record.unevaluated_factors)
            outcome_note += f" Not evaluated (no validated threshold): {names}."
        if record.evidence_issues:
            outcome_note += (
                f" {len(record.evidence_issues)} source issue(s) are listed in the record."
            )
        if record.ruleset_version.startswith("interim"):
            outcome_note += (
                " The ruleset is interim and unreviewed: its limits are placeholders, so no dive "
                "can currently return GO. More data alone will not change that."
            )
        return cls(
            record=record,
            outcome_note=outcome_note,
            agent_note=_agent_note(record),
            status_note=(
                "Awaiting a human decision. Do not act on this result yet."
                if record.status == "PENDING_HUMAN"
                else (
                    "A human decision has been recorded. "
                    "It does not change the system's recommendation."
                )
            ),
            status=record.status,
            overrides_to_less_severe=record.overrides_to_less_severe,
            llm_tightened=record.llm_tightened,
            confidence_note=(
                "Confidence was not computed (no risk model yet). Do not read this as high or low."
                if record.confidence is None
                else "Confidence describes the reliability of this assessment, not dive safety."
            ),
            attributions=tuple(attributions),
        )
