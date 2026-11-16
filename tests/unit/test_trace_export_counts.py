"""ADR 0027 amendment, step 3: the exporter's drop reasons and its span reservation.

The real exporter runs over the stub wire (`tests.safety.trace_wire`); no socket is opened. The
library's retry waits are skipped, not its retries, so each failed export is counted once.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import requests
from opentelemetry.exporter.otlp.common import http as otlp_http
from tests.safety import trace_wire as tw
from tests.safety.test_tracing_export import TASKS, _real_run

from divesafe.api import trace_export
from divesafe.api.trace_export import _Pending
from divesafe.domain import StageName, StageOutcome
from divesafe.observability import collector as collector_module


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[tw.Wire]:
    monkeypatch.setattr(otlp_http._OTLPHTTPClient, "_compute_backoff", lambda self, retry: 0.0)
    with tw.installed_wire(monkeypatch) as installed:
        yield installed


def _one_run() -> object:
    return tw.run_trace(
        assessment_id="0" * 32,
        stages=(tw.stage(StageName.OBSERVE, StageOutcome.COMPLETED),),
        model_calls=(tw.call(task="risk_assessment", outcome="ok", duration_ms=3),),
    )


def _counts_after_one_export(wire: tw.Wire) -> dict[str, int]:
    export = wire.build()
    export.sink.emit(_one_run())  # type: ignore[arg-type]
    assert export.shutdown(5.0)
    return collector_module.drop_counts()


def _only(reason: str) -> dict[str, int]:
    return {name: int(name == reason) for name in collector_module.DROP_REASONS}


def test_a_successful_export_counts_nothing(wire: tw.Wire) -> None:
    assert _counts_after_one_export(wire) == _only("none")
    assert len(wire.sent) == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (requests.ReadTimeout("slow"), "timeout"),
        (requests.ConnectTimeout("slow"), "timeout"),
        (requests.ConnectionError("refused"), "connection"),
    ],
)
def test_a_failed_send_counts_its_reason_once(error: Exception, reason: str, wire: tw.Wire) -> None:
    wire.mode, wire.error = "raise", error
    assert _counts_after_one_export(wire) == _only(reason)
    assert len(wire.sent) >= 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 500, 503])
def test_an_error_status_counts_http_status_once(status: int, wire: tw.Wire) -> None:
    wire.status = status
    assert _counts_after_one_export(wire) == _only("http_status")


def test_a_run_that_does_not_fit_is_dropped_whole_as_queue_full(wire: tw.Wire) -> None:
    stages = tuple(
        tw.stage(StageName.OBSERVE, StageOutcome.COMPLETED)
        for _ in range(trace_export.MAX_QUEUE_SIZE)
    )
    export = wire.build()
    export.sink.emit(tw.run_trace(assessment_id="0" * 32, stages=stages))
    assert export.shutdown(5.0)
    assert collector_module.drop_counts() == _only("queue_full")
    assert wire.sent == []


def test_a_run_after_shutdown_is_ignored(wire: tw.Wire) -> None:
    export = wire.build()
    assert export.shutdown(5.0)
    export.sink.emit(_one_run())  # type: ignore[arg-type]
    assert wire.sent == []
    assert collector_module.drop_counts() == _only("none")


def test_pending_reserves_up_to_its_limit_and_releases() -> None:
    pending = _Pending(10)
    assert pending.reserve(6) and pending.count == 6
    assert not pending.reserve(5) and pending.count == 6  # all or nothing
    assert pending.reserve(4) and pending.count == 10
    assert not pending.reserve(1)
    pending.release(7)
    assert pending.count == 3
    pending.release(99)
    assert pending.count == 0  # never negative
    assert pending.reserve(10)


def test_the_reservation_is_released_once_every_span_is_exported(wire: tw.Wire) -> None:
    captured: list[_Pending] = []
    real = trace_export._Pending

    def keep(limit: int) -> _Pending:
        captured.append(real(limit))
        return captured[-1]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(trace_export, "_Pending", keep)
        export = wire.build()
    (pending,) = captured
    export.sink.emit(_one_run())  # type: ignore[arg-type]
    assert export.shutdown(5.0)
    assert pending.count == 0


def test_the_task_allow_list_is_exactly_a_cross_examination_runs_tasks() -> None:
    trace, _, provider = _real_run()
    tasks = {str(call.fields["task"]) for call in trace.model_calls}
    assert len(provider.requests) == len(trace.model_calls) == 14
    assert tasks == trace_export.TASK_NAMES == TASKS
