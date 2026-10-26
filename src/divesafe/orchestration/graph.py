"""The lifecycle as data (ADR 0025 item 2).

`STAGES` is the single description of which stage runs after which, what each consumes and
produces, and which outcomes let the run continue. `assess_dive` walks it; tests assert its
properties on the data (no path reaches the gate without the engine, `reconcile` alone produces
`final_recommendation`, every input is produced earlier) without running anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from divesafe.domain import STAGE_ORDER, StageName, StageOutcome

Router = Callable[[StageOutcome], StageName | None]


@dataclass(frozen=True)
class Stage:
    name: StageName
    requires: tuple[str, ...]
    produces: tuple[str, ...]
    route: Router
    timeout_seconds: float | None = None


def _always(next_stage: StageName) -> Router:
    """Proceed whatever happened: the stage's failure is recorded and the run goes on."""

    def route(outcome: StageOutcome) -> StageName | None:
        return next_stage

    return route


def _only_completed(next_stage: StageName | None) -> Router:
    """Proceed only on `completed`; any other outcome ends the run with no record."""

    def route(outcome: StageOutcome) -> StageName | None:
        return next_stage if outcome is StageOutcome.COMPLETED else None

    return route


def _terminal(outcome: StageOutcome) -> StageName | None:
    return None


STAGES: Final[tuple[Stage, ...]] = (
    # Connectors. Failed sources become issues, never an abort, so every outcome proceeds.
    Stage(
        name=StageName.OBSERVE,
        requires=("plan", "site"),
        produces=("evidence",),
        route=_always(StageName.RETRIEVE),
    ),
    # Placeholder until RAG stage 1 (ADR 0022): ends `not_configured` and still proceeds.
    Stage(
        name=StageName.RETRIEVE,
        requires=("plan", "site"),
        produces=(),
        route=_always(StageName.ASSESS),
    ),
    # The deterministic engine, plus the connector-issue rule result.
    Stage(
        name=StageName.ASSESS,
        requires=("plan", "evidence"),
        produces=("rule_results", "deterministic"),
        route=_only_completed(StageName.REASON),
    ),
    # The agent stage. It degrades to "no proposal" on any failure and the run goes on.
    Stage(
        name=StageName.REASON,
        requires=("plan", "evidence", "rule_results", "deterministic"),
        produces=("proposed", "scenarios", "explanation", "model_version", "findings"),
        route=_always(StageName.REJUDGE),
    ),
    # The engine again at the later clock; results are only ever added.
    Stage(
        name=StageName.REJUDGE,
        requires=("plan", "evidence", "rule_results", "deterministic"),
        produces=("rule_results", "deterministic"),
        route=_only_completed(StageName.PROPOSE),
    ),
    # `reconcile`: the only producer of `final_recommendation`.
    Stage(
        name=StageName.PROPOSE,
        requires=("deterministic", "proposed"),
        produces=("final_recommendation",),
        route=_only_completed(StageName.SUPERVISE),
    ),
    # Code-only checks. A tightening check appends a rule result and `reconcile` runs again
    # inside this stage; it never produces `final_recommendation` on its own.
    Stage(
        name=StageName.SUPERVISE,
        requires=("final_recommendation", "rule_results", "deterministic", "proposed", "evidence"),
        produces=("supervisor_verdict",),
        route=_only_completed(StageName.GATE),
    ),
    # The record is PENDING_HUMAN. Nothing runs after it.
    Stage(
        name=StageName.GATE,
        requires=("final_recommendation", "supervisor_verdict"),
        produces=("record",),
        route=_terminal,
    ),
)

# The record validates trace order against `domain.STAGE_ORDER`; the graph must agree with it.
if tuple(stage.name for stage in STAGES) != STAGE_ORDER:
    raise RuntimeError("the stage graph does not follow domain.STAGE_ORDER")
_BY_NAME: Final[Mapping[StageName, Stage]] = {stage.name: stage for stage in STAGES}
if len(_BY_NAME) != len(STAGES) or len(_BY_NAME) != len(StageName):
    raise RuntimeError("STAGES must hold exactly one stage per StageName")


def stage(name: StageName) -> Stage:
    return _BY_NAME[name]


def next_stage(name: StageName, outcome: StageOutcome) -> StageName | None:
    """The stage that follows `name` after `outcome`, or None when the run stops there."""
    return _BY_NAME[name].route(outcome)


def walk(outcomes: Mapping[StageName, StageOutcome]) -> tuple[StageName, ...]:
    """The path through the graph given each stage's outcome, starting at the first stage.

    Pure: nothing runs. A stage missing from `outcomes` is taken as `completed`. The path ends
    at the first stage whose route returns None, so a failed engine stage gives a path that
    stops short of `gate`.
    """
    path: list[StageName] = []
    current: StageName | None = STAGES[0].name
    while current is not None:
        if current in path:  # the graph is linear; guard a future edit that adds a cycle
            raise RuntimeError("the stage graph has a cycle")
        path.append(current)
        current = next_stage(current, outcomes.get(current, StageOutcome.COMPLETED))
    return tuple(path)
