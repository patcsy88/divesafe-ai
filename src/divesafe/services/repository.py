"""Assessment storage contract with write-once, append-only semantics.

Every adapter (in-memory and PostgreSQL) must pass tests/unit/test_repository_contract.py.
The only permitted change to a stored record is attaching a human decision once and then
actual conditions once; nothing else may differ. A new record cannot already carry a
decision. `replace` is compare-and-set, so two concurrent writers cannot both succeed.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from divesafe.domain import AssessmentRecord
from divesafe.services.runs import (
    Admission,
    CapacityError,
    CleanupResult,
    FailureCode,
    NewRun,
    PendingRun,
    RunMismatchError,
    RunState,
)


class RecordExistsError(Exception):
    """A record with this id already exists."""


class RecordNotFoundError(Exception):
    """No record has this id."""


class ConflictError(Exception):
    """The stored record is not the one the caller read (concurrent change)."""


class InvalidSuccessorError(ValueError):
    """The new record changes more than the permitted append-only fields."""


class RepositoryUnavailableError(Exception):
    """The store could not be reached or failed. Carries no connection details."""


class RepositoryIntegrityError(Exception):
    """A stored record could not be read back as a valid record."""


class UnstorableRecordError(ValueError):
    """The record cannot be stored faithfully (for example NUL characters, non-finite numbers,
    or a value that would not read back identical). Nothing was stored."""


class AssessmentRepository(Protocol):
    def create(self, record: AssessmentRecord) -> None: ...

    def get(self, assessment_id: str) -> AssessmentRecord | None: ...

    def replace(self, expected: AssessmentRecord, new: AssessmentRecord) -> None: ...


_APPEND_ONLY = ("human_decision", "actual_conditions")


def check_successor(old: AssessmentRecord, new: AssessmentRecord) -> None:
    """Raise unless `new` only appends the human decision and/or actual conditions."""
    try:
        AssessmentRecord.model_validate(new.model_dump())  # reject unvalidated constructions
    except ValueError as exc:
        raise InvalidSuccessorError("successor record is not valid") from exc
    before, after = old.model_dump(), new.model_dump()
    for key, value in before.items():
        if key not in _APPEND_ONLY and value != after[key]:
            raise InvalidSuccessorError(f"field '{key}' may not change")
    for field in _APPEND_ONLY:
        existing = getattr(old, field)
        if existing is not None and existing != getattr(new, field):
            raise InvalidSuccessorError(f"{field} is write-once")
    if old.human_decision is None and new.actual_conditions is not None:
        raise InvalidSuccessorError("actual conditions require a decision")


class InMemoryAssessmentRepository:
    """For tests and local development. Not durable."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[str, AssessmentRecord] = {}
        self._lock = threading.Lock()
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._runs: dict[str, PendingRun] = {}
        self._tokens: dict[str, str] = {}
        self._failure_counts: dict[str, int] = {}

    def create(self, record: AssessmentRecord) -> None:
        if record.human_decision is not None or record.actual_conditions is not None:
            raise InvalidSuccessorError("a new record cannot already carry a decision")
        with self._lock:
            if record.id in self._records:
                raise RecordExistsError(record.id)
            self._records[record.id] = record

    def get(self, assessment_id: str) -> AssessmentRecord | None:
        with self._lock:
            return self._records.get(assessment_id)

    def replace(self, expected: AssessmentRecord, new: AssessmentRecord) -> None:
        if expected.id != new.id:
            raise InvalidSuccessorError("id may not change")
        with self._lock:
            stored = self._records.get(expected.id)
            if stored is None:
                raise RecordNotFoundError(expected.id)
            if stored != expected:
                raise ConflictError(expected.id)
            check_successor(expected, new)
            self._records[new.id] = new

    # --- pending runs (ADR 0019); one lock with the records, so completion is one step --------

    def admit(
        self, run: NewRun, *, max_in_flight: int, max_per_actor: int, lease: timedelta
    ) -> Admission:
        with self._lock:
            if run.id in self._records:
                return Admission(kind="completed")
            existing = self._runs.get(run.id)
            if existing is not None:
                if (
                    existing.plan != run.plan
                    or existing.assigned_decider != run.assigned_decider
                    or existing.created_by != run.created_by
                ):
                    raise RunMismatchError(run.id)
                if (
                    existing.state is RunState.RUNNING
                    and existing.lease_expires_at <= self._clock()
                ):
                    existing = self._mark_failed(existing, FailureCode.TIMEOUT)
                kind = "joined" if existing.state is RunState.RUNNING else "failed"
                return Admission(kind=kind, run=existing)
            now = self._clock()
            live = [
                r
                for r in self._runs.values()
                if r.state is RunState.RUNNING and r.lease_expires_at > now
            ]
            if len(live) >= max_in_flight:
                raise CapacityError("server")
            if sum(1 for r in live if r.created_by == run.created_by) >= max_per_actor:
                raise CapacityError("actor")
            token = secrets.token_urlsafe(32)
            pending = PendingRun(
                id=run.id,
                created_by=run.created_by,
                assigned_decider=run.assigned_decider,
                plan=run.plan,
                created_at=now,
                state=RunState.RUNNING,
                lease_expires_at=now + lease,
            )
            self._runs[run.id] = pending
            self._tokens[run.id] = hashlib.sha256(token.encode()).hexdigest()
            return Admission(kind="started", run=pending, run_token=token)

    def get_run(self, run_id: str) -> PendingRun | None:
        with self._lock:
            run = self._runs.get(run_id)
            if (
                run is not None
                and run.state is RunState.RUNNING
                and run.lease_expires_at <= self._clock()
            ):
                run = self._mark_failed(run, FailureCode.TIMEOUT)
            return run

    def complete(self, record: AssessmentRecord, run_token: str) -> bool:
        """Store the record and drop the run, only if this run still holds a live lease."""
        if record.human_decision is not None or record.actual_conditions is not None:
            raise InvalidSuccessorError("a new record cannot already carry a decision")
        with self._lock:
            run = self._runs.get(record.id)
            if (
                run is None
                or run.state is not RunState.RUNNING
                or run.lease_expires_at <= self._clock()
                or not secrets.compare_digest(
                    self._tokens[record.id], hashlib.sha256(run_token.encode()).hexdigest()
                )
                or record.id in self._records
            ):
                return False
            self._records[record.id] = record
            del self._runs[record.id]
            del self._tokens[record.id]
            return True

    def fail(self, run_id: str, run_token: str, code: FailureCode) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if (
                run is None
                or run.state is not RunState.RUNNING
                or not secrets.compare_digest(
                    self._tokens[run_id], hashlib.sha256(run_token.encode()).hexdigest()
                )
            ):
                return False
            self._mark_failed(run, code)
            return True

    def cleanup(self, *, retention: timedelta, batch: int) -> CleanupResult:
        with self._lock:
            now = self._clock()
            expired = [
                r
                for r in self._runs.values()
                if r.state is RunState.RUNNING and r.lease_expires_at <= now
            ]
            for run in expired[:batch]:
                self._mark_failed(run, FailureCode.TIMEOUT)
            old = sorted(
                (
                    r
                    for r in self._runs.values()
                    if r.state is RunState.FAILED
                    and r.failed_at is not None
                    and r.failed_at <= now - retention
                ),
                key=lambda r: r.failed_at or now,
            )[:batch]
            for run in old:
                code = run.failure_code.value if run.failure_code else "internal"
                self._failure_counts[code] = self._failure_counts.get(code, 0) + 1
                del self._runs[run.id]
                del self._tokens[run.id]
            return CleanupResult(marked_failed=min(len(expired), batch), deleted=len(old))

    def failure_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._failure_counts)

    def _mark_failed(self, run: PendingRun, code: FailureCode) -> PendingRun:
        failed = run.model_copy(
            update={"state": RunState.FAILED, "failed_at": self._clock(), "failure_code": code}
        )
        self._runs[run.id] = failed
        return failed
