"""Human gate: the only way an assessment gets a decision or post-dive conditions.

The override flag is derived from the decision, never supplied by the caller. Records are
immutable: each step returns a new, fully revalidated record, and a recorded decision cannot be
replaced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from divesafe.domain import (
    MAX_REASSIGNMENTS,
    ActualConditions,
    AssessmentRecord,
    HumanDecision,
    Reassignment,
    Recommendation,
)


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


class NotReassignableError(RuntimeError):
    """The assessment cannot be moved (already decided, or the target is the current decider)."""


def reassign(
    record: AssessmentRecord,
    *,
    to_decider: str,
    reassigned_by: str,
    reassigned_at: datetime,
    key_id: str,
) -> AssessmentRecord:
    """Move a still-pending assessment to another decider. The previous assignee loses the right
    to decide at once. The caller has already checked that `to_decider` may decide."""
    if record.human_decision is not None:
        raise NotReassignableError("this assessment already has a decision")
    if record.assigned_decider is None:
        raise NotReassignableError("this assessment has no assigned decider")
    if record.assigned_decider.casefold() == to_decider.strip().casefold():
        raise NotReassignableError("this assessment is already assigned to that decider")
    if len(record.reassignments) >= MAX_REASSIGNMENTS:
        raise NotReassignableError("this assessment has been moved as often as is allowed")
    if record.reassignments and reassigned_at < record.reassignments[-1].reassigned_at:
        raise NotReassignableError("this move is dated before the last one")
    if reassigned_at < record.created_at:
        raise NotReassignableError("this move is dated before the assessment")
    move = Reassignment(
        from_decider=record.assigned_decider,
        to_decider=to_decider,
        reassigned_by=reassigned_by,
        reassigned_at=reassigned_at,
        key_id=key_id,
    )
    return _revalidated(
        record,
        assigned_decider=to_decider,
        reassignments=(*record.reassignments, move),
    )


def decide(
    record: AssessmentRecord,
    *,
    decided_by: str,
    decision: Recommendation,
    decided_at: datetime,
    rationale: str | None = None,
    key_id: str | None = None,
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
        key_id=key_id or None,
        decided_by_creator=(
            None
            if record.created_by is None
            else record.created_by.strip().casefold() == decided_by.strip().casefold()
        ),
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
