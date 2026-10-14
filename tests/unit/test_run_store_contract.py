"""Contract every pending-run store must satisfy (ADR 0019), run against each adapter."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from tests.unit.test_repository_contract import (
    TEST_DSN,
    _record,
    reset_test_database,
)

from divesafe.domain import AssessmentRecord
from divesafe.services import (
    CapacityError,
    FailureCode,
    InMemoryAssessmentRepository,
    NewRun,
    PostgresAssessmentRepository,
    RunMismatchError,
    RunState,
)

LEASE = timedelta(minutes=5)
SHORT = timedelta(milliseconds=150)


class Store:
    """Both repository types expose the same run methods."""


def _postgres() -> Any:
    assert TEST_DSN is not None
    reset_test_database()
    return PostgresAssessmentRepository(TEST_DSN)


ADAPTERS: dict[str, Callable[[], Any]] = {
    "in-memory": InMemoryAssessmentRepository,
    "postgres": pytest.param(  # type: ignore[dict-item]
        _postgres,
        marks=[
            pytest.mark.integration,
            pytest.mark.skipif(TEST_DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
        ],
    ),
}


@pytest.fixture(params=list(ADAPTERS.values()), ids=list(ADAPTERS))
def store(request: pytest.FixtureRequest) -> Any:
    return request.param()


def _run(run_id: str = "r1", actor: str = "alice", decider: str = "bob") -> NewRun:
    return NewRun(id=run_id, created_by=actor, assigned_decider=decider, plan=_record(run_id).plan)


def _admit(store: Any, run: NewRun, *, cap: int = 4, per_actor: int = 4, lease: Any = LEASE) -> Any:
    return store.admit(run, max_in_flight=cap, max_per_actor=per_actor, lease=lease)


def _completed(run_id: str = "r1") -> AssessmentRecord:
    return _record(run_id)


def test_a_started_run_is_pending_and_is_never_an_assessment(store: Any) -> None:
    admission = _admit(store, _run())
    assert admission.kind == "started" and admission.run_token
    assert store.get_run("r1").state is RunState.RUNNING
    assert store.get("r1") is None
    assert store.get_run("missing") is None


def test_the_same_request_joins_without_a_second_run_or_token(store: Any) -> None:
    first = _admit(store, _run(), cap=1, per_actor=1)
    again = _admit(store, _run(), cap=1, per_actor=1)  # at the cap, still a join, not a 429
    assert (first.kind, again.kind) == ("started", "joined")
    assert again.run_token is None


@pytest.mark.parametrize(
    "other", [_run(decider="carol"), _run(actor="mallory")], ids=["decider", "actor"]
)
def test_a_different_request_for_the_same_id_is_refused(store: Any, other: NewRun) -> None:
    _admit(store, _run())
    with pytest.raises(RunMismatchError):
        _admit(store, other)


def test_a_different_plan_for_the_same_id_is_refused(store: Any) -> None:
    _admit(store, _run())
    other = _run().model_copy(
        update={"plan": _run().plan.model_copy(update={"planned_duration_minutes": 45})}
    )
    with pytest.raises(RunMismatchError):
        _admit(store, other)


def test_the_server_cap_refuses_and_stores_nothing(store: Any) -> None:
    _admit(store, _run("r1", "a"), cap=1)
    with pytest.raises(CapacityError) as excinfo:
        _admit(store, _run("r2", "b"), cap=1)
    assert excinfo.value.scope == "server"
    assert store.get_run("r2") is None


def test_the_per_actor_cap_stops_one_key_taking_every_slot(store: Any) -> None:
    _admit(store, _run("r1", "a"), per_actor=1)
    with pytest.raises(CapacityError) as excinfo:
        _admit(store, _run("r2", "a"), per_actor=1)
    assert excinfo.value.scope == "actor"
    assert _admit(store, _run("r3", "b"), per_actor=1).kind == "started"


def test_expired_runs_do_not_count_toward_the_cap(store: Any) -> None:
    _admit(store, _run("r1", "a"), cap=1, lease=SHORT)
    time.sleep(0.4)
    assert _admit(store, _run("r2", "b"), cap=1).kind == "started"


def test_completion_stores_the_record_and_drops_the_run(store: Any) -> None:
    token = _admit(store, _run()).run_token
    assert token is not None
    assert store.complete(_completed(), token) is True
    assert store.get("r1") == _completed()
    assert store.get_run("r1") is None
    assert _admit(store, _run()).kind == "completed"


def test_a_wrong_token_stores_nothing(store: Any) -> None:
    _admit(store, _run())
    assert store.complete(_completed(), "not-the-token") is False
    assert store.get("r1") is None
    assert store.get_run("r1").state is RunState.RUNNING


def test_a_run_cannot_complete_twice(store: Any) -> None:
    token = _admit(store, _run()).run_token
    assert token is not None
    assert store.complete(_completed(), token) is True
    assert store.complete(_completed(), token) is False


def test_a_late_run_never_turns_a_failed_id_into_a_result(store: Any) -> None:
    token = _admit(store, _run(), lease=SHORT).run_token
    assert token is not None
    time.sleep(0.4)
    assert store.complete(_completed(), token) is False
    assert store.get("r1") is None
    failed = store.get_run("r1")
    assert failed.state is RunState.FAILED and failed.failure_code is FailureCode.TIMEOUT
    again = _admit(store, _run())
    assert again.kind == "failed" and again.run_token is None  # terminal: never restarted
    assert store.get("r1") is None


def test_a_failed_run_cannot_complete(store: Any) -> None:
    token = _admit(store, _run()).run_token
    assert token is not None
    assert store.fail("r1", token, FailureCode.INTERNAL) is True
    assert store.complete(_completed(), token) is False
    assert store.get("r1") is None
    assert store.get_run("r1").failure_code is FailureCode.INTERNAL
    assert store.fail("r1", token, FailureCode.TIMEOUT) is False  # the first reason stands


def test_only_the_run_that_owns_the_token_can_fail_it(store: Any) -> None:
    _admit(store, _run())
    assert store.fail("r1", "not-the-token", FailureCode.INTERNAL) is False
    assert store.get_run("r1").state is RunState.RUNNING


def test_a_run_record_cannot_carry_a_decision(store: Any) -> None:
    from tests.unit.test_repository_contract import _decided

    from divesafe.services import InvalidSuccessorError

    token = _admit(store, _run()).run_token
    assert token is not None
    with pytest.raises(InvalidSuccessorError):
        store.complete(_decided(_completed()), token)
    assert store.get("r1") is None


def test_two_requests_with_one_key_start_exactly_one_run(store: Any) -> None:
    kinds: list[str] = []
    gate = threading.Barrier(8)

    def go() -> None:
        gate.wait()
        kinds.append(_admit(store, _run()).kind)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert kinds.count("started") == 1 and kinds.count("joined") == 7


def test_concurrent_admission_never_exceeds_the_cap(store: Any) -> None:
    outcomes: list[str] = []
    gate = threading.Barrier(10)

    def go(n: int) -> None:
        gate.wait()
        try:
            outcomes.append(_admit(store, _run(f"c{n}", f"u{n}"), cap=3).kind)
        except CapacityError:
            outcomes.append("refused")

    threads = [threading.Thread(target=go, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("started") == 3 and outcomes.count("refused") == 7


def test_cleanup_marks_expired_runs_then_deletes_old_failures_and_counts_them(store: Any) -> None:
    _admit(store, _run("old", "a"), lease=SHORT)
    live = _admit(store, _run("live", "b"))
    time.sleep(0.4)
    first = store.cleanup(retention=timedelta(days=7), batch=50)
    assert (first.marked_failed, first.deleted) == (1, 0)
    assert store.get_run("old").state is RunState.FAILED
    second = store.cleanup(retention=timedelta(0), batch=50)
    assert second.deleted == 1
    assert store.get_run("old") is None
    assert store.get_run("live").state is RunState.RUNNING and live.run_token
    assert store.failure_counts() == {"timeout": 1}


def test_cleanup_is_bounded_by_the_batch(store: Any) -> None:
    for n in range(5):
        token = _admit(store, _run(f"f{n}", f"u{n}")).run_token
        assert token is not None
        store.fail(f"f{n}", token, FailureCode.SHUTDOWN)
    assert store.cleanup(retention=timedelta(0), batch=2).deleted == 2
    assert sum(1 for n in range(5) if store.get_run(f"f{n}") is not None) == 3


def test_cleanup_never_touches_an_assessment(store: Any) -> None:
    token = _admit(store, _run()).run_token
    assert token is not None
    store.complete(_completed(), token)
    store.cleanup(retention=timedelta(0), batch=50)
    assert store.get("r1") == _completed()


def test_a_finished_id_never_starts_a_second_run_when_admission_races_completion_smoke(
    store: Any,
) -> None:
    token = _admit(store, _run()).run_token
    assert token is not None
    results: list[str] = []
    gate = threading.Barrier(2)

    def finish() -> None:
        gate.wait()
        store.complete(_completed(), token)

    def retry() -> None:
        gate.wait()
        for _ in range(20):
            results.append(_admit(store, _run()).kind)

    threads = [threading.Thread(target=finish), threading.Thread(target=retry)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert "started" not in results
    assert store.get_run("r1") is None
