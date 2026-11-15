"""Run tracing that never changes a result (ADR 0027 amendment 2026-11-15)."""

from divesafe.observability.collector import (
    DROP_REASONS,
    MAX_TRACE_ENTRIES,
    DropReason,
    ModelCallEntry,
    RunCollector,
    RunTrace,
    StageEntry,
    TraceSink,
    count_drop,
    current_collector,
    drop_counts,
    record_model_call,
    record_stage,
    start_collecting,
)

__all__ = [
    "DROP_REASONS",
    "MAX_TRACE_ENTRIES",
    "DropReason",
    "ModelCallEntry",
    "RunCollector",
    "RunTrace",
    "StageEntry",
    "TraceSink",
    "count_drop",
    "current_collector",
    "drop_counts",
    "record_model_call",
    "record_stage",
    "start_collecting",
]
