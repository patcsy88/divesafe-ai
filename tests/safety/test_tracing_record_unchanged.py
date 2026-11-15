"""Tracing never changes a run (ADR 0027 amendment 2026-11-15, "Record unchanged").

Each path runs through `RunManager` with an in-memory store and a fixed clock, once with tracing
off (no sink) and once per traced setup. The stored record (except `stages[*].duration_ms`), the
run's state and failure code, how the run's task ends and the error log records must be equal.
Step 2 has no OpenTelemetry SDK, so the setups are a sink whose `emit` raises, a collector whose
append, start or finish raises, and a full collector; step 3 adds the tracer and exporter setups.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from tests.agent import scripted_llm as sl
from tests.safety.kb_fakes import mutated_module

from divesafe.agents import STAGE_TIMED_OUT_ISSUE, cross_examination, risk_agent, specialists, tot
from divesafe.agents import runner as agent_runner
from divesafe.api import runner as api_runner
from divesafe.data import TIOMAN_ISLAND, DataGovMyWarningConnector, OpenMeteoMarineConnector
from divesafe.domain import AssessmentRecord
from divesafe.models import LLMRequest, LLMResponse
from divesafe.observability import RunCollector, RunTrace
from divesafe.observability import collector as collector_module
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.risk import Reconciliation, reconcile
from divesafe.services import FailureCode, InMemoryAssessmentRepository, RunState
from divesafe.services.runs import NewRun

pytestmark = pytest.mark.safety

RUN_ID = "agent-1"
ACTOR = "actor-a"
R = sl.R


class _Provider:
    """Valid scripted replies; `slow` tasks (or every task with `block`) wait until cancelled."""

    name, model, external, host = "fake", "fake-model", False, None

    def __init__(
        self,
        *,
        slow: frozenset[str] = frozenset(),
        block: bool = False,
        call_timeout: float | None = None,
        recommendation: str = "INSUFFICIENT EVIDENCE",
    ) -> None:
        self._reply = sl.scripted(recommendation)
        self._slow, self._block = slow, block
        if call_timeout is not None:
            self.call_timeout_seconds = call_timeout
        self.entered = asyncio.Event()
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        self.entered.set()
        if self._block or sl.task_of(request) in self._slow:
            await asyncio.sleep(60)
        return LLMResponse(text=self._reply(request), provider=self.name, model=self.model)


Assess = Callable[..., Awaitable[AssessmentRecord]]


def _work(provider: _Provider, assess: Assess) -> Callable[[], Awaitable[AssessmentRecord]]:
    async def work() -> AssessmentRecord:
        return await assess(
            assessment_id=RUN_ID,
            plan=sl.PLAN,
            site=TIOMAN_ISLAND,
            connectors=[
                OpenMeteoMarineConnector(sl._Getter(sl.MARINE)),
                DataGovMyWarningConnector(sl._Getter(sl.WARNINGS)),
            ],
            engine=sl.engine(with_warning_rule=True),
            now=sl.NOW,
            provider=provider,
            clock=lambda: sl.NOW,
            egress_basis="declared_local",
            model_host="localhost",
        )

    return work


# ---- the paths -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Path:
    provider: Callable[[], _Provider]
    run_outcome: str
    deadline: float = 30.0
    cancel_on_entry: bool = False
    patches: Callable[[pytest.MonkeyPatch], None] = lambda _: None


def _relax(monkeypatch: pytest.MonkeyPatch) -> None:
    def relaxing(deterministic: Any, proposed: Any) -> Reconciliation:
        real = reconcile(deterministic, proposed)
        return Reconciliation(R.GO, real.deterministic, real.proposed, real.llm_attempted_downgrade)

    monkeypatch.setattr(pipeline_module, "reconcile", relaxing)


def _crash_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(pipeline_module, "run_agent_stage", crash)


def _short_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_runner, "STAGE_TIMEOUT_SECONDS", 0.05)


PATHS: dict[str, Path] = {
    "ok": Path(_Provider, "stored"),
    "call_timeout": Path(
        lambda: _Provider(slow=frozenset({"risk_assessment"}), call_timeout=0.05), "stored"
    ),
    "stage_deadline": Path(lambda: _Provider(block=True), "stored", patches=_short_stage),
    "run_deadline": Path(lambda: _Provider(block=True), "timeout", deadline=0.2),
    "cancellation": Path(lambda: _Provider(block=True), "shutdown", cancel_on_entry=True),
    "supervise_breach": Path(lambda: _Provider(recommendation="NO-GO"), "internal", patches=_relax),
    "reason_crash": Path(_Provider, "internal", patches=_crash_reason),
}


# ---- sinks and setups ------------------------------------------------------------------------


@dataclass
class RecordingSink:
    """Keeps each trace and what the store held when it arrived."""

    store: InMemoryAssessmentRepository | None = None
    traces: list[RunTrace] = field(default_factory=list)
    seen: list[tuple[bool, RunState | None, FailureCode | None]] = field(default_factory=list)

    def emit(self, trace: RunTrace) -> None:
        assert self.store is not None
        run = self.store.get_run(RUN_ID)
        self.seen.append(
            (
                self.store.get(RUN_ID) is not None,
                None if run is None else run.state,
                None if run is None else run.failure_code,
            )
        )
        self.traces.append(trace)


class RaisingSink(RecordingSink):
    def emit(self, trace: RunTrace) -> None:
        super().emit(trace)
        raise RuntimeError("sink failed")


def _raise(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("tracing failed")


def _append_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RunCollector, "add_model_call", _raise)
    monkeypatch.setattr(RunCollector, "add_stage", _raise)


def _full_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector_module, "MAX_TRACE_ENTRIES", 0)
    monkeypatch.setattr(collector_module, "_drops", dict.fromkeys(collector_module.DROP_REASONS, 0))


SETUPS: dict[str, tuple[type[RecordingSink], Callable[[pytest.MonkeyPatch], None]]] = {
    "recording": (RecordingSink, lambda _: None),
    "emit_raises": (RaisingSink, lambda _: None),
    "append_raises": (RecordingSink, _append_raises),
    "full_collector": (RecordingSink, _full_collector),
    "start_raises": (RecordingSink, lambda mp: mp.setattr(api_runner, "start_collecting", _raise)),
    "finish_raises": (RecordingSink, lambda mp: mp.setattr(RunCollector, "finish", _raise)),
}


def _empty(sink: RecordingSink) -> bool:
    return [(t.stages, t.model_calls) for t in sink.traces] == [((), ())]


# What each setup must visibly do, so a setup that silently stops applying fails here.
SETUP_EFFECTS: dict[str, Callable[[RecordingSink], bool]] = {
    "recording": lambda s: len(s.traces) == 1 and bool(s.traces[0].stages),
    "emit_raises": lambda s: len(s.traces) == 1,
    "append_raises": _empty,
    "full_collector": lambda s: _empty(s) and collector_module.drop_counts()["collector_full"] > 0,
    "start_raises": lambda s: s.traces == [],
    "finish_raises": lambda s: s.traces == [],
}


# ---- the driver ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    record: dict[str, Any] | None
    state: RunState | None
    code: FailureCode | None
    ending: tuple[str, type[BaseException] | None]
    errors: tuple[tuple[str, str], ...]


def _comparable(record: AssessmentRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    dumped = record.model_dump(mode="json")
    for stage in dumped["stages"]:
        stage["duration_ms"] = 0
    return dumped


async def _drive(
    path: Path,
    sink: RecordingSink | None,
    manager_type: type[Any],
    assess: Assess,
    store: InMemoryAssessmentRepository,
) -> tuple[str, type[BaseException] | None]:
    admission = store.admit(
        NewRun(id=RUN_ID, created_by=ACTOR, assigned_decider=ACTOR, plan=sl.PLAN),
        max_in_flight=4,
        max_per_actor=4,
        lease=timedelta(hours=1),
    )
    assert admission.run_token is not None
    manager = manager_type(
        store,
        deadline=timedelta(seconds=path.deadline),
        grace=timedelta(seconds=5),
        sink=sink,
    )
    provider = path.provider()
    manager.start(RUN_ID, admission.run_token, ACTOR, _work(provider, assess))
    (task,) = [t for t in asyncio.all_tasks() if t.get_name() == f"run-{RUN_ID}"]
    if path.cancel_on_entry:
        # Bounded: a run that ends before its first model call must not hang the suite.
        entered = asyncio.ensure_future(provider.entered.wait())
        await asyncio.wait({entered, task}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        entered.cancel()
        task.cancel()
    await asyncio.wait({task}, timeout=30)
    assert task.done(), "the run did not end"
    if task.cancelled():
        return ("cancelled", None)
    exc = task.exception()
    return ("returned", None) if exc is None else ("raised", type(exc))


def run_once(
    path: Path,
    sink: RecordingSink | None,
    caplog: pytest.LogCaptureFixture,
    *,
    manager_type: type[Any] = api_runner.RunManager,
    assess: Assess = pipeline_module.assess_dive,
) -> Outcome:
    store = InMemoryAssessmentRepository(clock=lambda: sl.NOW)
    if sink is not None:
        sink.store = store
    caplog.clear()
    with caplog.at_level(logging.INFO):
        ending = asyncio.run(_drive(path, sink, manager_type, assess, store))
    run = store.get_run(RUN_ID)
    return Outcome(
        record=_comparable(store.get(RUN_ID)),
        state=None if run is None else run.state,
        code=None if run is None else run.failure_code,
        ending=ending,
        errors=tuple(
            (r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR
        ),
    )


@contextmanager
def _applied(
    monkeypatch: pytest.MonkeyPatch, patch: Callable[[pytest.MonkeyPatch], None]
) -> Iterator[None]:
    with monkeypatch.context() as mp:
        patch(mp)
        yield


EXPECTED: dict[str, tuple[RunState | None, FailureCode | None, tuple[str, Any]]] = {
    "ok": (None, None, ("returned", None)),
    "call_timeout": (None, None, ("returned", None)),
    "stage_deadline": (None, None, ("returned", None)),
    "run_deadline": (RunState.FAILED, FailureCode.TIMEOUT, ("returned", None)),
    "cancellation": (RunState.FAILED, FailureCode.SHUTDOWN, ("cancelled", None)),
    "supervise_breach": (RunState.FAILED, FailureCode.INTERNAL, ("returned", None)),
    "reason_crash": (RunState.FAILED, FailureCode.INTERNAL, ("returned", None)),
}


# ---- record unchanged ------------------------------------------------------------------------


@pytest.mark.parametrize("setup", list(SETUPS))
@pytest.mark.parametrize("path_name", list(PATHS))
def test_tracing_never_changes_the_run(
    path_name: str, setup: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = PATHS[path_name]
    sink_type, setup_patch = SETUPS[setup]
    with _applied(monkeypatch, path.patches):
        off = run_once(path, None, caplog)
        with _applied(monkeypatch, setup_patch):
            sink = sink_type()
            on = run_once(path, sink, caplog)
            assert SETUP_EFFECTS[setup](sink), f"the {setup} setup did not take effect"
    state, code, ending = EXPECTED[path_name]
    assert (off.state, off.code, off.ending) == (state, code, ending)  # the path is what it says
    assert (off.record is not None) == (code is None)
    assert on == off


@pytest.mark.parametrize("path_name", list(PATHS))
def test_each_path_emits_one_trace_with_its_outcome_and_what_it_collected(
    path_name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Teeth for the comparison above: tracing is really on in the traced runs."""
    path = PATHS[path_name]
    sink = RecordingSink()
    with _applied(monkeypatch, path.patches):
        outcome = run_once(path, sink, caplog)
    (trace,) = sink.traces
    assert trace.assessment_id == RUN_ID and trace.run_outcome == path.run_outcome
    if outcome.record is not None:
        stored = [(s["name"], s["outcome"]) for s in outcome.record["stages"]]
        assert [(s.name.value, s.outcome.value) for s in trace.stages] == stored
    if path_name in ("ok", "call_timeout", "supervise_breach"):
        assert trace.model_calls, "the model calls were not collected"
    if path_name == "call_timeout":
        assert "timed_out" in {c.fields["outcome"] for c in trace.model_calls}
    if path_name == "stage_deadline":
        assert outcome.record is not None
        assert STAGE_TIMED_OUT_ISSUE in outcome.record["agent_issues"]


