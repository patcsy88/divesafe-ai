"""The lifecycle's fixed vocabulary: stage names, outcomes, the per-stage trace and its issue
codes (ADR 0025 items 2 and 3).

These live in `domain` because the audit record stores them. The graph itself (which stage
follows which) is orchestration's (`divesafe.orchestration.graph`), which asserts it matches
`STAGE_ORDER`.

The trace records when a stage ran, how it ended, what evidence it used and which fixed codes
describe its problems. It is not a reasoning trace: no prompts, no model text, no provider error
text, nothing copied from `evidence_issues` or `agent_issues`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from divesafe.domain.base import Frozen


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


# The graph's order, pinned here so the record can validate trace order without importing
# orchestration. `orchestration.graph` refuses to load if `STAGES` disagrees.
STAGE_ORDER: Final[tuple[StageName, ...]] = tuple(StageName)


class StageOutcome(StrEnum):
    """How a stage ended. `not_configured` is a stage with nothing to run (no RAG yet)."""

    COMPLETED = "completed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"


# --- Issue codes -------------------------------------------------------------------------------

# `<stage>.<kind>` or `<stage>.<kind>:<qualifier>`. Qualifiers are code-registered names
# (connector, specialist, scenario kind, supervisor check), never free text.
QUALIFIER_PATTERN: Final = re.compile(r"^[a-z0-9_\-]{1,40}$")
ISSUE_CODE_PATTERN: Final = r"^[a-z]+\.[a-z_]+(:[a-z0-9_\-]{1,40})?$"
MAX_ISSUE_CODE_CHARS: Final = 80
# Recorded instead of a qualifier that does not match `QUALIFIER_PATTERN`. `unnamed_qualifier`
# numbers distinct bad names within one stage so they do not collapse into one code.
UNNAMED_QUALIFIER: Final = "unnamed"
# Written once, in place of the last code, when a stage would exceed `MAX_STAGE_ISSUES`, and
# when a stage's evidence ids were cut at `MAX_EVIDENCE_ITEMS`.
OVERFLOW: Final = "overflow"

# Kinds that take a qualifier are listed with the qualifier's meaning as the value.
TRACE_ISSUE_KINDS: Final[Mapping[StageName, Mapping[str, str | None]]] = {
    StageName.OBSERVE: {
        "connector_failed": "connector",
        "connector_timed_out": "connector",
        "connector_issue": "connector",
        "evidence_conflict": None,
        "no_evidence": None,
    },
    StageName.RETRIEVE: {"not_configured": None},
    StageName.ASSESS: {"connector_issues_rule": None, "unevaluated_factors": None},
    StageName.REASON: {
        "skipped_no_provider": None,
        "timed_out": None,
        "specialist_failed": "specialist",
        # A specialist output the agent layer rejected, with the fixed reason: its shape (schema
        # or oversize) or an evidence id it cites that the call did not see. A specialist task
        # emits at most one of these and `specialist_failed` and `task_skipped`.
        "specialist_schema_rejected": "specialist",
        "specialist_evidence_rejected": "specialist",
        "scenario_failed": "scenario kind",
        "proposal_failed": None,
        # A task that did not run and did not fail (no evidence for the call, or an ablation
        # switch): the qualifier is a specialist name, a scenario kind or `risk_assessment`.
        "task_skipped": "task",
        # Emitted only when cross-examination is on (ADR 0025 item 5). Reason's maximum is
        # 2 outcome codes + 5 specialists + 3 scenario kinds + 1 proposal + 9 task_skipped
        # qualifiers + 1 cross-examination code = 21, which stays within MAX_STAGE_ISSUES (26).
        "cross_examination_on": None,
    },
    StageName.REJUDGE: {"window_started": None, "result_changed": None, "rules_added": None},
    StageName.PROPOSE: {"llm_attempted_downgrade": None},
    # `llm_attempted_downgrade` here: the second `reconcile`, after a tightening, discarded the
    # proposal when the first had not.
    StageName.SUPERVISE: {"tightened": "supervisor check", "llm_attempted_downgrade": None},
    StageName.GATE: {},
}

# Capacity bounds for the issue list, not safety thresholds. `MAX_CONNECTORS` is the number of
# connectors the project can register today (6) rounded up to a power of two; Observe is the
# widest stage (three codes per connector plus two), so the bound covers every stage's maximum
# and `overflow` is only reachable through a future change.
MAX_CONNECTORS: Final = 8
MAX_STAGE_ISSUES: Final = 3 * MAX_CONNECTORS + 2
# Evidence items one agent call may see and evidence ids one trace entry may list. Owned here so
# the agent layer (`agents.runner.MAX_EVIDENCE_ITEMS`) and the record share one number.
MAX_EVIDENCE_ITEMS: Final = 120


def issue_code(stage: StageName, kind: str, qualifier: str | None = None) -> str:
    """One fixed code. `kind` must be in `TRACE_ISSUE_KINDS[stage]` (or `overflow`); a qualifier
    is required exactly when the kind takes one and is replaced by `unnamed` if it does not match
    the qualifier pattern. Never pass free text."""
    kinds = TRACE_ISSUE_KINDS[stage]
    if kind == OVERFLOW:
        if qualifier is not None:
            raise ValueError("overflow takes no qualifier")
        return f"{stage.value}.{OVERFLOW}"
    if kind not in kinds:
        raise ValueError(f"'{kind}' is not an issue kind of stage '{stage.value}'")
    takes_qualifier = kinds[kind] is not None
    if takes_qualifier != (qualifier is not None):
        raise ValueError(f"'{stage.value}.{kind}' qualifier mismatch")
    if qualifier is None:
        return f"{stage.value}.{kind}"
    if not QUALIFIER_PATTERN.fullmatch(qualifier):
        qualifier = UNNAMED_QUALIFIER
    return f"{stage.value}.{kind}:{qualifier}"


def unnamed_qualifier(name: str, unnamed: list[str]) -> str:
    """`name` when it matches the qualifier pattern; otherwise `unnamed_<n>`, n being the
    1-based position of that name among the non-matching names seen so far in one stage
    (`unnamed` is the caller's per-stage list), capped at `MAX_CONNECTORS` so the bound holds."""
    if QUALIFIER_PATTERN.fullmatch(name):
        return name
    if name not in unnamed:
        unnamed.append(name)
    return f"{UNNAMED_QUALIFIER}_{min(unnamed.index(name) + 1, MAX_CONNECTORS)}"


def bound_issues(codes: list[str], stage: StageName) -> tuple[str, ...]:
    """Deduplicate, keep order, and cap at `MAX_STAGE_ISSUES` with `overflow` as the last code."""
    unique = list(dict.fromkeys(codes))
    if len(unique) <= MAX_STAGE_ISSUES:
        return tuple(unique)
    return (*unique[: MAX_STAGE_ISSUES - 1], issue_code(stage, OVERFLOW))


IssueCode = Annotated[
    str, StringConstraints(pattern=ISSUE_CODE_PATTERN, max_length=MAX_ISSUE_CODE_CHARS)
]


class StageTrace(Frozen):
    """One executed stage: name, outcome, where the run went next, timing, the evidence ids it
    used and fixed issue codes.

    `started_at` and `finished_at` are coarse bounds, not per-stage measurements: the run's `now`
    and the one later clock reading taken in Rejudge (equal to `now` with no clock).
    `duration_ms` is the stage's own wall time from a monotonic clock (0 when not measured)."""

    name: StageName
    outcome: StageOutcome
    next_stage: StageName | None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=MAX_EVIDENCE_ITEMS)
    issues: tuple[IssueCode, ...] = Field(default=(), max_length=MAX_STAGE_ISSUES)
    duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _well_formed(self) -> StageTrace:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        if len(set(self.issues)) != len(self.issues):
            raise ValueError("trace issues must be unique")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("trace evidence ids must be unique")
        prefix = f"{self.name.value}."
        kinds = TRACE_ISSUE_KINDS[self.name]
        for code in self.issues:
            if not code.startswith(prefix):
                raise ValueError("a trace issue must carry its own stage's prefix")
            kind, _, qualifier = code.removeprefix(prefix).partition(":")
            if kind == OVERFLOW:
                if qualifier:
                    raise ValueError("overflow takes no qualifier")
                continue
            if kind not in kinds or (kinds[kind] is not None) != bool(qualifier):
                raise ValueError("a trace issue must be one of the fixed codes")
        return self
