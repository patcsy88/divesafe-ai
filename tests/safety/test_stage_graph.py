"""The lifecycle stage graph (ADR 0025 item 2): properties asserted on the DATA in
`orchestration.graph.STAGES`, not on a run. Nothing here fetches, judges or calls a model.

What the graph must guarantee:
  - the order is the ADR's: observe, retrieve, assess, reason, rejudge, propose, supervise, gate;
  - no path reaches `gate` without `assess`, `rejudge`, `propose` and `supervise`;
  - those four stages continue only on `completed` (any other outcome stops the run, no record);
  - `reason` and `retrieve` never stop the run (a failed agent stage is "no proposal");
  - `propose` is the only producer of `final_recommendation`; `supervise` directly follows it;
  - every input a stage requires is an initial input (plan, site) or produced earlier.

The last tests prove `assess_dive` really walks this graph (`next_stage`, `STAGE_WORK`) rather
than a hard-coded sequence, so the properties above apply to the pipeline.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest
from tests.agent import scripted_llm as sl

from divesafe.domain import StageName, StageOutcome
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.orchestration.graph import STAGES, next_stage, stage, walk

pytestmark = pytest.mark.safety

N = StageName
O = StageOutcome  # noqa: E741

ADR_ORDER = (N.OBSERVE, N.RETRIEVE, N.ASSESS, N.REASON, N.REJUDGE, N.PROPOSE, N.SUPERVISE, N.GATE)
ENGINE_STAGES = (N.ASSESS, N.REJUDGE, N.PROPOSE, N.SUPERVISE)
INITIAL_INPUTS = {"plan", "site"}
NOT_COMPLETED = tuple(o for o in O if o is not O.COMPLETED)
ALL_COMPLETED = dict.fromkeys(N, O.COMPLETED)


def every_outcome_assignment() -> list[dict[StageName, StageOutcome]]:
    """Every combination of one outcome per stage (5^8 = 390,625; `walk` is pure and fast)."""
    return [dict(zip(N, combo, strict=True)) for combo in itertools.product(list(O), repeat=len(N))]


# --- order and inputs ---------------------------------------------------------------------------


def test_stages_are_in_the_adr_order_and_cover_every_stage_name_once() -> None:
    assert tuple(s.name for s in STAGES) == ADR_ORDER
    assert tuple(N) == ADR_ORDER  # the stored vocabulary keeps the same order


def test_every_required_input_is_initial_or_produced_by_an_earlier_stage() -> None:
    available = set(INITIAL_INPUTS)
    for s in STAGES:
        missing = set(s.requires) - available
        assert not missing, f"{s.name.value} requires {sorted(missing)} before anything produces it"
        available |= set(s.produces)


def test_reason_requires_the_engines_output() -> None:
    assert {"rule_results", "deterministic"} <= set(stage(N.REASON).requires)
    assert {"rule_results", "deterministic"} <= set(stage(N.ASSESS).produces)


def test_the_gate_requires_the_final_recommendation_and_the_verdict() -> None:
    assert {"final_recommendation", "supervisor_verdict"} <= set(stage(N.GATE).requires)
    assert stage(N.GATE).produces == ("record",)


# --- routing ------------------------------------------------------------------------------------


def test_walk_with_every_stage_completed_visits_every_stage_and_ends_at_the_gate() -> None:
    path = walk(ALL_COMPLETED)
    assert path == ADR_ORDER
    assert path[-1] is N.GATE
    assert walk({}) == path  # a stage missing from the outcomes counts as completed


def test_nothing_runs_after_the_gate() -> None:
    assert all(next_stage(N.GATE, outcome) is None for outcome in O)


@pytest.mark.parametrize("name", ENGINE_STAGES)
@pytest.mark.parametrize("outcome", NOT_COMPLETED)
def test_an_engine_stage_that_did_not_complete_stops_the_run(
    name: StageName, outcome: StageOutcome
) -> None:
    assert next_stage(name, outcome) is None
    path = walk({name: outcome})
    assert path[-1] is name
    assert N.GATE not in path


@pytest.mark.parametrize("name", ENGINE_STAGES)
def test_an_engine_stage_that_completed_continues(name: StageName) -> None:
    assert next_stage(name, O.COMPLETED) is not None


@pytest.mark.parametrize("name", (N.OBSERVE, N.RETRIEVE, N.REASON))
@pytest.mark.parametrize("outcome", list(O))
def test_observe_retrieve_and_reason_continue_whatever_happened(
    name: StageName, outcome: StageOutcome
) -> None:
    assert next_stage(name, outcome) is next_stage(name, O.COMPLETED)
    assert next_stage(name, outcome) is not None
    assert N.GATE in walk({name: outcome})


def test_no_path_reaches_the_gate_without_assess_rejudge_propose_and_supervise() -> None:
    reached_gate = 0
    for outcomes in every_outcome_assignment():
        path = walk(outcomes)
        if N.GATE in path:
            reached_gate += 1
            assert set(ENGINE_STAGES) <= set(path)
            assert all(outcomes[name] is O.COMPLETED for name in ENGINE_STAGES)
        else:
            # The path stopped at an engine stage that did not complete.
            assert path[-1] in ENGINE_STAGES
            assert outcomes[path[-1]] is not O.COMPLETED
    # Control: exactly the assignments with all four engine stages completed reach the gate.
    assert reached_gate == len(O) ** (len(N) - len(ENGINE_STAGES))


def test_every_path_is_a_prefix_of_the_adr_order() -> None:
    for outcomes in every_outcome_assignment():
        path = walk(outcomes)
        assert path == ADR_ORDER[: len(path)]


def test_supervise_always_directly_follows_propose() -> None:
    assert next_stage(N.PROPOSE, O.COMPLETED) is N.SUPERVISE
    for outcomes in every_outcome_assignment():
        path = walk(outcomes)
        if N.SUPERVISE in path:
            assert path[path.index(N.SUPERVISE) - 1] is N.PROPOSE
        if N.PROPOSE in path and outcomes[N.PROPOSE] is O.COMPLETED:
            assert path[path.index(N.PROPOSE) + 1] is N.SUPERVISE


def test_propose_is_the_only_producer_of_final_recommendation() -> None:
    producers = [s.name for s in STAGES if "final_recommendation" in s.produces]
    assert producers == [N.PROPOSE]
    assert "final_recommendation" in stage(N.SUPERVISE).requires  # consumed, never produced
    assert stage(N.SUPERVISE).produces == ("supervisor_verdict",)


# --- the pipeline walks this graph --------------------------------------------------------------


def test_stage_work_keys_equal_the_graph_stage_names() -> None:
    assert set(pipeline_module.STAGE_WORK) == {s.name for s in STAGES}
    assert set(pipeline_module.STAGE_WORK) == set(N)


def test_assess_dive_routes_through_next_stage_and_runs_the_work_in_graph_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed: list[tuple[StageName, StageOutcome, StageName | None]] = []
    ran: list[StageName] = []

    def recording_next(name: StageName, outcome: StageOutcome) -> StageName | None:
        following = next_stage(name, outcome)
        routed.append((name, outcome, following))
        return following

    monkeypatch.setattr(pipeline_module, "next_stage", recording_next)
    for name, work in list(pipeline_module.STAGE_WORK.items()):

        def wrapped(run: Any, _name: StageName = name, _work: Any = work) -> Any:
            ran.append(_name)
            return _work(run)

        monkeypatch.setitem(pipeline_module.STAGE_WORK, name, wrapped)

    record = sl.run_pipeline(sl.fake())

    assert ran == list(ADR_ORDER)
    assert [name for name, _, _ in routed] == list(ADR_ORDER)
    assert routed[-1] == (N.GATE, O.COMPLETED, None)
    assert routed[1][1] is O.NOT_CONFIGURED  # retrieve is the RAG placeholder
    assert [following for _, _, following in routed] == [*ADR_ORDER[1:], None]
    assert record.supervisor_verdict is not None


def test_assess_dive_stops_with_no_record_when_the_graph_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route, not the work table, decides: a graph that ends after `assess` yields nothing."""
    ran: list[StageName] = []

    def stop_after_assess(name: StageName, outcome: StageOutcome) -> StageName | None:
        return None if name is N.ASSESS else next_stage(name, outcome)

    monkeypatch.setattr(pipeline_module, "next_stage", stop_after_assess)
    for name, work in list(pipeline_module.STAGE_WORK.items()):

        def wrapped(run: Any, _name: StageName = name, _work: Any = work) -> Any:
            ran.append(_name)
            return _work(run)

        monkeypatch.setitem(pipeline_module.STAGE_WORK, name, wrapped)

    with pytest.raises(RuntimeError, match="stopped before the human gate"):
        sl.run_pipeline(sl.fake())
    assert ran == [N.OBSERVE, N.RETRIEVE, N.ASSESS]