def test_tracing_off_collects_nothing_and_emits_nothing(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[object] = []

    class _Watching(_Provider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            seen.append(collector_module.current_collector())
            return await super().complete(request)

    run_once(Path(_Watching, "stored"), None, caplog)
    assert seen and all(c is None for c in seen)


# ---- ordering --------------------------------------------------------------------------------


@pytest.mark.parametrize("path_name", list(PATHS))
def test_no_trace_is_emitted_before_the_record_is_stored_or_the_run_marked_failed(
    path_name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _assert_ordered(path_name, monkeypatch, caplog, api_runner.RunManager)


def _assert_ordered(
    path_name: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    manager_type: type[Any],
) -> None:
    path = PATHS[path_name]
    sink = RecordingSink()
    with _applied(monkeypatch, path.patches):
        run_once(path, sink, caplog, manager_type=manager_type)
    state, code, _ = EXPECTED[path_name]
    if code is None:
        assert sink.seen == [(True, None, None)]
    else:
        assert sink.seen == [(False, state, code)]


# ---- mutations -------------------------------------------------------------------------------

_EMIT_SUPPRESS = (
    "                with contextlib.suppress(Exception):\n"
    '                    outcome = "stored" if code is None else code.value\n'
)


def _manager_mutant(*edits: tuple[str, str]) -> type[Any]:
    mutant = mutated_module(api_runner, *edits)
    manager: type[Any] = mutant.RunManager
    return manager


@pytest.mark.parametrize("path_name", list(PATHS))
def test_a_mutant_without_the_emission_suppression_changes_how_the_task_ends(
    path_name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    unsuppressed = _EMIT_SUPPRESS.replace("with contextlib.suppress(Exception):", "if True:")
    mutant = _manager_mutant((_EMIT_SUPPRESS, unsuppressed))
    path = PATHS[path_name]
    with _applied(monkeypatch, path.patches):
        off = run_once(path, None, caplog)
        broken = run_once(path, RaisingSink(), caplog, manager_type=mutant)
    assert broken.ending != off.ending


class _TestStop(BaseException):
    """Test-private: never KeyboardInterrupt or SystemExit, which asyncio re-raises out of the
    event loop and would stop the pytest session."""


class _StoppingSink(RecordingSink):
    def emit(self, trace: RunTrace) -> None:
        super().emit(trace)
        raise _TestStop


@pytest.mark.parametrize("widened", [False, True])
def test_a_base_exception_from_the_emission_is_never_swallowed(
    widened: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """The record is stored first, and the task still ends with the sink's `BaseException`.
    Widening the suppression to `BaseException` swallows it (the mutation check)."""
    manager: type[Any] = api_runner.RunManager
    if widened:
        manager = _manager_mutant(
            (_EMIT_SUPPRESS, _EMIT_SUPPRESS.replace("(Exception)", "(BaseException)"))
        )
    sink = _StoppingSink()
    outcome = run_once(PATHS["ok"], sink, caplog, manager_type=manager)
    assert outcome.record is not None and sink.seen == [(True, None, None)]
    if widened:
        assert outcome.ending == ("returned", None)
    else:
        assert outcome.ending == ("raised", _TestStop)


_COMPLETE = (
    "            stored = await run_in_threadpool(self._store.complete, record, run_token)\n"
)
_EARLY_EMIT = (
    "            if collector is not None and self._sink is not None:\n"
    '                self._sink.emit(collector.finish(run_id, "stored"))\n'
)
_MARK = "                await self._mark_failed(run_id, run_token, code)\n"
_EARLY_FAILED_EMIT = (
    "                if collector is not None and self._sink is not None:\n"
    "                    self._sink.emit(collector.finish(run_id, code.value))\n"
)


@pytest.mark.parametrize(
    ("path_name", "edit"),
    [
        ("ok", (_COMPLETE, _EARLY_EMIT + _COMPLETE)),
        ("run_deadline", (_MARK, _EARLY_FAILED_EMIT + _MARK)),
        ("reason_crash", (_MARK, _EARLY_FAILED_EMIT + _MARK)),
    ],
)
def test_a_mutant_that_emits_before_storing_or_marking_fails_the_ordering_check(
    path_name: str,
    edit: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mutant = _manager_mutant(edit)
    with pytest.raises(AssertionError):
        _assert_ordered(path_name, monkeypatch, caplog, mutant)


_MODEL_APPEND = (
    "        with contextlib.suppress(Exception):  # tracing must never change the call's result\n"
    "            record_model_call(fields, started_ns)\n"
)
_STAGE_APPEND = (
    "    with contextlib.suppress(Exception):  # tracing must never change the run (ADR 0027)\n"
    "        record_stage(trace)\n"
)


def test_a_mutant_without_the_model_call_append_suppression_changes_the_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = PATHS["ok"]
    off = run_once(path, None, caplog)
    mutant = mutated_module(
        agent_runner,
        (_MODEL_APPEND, _MODEL_APPEND.replace(_MODEL_APPEND.split("\n")[0], "        if True:")),
    )
    with monkeypatch.context() as mp:
        for module in (specialists, tot, risk_agent, cross_examination):
            mp.setattr(module, "call_structured", mutant.call_structured)
        _append_raises(mp)
        broken = run_once(path, RecordingSink(), caplog)
    assert broken.record != off.record


def test_a_mutant_without_the_stage_append_suppression_changes_the_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = PATHS["ok"]
    off = run_once(path, None, caplog)
    mutant = mutated_module(
        pipeline_module,
        (_STAGE_APPEND, _STAGE_APPEND.replace(_STAGE_APPEND.split("\n")[0], "    if True:")),
    )
    with monkeypatch.context() as mp:
        _append_raises(mp)
        broken = run_once(path, RecordingSink(), caplog, assess=mutant.assess_dive)
    assert broken.record != off.record
    assert off.record is not None
