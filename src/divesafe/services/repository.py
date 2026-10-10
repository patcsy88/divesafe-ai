"""Assessment storage contract with write-once, append-only semantics.

Every adapter (in-memory and PostgreSQL) must pass tests/unit/test_repository_contract.py.
The only permitted change to a stored record is attaching a human decision once and then
actual conditions once; nothing else may differ. A new record cannot already carry a
decision. `replace` is compare-and-set, so two concurrent writers cannot both succeed.
"""

from __future__ import annotations

import threading
from typing import Protocol

from divesafe.domain import AssessmentRecord


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

    def __init__(self) -> None:
        self._records: dict[str, AssessmentRecord] = {}
        self._lock = threading.Lock()

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
