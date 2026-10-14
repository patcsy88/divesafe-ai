"""Pending assessment runs (ADR 0019).

A run is stored before it starts and holds only what is needed to join it again: who asked, the
assigned decider, the plan and its lease. It never holds evidence, model text or an error message.
A run is not an assessment: `AssessmentRepository.get` never returns one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, model_validator

from divesafe.domain import AssessmentRecord, DivePlan
from divesafe.domain.base import Frozen


class RunState(StrEnum):
    RUNNING = "RUNNING"
    FAILED = "FAILED"


class FailureCode(StrEnum):
    """Why a run did not complete. Fixed codes only, never exception text."""

    TIMEOUT = "timeout"
    STORAGE = "storage"
    INTERNAL = "internal"
    SHUTDOWN = "shutdown"
    START_PASSED = "start_passed"


class NewRun(Frozen):
    id: str
    created_by: str
    assigned_decider: str
    plan: DivePlan


class PendingRun(Frozen):
    id: str
    created_by: str
    assigned_decider: str
    plan: DivePlan
    created_at: datetime
    state: RunState
    lease_expires_at: datetime
    failed_at: datetime | None = None
    failure_code: FailureCode | None = None

    @model_validator(mode="after")
    def _failed_means_failed(self) -> PendingRun:
        failed = self.state is RunState.FAILED
        if failed != (self.failed_at is not None) or failed != (self.failure_code is not None):
            raise ValueError("a failed run needs a time and a code; a running one has neither")
        return self


class Admission(Frozen):
    """`started` carries the secret token of the new run. `joined` is a run still going, `failed`
    one that ended without a result (terminal for this id), `completed` an id that already has an
    assessment record."""

    kind: Literal["started", "joined", "failed", "completed"]
    run: PendingRun | None = None
    run_token: str | None = Field(default=None, repr=False)


class CleanupResult(Frozen):
    marked_failed: int
    deleted: int


class CapacityError(Exception):
    """Too many runs are in flight. `scope` is `server` or `actor`. Nothing was stored."""

    def __init__(self, scope: Literal["server", "actor"]) -> None:
        super().__init__(scope)
        self.scope = scope


class RunMismatchError(Exception):
    """The id already belongs to a run for a different request."""


class RunStore(Protocol):
    def admit(
        self, run: NewRun, *, max_in_flight: int, max_per_actor: int, lease: timedelta
    ) -> Admission: ...

    def get_run(self, run_id: str) -> PendingRun | None: ...

    def complete(self, record: AssessmentRecord, run_token: str) -> bool: ...

    def fail(self, run_id: str, run_token: str, code: FailureCode) -> bool: ...

    def cleanup(self, *, retention: timedelta, batch: int) -> CleanupResult: ...

    def failure_counts(self) -> dict[str, int]: ...