def test_an_engine_stage_that_does_not_complete_yields_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graph's rule applied by the real pipeline: `assess` ending other than `completed`
    stops the run before the gate, so no record is built from a half-judged plan."""

    async def failed_assess(run: Any) -> StageOutcome:
        return O.FAILED

    monkeypatch.setitem(pipeline_module.STAGE_WORK, N.ASSESS, failed_assess)
    with pytest.raises(RuntimeError, match="stopped before the human gate"):
        asyncio.run(
            pipeline_module.assess_dive(
                assessment_id="g-1",
                plan=sl.PLAN,
                site=sl.TIOMAN_ISLAND,
                connectors=[],
                engine=sl.engine(with_warning_rule=False),
                now=sl.NOW,
            )
        )


# --- the walk refuses a cycle and labels a timed-out agent stage --------------------------------

REVISIT_MESSAGE = "the stage graph revisited a stage; the assessment was stopped"


def _record_work(monkeypatch: pytest.MonkeyPatch) -> list[StageName]:
    ran: list[StageName] = []
    for name, work in list(pipeline_module.STAGE_WORK.items()):

        def wrapped(run: Any, _name: StageName = name, _work: Any = work) -> Any:
            ran.append(_name)
            return _work(run)

        monkeypatch.setitem(pipeline_module.STAGE_WORK, name, wrapped)
    return ran


def test_a_route_back_to_an_earlier_stage_is_refused_with_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A graph edit that routes supervise -> propose would re-run `reconcile` forever; the walk
    stops at the first revisit with a fixed message, builds no record and never reaches the gate."""
    gate_ran: list[bool] = []
    original_gate = pipeline_module.STAGE_WORK[N.GATE]

    def cycle(name: StageName, outcome: StageOutcome) -> StageName | None:
        return N.PROPOSE if name is N.SUPERVISE else next_stage(name, outcome)

    async def gate(run: Any) -> Any:
        gate_ran.append(True)
        return await original_gate(run)

    monkeypatch.setattr(pipeline_module, "next_stage", cycle)
    monkeypatch.setitem(pipeline_module.STAGE_WORK, N.GATE, gate)
    ran = _record_work(monkeypatch)

    with pytest.raises(RuntimeError) as caught:
        sl.run_pipeline(sl.fake())
    assert str(caught.value) == REVISIT_MESSAGE
    assert gate_ran == []
    assert ran == [N.OBSERVE, N.RETRIEVE, N.ASSESS, N.REASON, N.REJUDGE, N.PROPOSE, N.SUPERVISE]
    assert len(ran) <= len(STAGES)
    assert ran.count(N.PROPOSE) == 1  # stopped before the second `reconcile`


