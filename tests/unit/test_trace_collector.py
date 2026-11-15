"""The per-run trace collector and its drop counter (ADR 0027 amendment 2026-11-15). Not safety
tests: the counter cannot change an outcome; "record unchanged" is in tests/safety."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from tests.agent import scripted_llm as sl

from divesafe.api.runner import RunManager
from divesafe.domain import AssessmentRecord, StageName, StageOutcome, StageTrace
from divesafe.observability import (
    MAX_TRACE_ENTRIES,
    RunCollector,
    RunTrace,
    StageEntry,
    count_drop,
    current_collector,
    drop_counts,
    record_model_call,
    record_stage,
    start_collecting,
)
from divesafe.observability import collector as collector_module
from divesafe.services import InMemoryAssessmentRepository
from divesafe.services.runs import NewRun

SRC = Path(__file__).resolve().parents[2] / "src" / "divesafe"


def _stage() -> StageTrace:
    return StageTrace(
        name=StageName.OBSERVE,
        outcome=StageOutcome.COMPLETED,
        next_stage=StageName.RETRIEVE,
        started_at=sl.NOW,
        finished_at=sl.NOW,
        duration_ms=1,
    )


def test_the_bound_covers_a_run_with_cross_examination_several_times_over() -> None:
    from divesafe.config.settings import CROSS_EXAMINATION_CALL_CAP
    from divesafe.orchestration.graph import STAGES

    assert 2 * (CROSS_EXAMINATION_CALL_CAP + len(STAGES)) <= MAX_TRACE_ENTRIES


def test_a_full_collector_drops_and_counts_collector_full(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(collector_module, "MAX_TRACE_ENTRIES", 2)
    monkeypatch.setattr(collector_module, "_drops", dict.fromkeys(collector_module.DROP_REASONS, 0))
    collector = RunCollector()
    collector.add_stage(_stage())
    collector.add_model_call({"task": "t"}, 1)
    with caplog.at_level(logging.WARNING, logger=collector_module.__name__):
        collector.add_stage(_stage())
        collector.add_model_call({"task": "u"}, 2)
    trace = collector.finish("a", "stored")
    assert len(trace.stages) == 1 and len(trace.model_calls) == 1
    assert drop_counts()["collector_full"] == 2
    assert {k: v for k, v in drop_counts().items() if k != "collector_full"} == dict.fromkeys(
        ("timeout", "connection", "http_status", "queue_full"), 0
    )
    # Logged once per reason, as the code only.
    logged = [r for r in caplog.records if r.getMessage() == "trace data dropped"]
    assert [getattr(r, "reason", None) for r in logged] == ["collector_full"]


@pytest.mark.parametrize("reason", collector_module.DROP_REASONS)
def test_each_reason_counts_only_itself(reason: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector_module, "_drops", dict.fromkeys(collector_module.DROP_REASONS, 0))
    count_drop(reason)  # type: ignore[arg-type]
    count_drop(reason)  # type: ignore[arg-type]
    assert drop_counts() == {r: (2 if r == reason else 0) for r in collector_module.DROP_REASONS}


def test_an_unknown_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown drop reason"):
        count_drop("other")  # type: ignore[arg-type]


def test_the_collected_fields_are_a_read_only_copy() -> None:
    fields: dict[str, object] = {"task": "t", "outcome": "ok"}
    collector = RunCollector()
    collector.add_model_call(fields, 5)
    fields["task"] = "changed"
    (entry,) = collector.finish("a", "stored").model_calls
    assert entry.fields == {"task": "t", "outcome": "ok"} and entry.started_ns == 5
    with pytest.raises(TypeError):
        entry.fields["task"] = "x"  # type: ignore[index]


def test_without_a_collector_the_record_functions_do_nothing() -> None:
    def outside() -> None:
        assert current_collector() is None
        record_model_call({"task": "t"}, 1)
        record_stage(_stage())

    contextvars.Context().run(outside)


def test_child_tasks_share_the_runs_collector() -> None:
    async def run() -> RunCollector:
        collector = start_collecting()

        async def child(n: int) -> None:
            record_model_call({"task": f"t{n}"}, n)

        await asyncio.gather(child(1), child(2))
        await asyncio.create_task(child(3))
        return collector

    collector = asyncio.run(run())
    tasks = {c.fields["task"] for c in collector.finish("a", "stored").model_calls}
    assert tasks == {"t1", "t2", "t3"}


class _Sink:
    def __init__(self) -> None:
        self.traces: list[RunTrace] = []

    def emit(self, trace: RunTrace) -> None:
        self.traces.append(trace)


def test_two_concurrent_runs_each_hold_only_their_own_model_calls() -> None:
    both_started = asyncio.Event()
    started: set[str] = set()

    def work(run_id: str, store: InMemoryAssessmentRepository) -> object:
        async def go() -> AssessmentRecord:
            record_model_call({"task": f"{run_id}:first"}, 1)
            started.add(run_id)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()  # interleave the two runs
            record_model_call({"task": f"{run_id}:second"}, 2)
            raise RuntimeError("stop here; the trace is what is checked")

        return go

    async def main() -> list[RunTrace]:
        store = InMemoryAssessmentRepository(clock=lambda: sl.NOW)
        sink = _Sink()
        manager = RunManager(
            store, deadline=timedelta(seconds=10), grace=timedelta(seconds=1), sink=sink
        )
        for run_id in ("run-a", "run-b"):
            admission = store.admit(
                NewRun(id=run_id, created_by="x", assigned_decider="x", plan=sl.PLAN),
                max_in_flight=4,
                max_per_actor=4,
                lease=timedelta(hours=1),
            )
            assert admission.run_token is not None
            manager.start(run_id, admission.run_token, "x", work(run_id, store))  # type: ignore[arg-type]
        await manager.shutdown()
        return sink.traces

    traces = {t.assessment_id: t for t in asyncio.run(main())}
    assert set(traces) == {"run-a", "run-b"}
    for run_id, trace in traces.items():
        assert [c.fields["task"] for c in trace.model_calls] == [
            f"{run_id}:first",
            f"{run_id}:second",
        ]
        assert trace.run_outcome == "internal"


def test_the_collector_package_imports_only_domain_and_the_standard_library() -> None:
    """It is imported by `agents`, `orchestration` and `api` (ADR 0027 Known gaps)."""
    for path in (SRC / "observability").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, (path.name, "relative import")
                names = [node.module or ""]
            for name in names:
                if name.startswith("divesafe"):
                    assert name.startswith(("divesafe.domain", "divesafe.observability")), (
                        path.name,
                        name,
                    )
                else:
                    assert name.split(".")[0] in {
                        "__future__",
                        "collections",
                        "contextvars",
                        "dataclasses",
                        "datetime",
                        "logging",
                        "threading",
                        "time",
                        "types",
                        "typing",
                    }, (path.name, name)


def test_a_collected_stage_holds_no_evidence_ids_or_issues() -> None:
    assert set(StageEntry.__slots__) == {
        "name",
        "outcome",
        "started_at",
        "finished_at",
        "duration_ms",
    }
    trace = StageTrace(
        name=StageName.OBSERVE,
        outcome=StageOutcome.COMPLETED,
        next_stage=StageName.RETRIEVE,
        started_at=sl.NOW,
        finished_at=sl.NOW,
        evidence_ids=("ev-marker",),
        issues=(),
        duration_ms=3,
    )
    collector = RunCollector()
    collector.add_stage(trace)
    (entry,) = collector.finish("a", "stored").stages
    assert entry == StageEntry(StageName.OBSERVE, StageOutcome.COMPLETED, sl.NOW, sl.NOW, 3)
    assert "ev-marker" not in repr(collector.finish("a", "stored"))
