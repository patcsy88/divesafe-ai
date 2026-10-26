"""The lifecycle's fixed vocabulary: stage names and stage outcomes (ADR 0025).

These live in `domain` because the audit record stores them (the supervisor verdict now, the
per-stage trace at ADR 0025 item 3). The graph itself (which stage follows which) is
orchestration's and lives in `divesafe.orchestration.graph`.
"""

from __future__ import annotations

from enum import StrEnum


class StageName(StrEnum):
    """The stages of one assessment, in lifecycle order. Nothing runs after `gate`."""

    OBSERVE = "observe"
    RETRIEVE = "retrieve"
    ASSESS = "assess"
    REASON = "reason"
    REJUDGE = "rejudge"
    PROPOSE = "propose"
    SUPERVISE = "supervise"
    GATE = "gate"


class StageOutcome(StrEnum):
    """How a stage ended. `not_configured` is a stage with nothing to run (no RAG yet)."""

    COMPLETED = "completed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