def test_a_self_loop_is_refused_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    def loop(name: StageName, outcome: StageOutcome) -> StageName | None:
        return N.OBSERVE if name is N.OBSERVE else next_stage(name, outcome)

    monkeypatch.setattr(pipeline_module, "next_stage", loop)
    ran = _record_work(monkeypatch)
    with pytest.raises(RuntimeError, match=REVISIT_MESSAGE):
        sl.run_pipeline(sl.fake())
    assert ran == [N.OBSERVE]


def test_the_walk_terminates_within_one_visit_per_stage_whatever_the_router_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a router that always names a stage (never None) runs at most len(STAGES) units."""
    order = list(ADR_ORDER)

    def rotate(name: StageName, outcome: StageOutcome) -> StageName | None:
        return order[(order.index(name) + 1) % len(order)]  # gate -> observe, never None

    monkeypatch.setattr(pipeline_module, "next_stage", rotate)
    ran = _record_work(monkeypatch)
    with pytest.raises(RuntimeError, match=REVISIT_MESSAGE):
        sl.run_pipeline(sl.fake())
    assert len(ran) == len(STAGES)
    assert ran == order


def test_the_pure_walk_refuses_a_cycle_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """`walk` has the same guard: with supervise routed back to propose it raises instead of
    spinning."""
    from divesafe.orchestration import graph as graph_module

    assert walk(ALL_COMPLETED)[-1] is N.GATE  # control
    cyclic = dict(graph_module._BY_NAME)
    real = cyclic[N.SUPERVISE]
    cyclic[N.SUPERVISE] = graph_module.Stage(
        real.name, real.requires, real.produces, lambda outcome: N.PROPOSE
    )
    monkeypatch.setattr(graph_module, "_BY_NAME", cyclic)
    with pytest.raises(RuntimeError, match="cycle"):
        graph_module.walk(ALL_COMPLETED)


def test_the_timed_out_issue_is_the_string_the_agent_stage_emits_and_routes_as_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`STAGE_TIMED_OUT_ISSUE` is what `run_agent_stage` appends on its deadline, the Reason
    stage ends `timed_out`, and the run still reaches the gate with no proposal."""
    import asyncio

    from divesafe.agents import STAGE_TIMED_OUT_ISSUE

    class _Slow:
        name, model, external = "slow", "m", False

        async def complete(self, request: object) -> object:
            await asyncio.sleep(2)
            raise AssertionError("unreachable")

    routed: list[tuple[StageName, StageOutcome]] = []

    def recording_next(name: StageName, outcome: StageOutcome) -> StageName | None:
        routed.append((name, outcome))
        return next_stage(name, outcome)

    monkeypatch.setattr("divesafe.agents.runner.STAGE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pipeline_module, "next_stage", recording_next)
    record = sl.run_pipeline(_Slow())

    assert STAGE_TIMED_OUT_ISSUE in record.agent_issues
    assert STAGE_TIMED_OUT_ISSUE == "agent stage: timed out; no proposal was produced"
    assert record.proposed_recommendation is None
    assert dict(routed)[N.REASON] is O.TIMED_OUT
    assert [name for name, _ in routed] == list(ADR_ORDER)
    assert next_stage(N.REASON, O.TIMED_OUT) is N.REJUDGE


def test_a_reason_stage_that_completes_is_not_labelled_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed: list[tuple[StageName, StageOutcome]] = []

    def recording_next(name: StageName, outcome: StageOutcome) -> StageName | None:
        routed.append((name, outcome))
        return next_stage(name, outcome)

    monkeypatch.setattr(pipeline_module, "next_stage", recording_next)
    sl.run_pipeline(sl.fake())
    assert dict(routed)[N.REASON] is O.COMPLETED
