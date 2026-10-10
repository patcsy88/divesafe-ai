"""Human gate: the only way an assessment gets a decision or post-dive conditions.

The override flag is derived from the decision, never supplied by the caller. Records are
immutable: each step returns a new, fully revalidated record, and a recorded decision cannot be
replaced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from divesafe.domain import ActualConditions, AssessmentRecord, HumanDecision, Recommendation


class AlreadyDecidedError(RuntimeError):
    """The record already holds a human decision (or actual conditions); it is not replaced."""


class NotAssignedDeciderError(PermissionError):
    """Only the decider assigned to an assessment may record its decision or actual conditions."""


def check_assigned(record: AssessmentRecord, actor: str) -> None:
    assigned = record.assigned_decider
    if assigned is None or assigned.casefold() != actor.strip().casefold():
        raise NotAssignedDeciderError("this assessment is assigned to another decider")


class DecisionRequiredError(RuntimeError):
    """Actual conditions need a recorded human decision first."""


def _revalidated(record: AssessmentRecord, **changes: Any) -> AssessmentRecord:
    return AssessmentRecord.model_validate({**record.model_dump(), **changes})


def decide(
    record: AssessmentRecord,
    *,
    decided_by: str,
    decision: Recommendation,
    decided_at: datetime,
    rationale: str | None = None,
) -> AssessmentRecord:
    check_assigned(record, decided_by)
    if record.human_decision is not None:
        raise AlreadyDecidedError("this assessment already has a human decision")
    human = HumanDecision(
        decided_by=decided_by,
        decision=decision,
        decided_at=decided_at,
        is_override=decision != record.final_recommendation,
        override_rationale=rationale,
    )
    return _revalidated(record, human_decision=human.model_dump())


def report_actual_conditions(
    record: AssessmentRecord, *, actual: ActualConditions
) -> AssessmentRecord:
    check_assigned(record, actual.reported_by)
    if record.human_decision is None:
        raise DecisionRequiredError("actual conditions require a recorded human decision")
    if record.actual_conditions is not None:
        raise AlreadyDecidedError("actual conditions were already reported")
    return _revalidated(record, actual_conditions=actual.model_dump())
