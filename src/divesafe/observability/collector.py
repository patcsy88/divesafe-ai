"""Per-run trace collection (ADR 0027 amendment 2026-11-15).

A run's collector lives in a `ContextVar` that `RunManager` sets inside the run's own task, and
only when tracing is enabled. Its default is None, so `assess_dive` called directly collects
nothing. It holds plain values only (never a stage's evidence ids or issue codes) and is handed
to a sink once, after the run has stored its record or been marked failed. Callers append under
`contextlib.suppress(Exception)`: tracing never changes a run. `agents`, `orchestration` and
`api` import this module, so it imports only `domain` and the standard library.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

from divesafe.domain import StageName, StageOutcome, StageTrace

logger = logging.getLogger(__name__)

# One run makes at most 14 model calls (5 specialists, 3 scenarios, the proposal and 5
# cross-examinations) and walks 8 stages; the bound leaves room for both several times over.
MAX_TRACE_ENTRIES = 128

# Fixed reasons for trace data that was dropped. Until ADR 0027 item 2 exists this is a plain
# in-process count with no endpoint; the lock guards only the increment, never I/O or logging.
DropReason = Literal["timeout", "connection", "http_status", "queue_full", "collector_full"]
DROP_REASONS: tuple[DropReason, ...] = (
    "timeout",
    "connection",
    "http_status",
    "queue_full",
    "collector_full",
)
_drops: dict[str, int] = dict.fromkeys(DROP_REASONS, 0)
_drops_lock = threading.Lock()


def count_drop(reason: DropReason) -> None:
    """Count one drop. The first drop of each reason is logged once, as the code only."""
    if reason not in _drops:
        raise ValueError("unknown drop reason")
    with _drops_lock:
        _drops[reason] += 1
        first = _drops[reason] == 1
    if first:
        logger.warning("trace data dropped", extra={"reason": reason})


def drop_counts() -> dict[str, int]:
    with _drops_lock:
        return dict(_drops)


@dataclass(frozen=True, slots=True)
class ModelCallEntry:
    """The "model call finished" fields (the log line's own dict) and the call's wall-clock
    start, for the span only."""

    fields: Mapping[str, object]
    started_ns: int


@dataclass(frozen=True, slots=True)
class StageEntry:
    """The exportable part of a `StageTrace`: never its evidence ids or issue codes. `ended_ns` is
    the wall clock when the stage's trace was appended, for the span only (the record's times
    are coarse run-clock bounds)."""

    name: StageName
    outcome: StageOutcome
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    ended_ns: int


@dataclass(frozen=True, slots=True)
class RunTrace:
    """What one run hands to the sink. `run_outcome` is `stored` or a `FailureCode` value.
    `request_id` is the id of the request that started the run (ADR 0027 item 1), or None."""

    assessment_id: str
    run_outcome: str
    started_ns: int
    finished_ns: int
    model_calls: tuple[ModelCallEntry, ...]
    stages: tuple[StageEntry, ...]
    request_id: str | None = None


class TraceSink(Protocol):
    """Receives a finished run's trace, synchronously and once. Must not block the event loop."""

    def emit(self, trace: RunTrace) -> None: ...


class RunCollector:
    def __init__(self) -> None:
        self._limit = MAX_TRACE_ENTRIES
        self._started_ns = time.time_ns()
        self._calls: list[ModelCallEntry] = []
        self._stages: list[StageEntry] = []

    def _has_room(self) -> bool:
        if len(self._calls) + len(self._stages) < self._limit:
            return True
        count_drop("collector_full")
        return False

    def add_model_call(self, fields: Mapping[str, object], started_ns: int) -> None:
        if self._has_room():
            self._calls.append(ModelCallEntry(MappingProxyType(dict(fields)), started_ns))

    def add_stage(self, trace: StageTrace) -> None:
        if self._has_room():
            self._stages.append(
                StageEntry(
                    trace.name,
                    trace.outcome,
                    trace.started_at,
                    trace.finished_at,
                    trace.duration_ms,
                    time.time_ns(),
                )
            )

    def finish(
        self, assessment_id: str, run_outcome: str, *, request_id: str | None = None
    ) -> RunTrace:
        return RunTrace(
            assessment_id=assessment_id,
            run_outcome=run_outcome,
            request_id=request_id,
            started_ns=self._started_ns,
            finished_ns=time.time_ns(),
            model_calls=tuple(self._calls),
            stages=tuple(self._stages),
        )


_CURRENT: ContextVar[RunCollector | None] = ContextVar("divesafe_run_collector", default=None)


def start_collecting() -> RunCollector:
    """A new collector for the calling task's context; `RunManager` calls it in the run's own
    task. Child tasks copy the context and share it."""
    collector = RunCollector()
    _CURRENT.set(collector)
    return collector


def current_collector() -> RunCollector | None:
    return _CURRENT.get()


def record_model_call(fields: Mapping[str, object], started_ns: int) -> None:
    """Append to the current run's collector, if any. May raise: callers suppress."""
    collector = _CURRENT.get()
    if collector is not None:
        collector.add_model_call(fields, started_ns)


def record_stage(trace: StageTrace) -> None:
    """Append to the current run's collector, if any. May raise: callers suppress."""
    collector = _CURRENT.get()
    if collector is not None:
        collector.add_stage(trace)
