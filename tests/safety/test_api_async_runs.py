"""Asynchronous creation (ADR 0019): a pending or failed run is never a result or decidable."""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from typing import Any

import pytest
from tests.api_polling import create_and_poll, poll
from tests.safety.test_api_human_gate import AUTH_A, AUTH_B, PLAN, T0, _build, _hash

from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role
from divesafe.api.ratelimit import RateLimiter
from divesafe.api.state import RunLimits, default_limits
from divesafe.services import FailureCode, RunState
from divesafe.services.runs import NewRun

pytestmark = pytest.mark.safety

KEY = "async-key-0123456789"
H = {**AUTH_A, "Idempotency-Key": KEY}


class _Gate:
    """A connector that holds a run open until the test lets it go."""

    name = "gate"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()

    async def fetch(self, *args: object) -> Any:
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        from divesafe.data import FetchResult

        return FetchResult(items=(), issues=("gate: released",))


def _held(*, live: bool = True, **limits: Any) -> tuple[Any, _Gate, Any]:
    client, clock, repo = _build(live=live, runs=RunLimits(**limits))
    gate = _Gate()
    state = client.app.state.divesafe
    state.limiter = RateLimiter(default_limits(read_per_minute=100_000))  # polling is frequent
    state.connectors = [gate]
    return client, gate, repo


def _wait(event: threading.Event) -> None:
    assert event.wait(5), "the run did not reach the connector"


def test_a_create_returns_202_with_a_location_and_no_recommendation() -> None:
    client, gate, _ = _held()
    try:
        r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
        assert r.status_code == 202 and r.headers["location"].startswith("/v1/assessments/")
        body = r.json()
        assert body["status"] == "RUNNING"
        assert not {"record", "recommendation", "final_recommendation", "evidence"} & set(body)
        assert "Do not act" in r.text or "nothing may be" in r.text
    finally:
        gate.release.set()


def test_a_running_run_has_no_result_and_cannot_be_decided_or_read_as_evidence() -> None:
    client, gate, repo = _held()
    try:
        r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
        rid = r.json()["id"]
        _wait(gate.entered)
        got = client.get(r.headers["location"], headers=AUTH_A)
        assert got.status_code == 200 and got.json()["status"] == "RUNNING"
        assert "record" not in got.json()
        for path, body in (
            (f"/v1/assessments/{rid}/decision", {"decision": "GO"}),
            (f"/v1/assessments/{rid}/actual-conditions", {"observations": {"v": 1}}),
        ):
            refused = client.post(path, json=body, headers=AUTH_A)
            assert refused.status_code == 409
            assert refused.json()["detail"].startswith("nothing was recorded")
        ev = client.get(f"/v1/assessments/{rid}/evidence", headers=AUTH_A)
        assert ev.status_code == 409 and "records" not in ev.text
        assert repo.get(rid) is None
    finally:
        gate.release.set()


def test_a_stranger_cannot_tell_that_a_run_exists() -> None:
    client, gate, _ = _held()
    state = client.app.state.divesafe
    state.authenticator = ApiKeyAuthenticator(
        {
            _hash("key-for-alice-test-only"): KeyEntry("alice", frozenset(Role)),
            _hash("k-mallory"): KeyEntry("mallory", frozenset({Role.ASSESSOR, Role.DECIDER})),
        }
    )
    try:
        rid = client.post("/v1/assessments", json=PLAN, headers=AUTH_A).json()["id"]
        _wait(gate.entered)
        other = {"Authorization": "Bearer k-mallory"}
        missing = "f" * 32
        for path in (f"/v1/assessments/{rid}", f"/v1/assessments/{missing}"):
            assert client.get(path, headers=other).status_code == 404
        for tail, body in (("decision", {"decision": "GO"}),):
            a = client.post(f"/v1/assessments/{rid}/{tail}", json=body, headers=other)
            b = client.post(f"/v1/assessments/{missing}/{tail}", json=body, headers=other)
            assert (a.status_code, a.json()) == (b.status_code, b.json())
    finally:
        gate.release.set()


def test_the_same_key_joins_the_run_and_starts_one() -> None:
    client, gate, repo = _held()
    try:
        first = client.post("/v1/assessments", json=PLAN, headers=H)
        _wait(gate.entered)
        again = client.post("/v1/assessments", json=PLAN, headers=H)
        assert (first.status_code, again.status_code) == (202, 202)
        assert first.json()["id"] == again.json()["id"]
        gate.release.set()
        done = poll(client, first.headers["location"], AUTH_A)
        assert done.json()["record"]["id"] == first.json()["id"]
        assert len(repo._records) == 1
    finally:
        gate.release.set()


def test_a_join_with_a_different_request_is_refused() -> None:
    client, gate, _ = _held()
    try:
        client.post("/v1/assessments", json=PLAN, headers=H)
        other = client.post("/v1/assessments", json={**PLAN, "max_depth_m": 31}, headers=H)
        assert other.status_code == 422 and "different request" in other.json()["detail"]
        named = client.post("/v1/assessments", json={**PLAN, "decider": "bob"}, headers=H)
        assert named.status_code == 422
    finally:
        gate.release.set()


def test_the_server_cap_refuses_with_retry_after_and_stores_nothing() -> None:
    client, gate, repo = _held(max_in_flight=1, max_per_actor=1)
    try:
        client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
        _wait(gate.entered)
        full = client.post("/v1/assessments", json=PLAN, headers=AUTH_B)
        assert full.status_code == 429 and full.headers["retry-after"] == "30"
        assert full.json()["detail"].startswith("nothing was recorded")
        assert len(repo._runs) == 1
    finally:
        gate.release.set()


def test_the_per_actor_cap_stops_one_key_taking_every_slot() -> None:
    client, gate, _ = _held(max_in_flight=4, max_per_actor=1)
    try:
        assert client.post("/v1/assessments", json=PLAN, headers=AUTH_A).status_code == 202
        assert client.post("/v1/assessments", json=PLAN, headers=AUTH_A).status_code == 429
        assert client.post("/v1/assessments", json=PLAN, headers=AUTH_B).status_code == 202
    finally:
        gate.release.set()


def test_a_run_past_its_deadline_is_failed_never_a_result() -> None:
    client, gate, repo = _held(deadline=timedelta(milliseconds=200))
    r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    done = poll(client, r.headers["location"], AUTH_A)
    body = done.json()
    assert body["status"] == "FAILED" and body["failure_code"] == "timeout"
    assert "record" not in body and "recommendation" not in done.text
    assert repo.get(r.json()["id"]) is None
    gate.release.set()
    assert repo.get(r.json()["id"]) is None  # a late finish stores nothing


def test_a_failed_key_is_refused_not_restarted() -> None:
    client, gate, repo = _held(deadline=timedelta(milliseconds=200))
    r = client.post("/v1/assessments", json=PLAN, headers=H)
    poll(client, r.headers["location"], AUTH_A)
    again = client.post("/v1/assessments", json=PLAN, headers=H)
    assert again.status_code == 409 and again.json()["detail"].startswith("nothing was recorded")
    assert repo.get(r.json()["id"]) is None
    gate.release.set()


def test_a_run_that_lost_its_lease_stores_nothing() -> None:
    client, gate, repo = _held()
    r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    rid = r.json()["id"]
    _wait(gate.entered)
    repo._runs[rid] = repo._runs[rid].model_copy(
        update={"lease_expires_at": repo._runs[rid].created_at - timedelta(seconds=1)}
    )
    gate.release.set()
    done = poll(client, r.headers["location"], AUTH_A)
    assert done.json()["status"] == "FAILED" and repo.get(rid) is None


def test_the_status_view_has_no_field_that_could_carry_an_outcome() -> None:
    from divesafe.api.schemas import RunStatusView

    fields = set(RunStatusView.model_fields)
    assert fields == {"id", "status", "created_at", "failure_code", "status_note", "notice"}
    assert RunStatusView.model_json_schema()["properties"]["status"]["enum"] == [
        "RUNNING",
        "FAILED",
    ]


def test_shutdown_cancels_a_running_run_and_marks_it_failed_not_stored() -> None:
    client, gate, repo = _held(live=False, shutdown_grace=timedelta(milliseconds=50))
    with client:
        r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
        rid = r.json()["id"]
        _wait(gate.entered)
    run = repo._runs[rid]
    assert run.state is RunState.FAILED and run.failure_code is FailureCode.SHUTDOWN
    assert repo.get(rid) is None


def test_a_create_during_shutdown_is_refused_and_stores_nothing() -> None:
    client, gate, repo = _held()
    client.app.state.runner.closing = True
    r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    assert r.status_code == 503 and "nothing was recorded" in r.json()["detail"]
    assert repo._runs == {}
    gate.release.set()


def test_a_planned_start_in_the_past_is_refused_before_a_run_is_created() -> None:
    client, gate, repo = _held()
    past = {**PLAN, "planned_start": (T0 - timedelta(hours=1)).isoformat()}
    assert client.post("/v1/assessments", json=past, headers=AUTH_A).status_code == 422
    assert repo._runs == {}
    gate.release.set()


def test_an_unfinished_connector_becomes_an_issue_not_a_hang() -> None:
    from datetime import UTC, datetime

    from divesafe.data import DiveSite
    from divesafe.services import gather_evidence

    class _Stuck:
        name = "stuck"

        async def fetch(self, *args: object) -> Any:
            await asyncio.sleep(30)

    site = next(iter(_build()[0].app.state.divesafe.sites.values()))
    assert isinstance(site, DiveSite)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = asyncio.run(
        gather_evidence([_Stuck()], site, now, now + timedelta(hours=1), now, timeout_seconds=0.05)
    )
    assert result.items == () and any("stuck" in i and "Timeout" in i for i in result.issues)


def test_an_unexpected_error_in_a_run_stores_nothing_and_leaks_no_text() -> None:
    import gc
    import logging

    class _Collect(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    class _Broken:
        name = "broken"

        async def fetch(self, *args: object) -> Any:
            raise RuntimeError("secret internal detail")

    # The app replaces the root handlers at start-up, so listen on the loggers that matter.
    sink = _Collect()
    watched = [logging.getLogger(n) for n in ("divesafe", "asyncio")]
    client, _, repo = _build()
    client.app.state.divesafe.connectors = [_Broken()]
    for logger in watched:
        logger.addHandler(sink)
    try:
        done = create_and_poll(client, PLAN, AUTH_A)
        gc.collect()  # a task whose error nobody retrieved is reported when it is collected
    finally:
        for logger in watched:
            logger.removeHandler(sink)
    body = done.json()
    assert body["status"] == "FAILED" and body["failure_code"] == "internal"
    rid = body["id"]
    assert repo.get(rid) is None and repo._runs[rid].state is RunState.FAILED
    assert repo._runs[rid].failure_code is FailureCode.INTERNAL
    failures = [r for r in sink.records if r.getMessage() == "run failed"]
    assert len(failures) == 1 and failures[0].__dict__["error_type"] == "RuntimeError"
    for record in sink.records:
        assert "secret internal detail" not in repr(record.__dict__)
        assert "secret internal detail" not in record.getMessage()
        assert record.exc_info is None and record.exc_text is None
    assert not [r for r in sink.records if r.name == "asyncio" and r.levelno >= logging.ERROR]


def test_evidence_that_goes_stale_during_a_run_is_judged_again_and_nothing_is_dropped() -> None:
    later_start = {**PLAN, "planned_start": "2026-10-08T22:00:00Z"}  # still ahead after the jump
    steady, _, _ = _build()
    before = create_and_poll(steady, later_start, AUTH_A).json()["record"]
    assert not any(r["rationale"].startswith("No fresh") for r in before["rule_results"])

    client, _, _ = _build()
    readings = iter([T0, T0])  # the request check and the run start; later readings are 2 h on
    client.app.state.divesafe.clock = lambda: next(readings, T0 + timedelta(hours=2))
    after = create_and_poll(client, later_start, AUTH_A).json()["record"]

    assert any(
        r["rule_id"] == "evidence.required.marine_warnings"
        and r["rationale"].startswith("No fresh 'marine_warnings'")
        for r in after["rule_results"]
    )
    assert not any(r["rule_id"] == "assessment.window_started" for r in after["rule_results"])
    for earlier in before["rule_results"]:
        assert earlier in after["rule_results"]  # re-judging adds problems, never removes one
    assert after["created_at"] == (T0 + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    assert after["final_recommendation"] == "INSUFFICIENT EVIDENCE"


def test_create_and_poll_end_to_end_still_gives_a_pending_human_result() -> None:
    client, _, _ = _build()
    done = create_and_poll(client, PLAN, AUTH_A)
    body = done.json()
    assert body["status"] == "PENDING_HUMAN" and body["record"]["human_decision"] is None
    assert isinstance(NewRun, type)


def test_a_create_only_key_polling_its_finished_run_cannot_read_the_decision() -> None:
    from tests.safety.test_api_roles_and_limits import HEADERS, PLAN_D, _client

    client, _ = _client()
    mine = HEADERS[Role.ASSESSOR]
    done = create_and_poll(client, PLAN_D, mine)
    rid = done.json()["record"]["id"]
    decided = client.post(
        f"/v1/assessments/{rid}/decision",
        json={"decision": "NO-GO", "rationale": "Secret reasoning."},
        headers=HEADERS[Role.DECIDER],
    )
    assert decided.status_code == 200, decided.text
    again = client.get(f"/v1/assessments/{rid}", headers=mine)
    assert again.status_code == 200 and again.json()["record"]["human_decision"] is None
    assert "Secret reasoning" not in again.text


def test_shutdown_ends_the_runs_before_the_provider_closes() -> None:
    from divesafe.models import FakeProvider

    seen: list[str] = []

    class _Closable(FakeProvider):
        def close(self) -> None:
            seen.extend(r.state.value for r in repo._runs.values())

    client, clock, repo = _build(
        provider=_Closable(), live=False, runs=RunLimits(shutdown_grace=timedelta(milliseconds=50))
    )
    gate = _Gate()
    client.app.state.divesafe.connectors = [gate]
    with client:
        assert client.post("/v1/assessments", json=PLAN, headers=AUTH_A).status_code == 202
        _wait(gate.entered)
    assert seen == ["FAILED"]


def test_a_run_that_finishes_between_the_two_reads_is_not_reported_missing() -> None:
    client, _, repo = _build()
    done = create_and_poll(client, PLAN, AUTH_A)
    rid = done.json()["record"]["id"]
    real_get, calls = repo.get, [0]

    def blind_first(assessment_id: str):  # type: ignore[no-untyped-def]
        calls[0] += 1
        return None if calls[0] == 1 else real_get(assessment_id)

    repo.get = blind_first  # type: ignore[method-assign]
    for path in (f"/v1/assessments/{rid}", f"/v1/assessments/{rid}/evidence"):
        calls[0] = 0
        assert client.get(path, headers=AUTH_A).status_code == 200


def test_a_run_whose_start_passes_before_it_begins_is_failed_as_start_passed() -> None:
    client, _, repo = _build()
    readings = iter([T0])  # the request check; the run itself then sees a start that has passed
    client.app.state.divesafe.clock = lambda: next(readings, T0 + timedelta(hours=2))
    r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    body = poll(client, r.headers["location"], AUTH_A).json()
    assert body["status"] == "FAILED" and body["failure_code"] == "start_passed"
    assert repo.get(r.json()["id"]) is None


def test_a_wrong_site_is_an_internal_failure_not_a_start_that_passed() -> None:
    from divesafe.orchestration import SiteMismatchError, StartPassedError

    assert not issubclass(SiteMismatchError, StartPassedError)
    client, _, repo = _build()
    other = next(iter(client.app.state.divesafe.sites.values())).model_copy(update={"id": "other"})
    client.app.state.divesafe.sites = {PLAN["site_id"]: other}
    r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    body = poll(client, r.headers["location"], AUTH_A).json()
    assert body["status"] == "FAILED" and body["failure_code"] == "internal"
    assert repo.get(r.json()["id"]) is None


def test_the_connector_budget_has_one_source_and_reaches_the_evidence_gather() -> None:
    import inspect

    from divesafe.config.settings import CONNECTOR_BUDGET_SECONDS
    from divesafe.orchestration import assess_dive
    from divesafe.services import gather_evidence

    for function in (assess_dive, gather_evidence):
        name = "connector_timeout_seconds" if function is assess_dive else "timeout_seconds"
        assert inspect.signature(function).parameters[name].default == CONNECTOR_BUDGET_SECONDS


def test_the_pipeline_passes_its_connector_budget_on_to_the_gather() -> None:
    from datetime import UTC, datetime

    from divesafe.domain import DivePlan
    from divesafe.orchestration import assess_dive

    class _Stuck:
        name = "stuck"

        async def fetch(self, *args: object) -> Any:
            await asyncio.sleep(30)

    client, _, _ = _build()
    site = next(iter(client.app.state.divesafe.sites.values()))
    now = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
    run = assess_dive(
        assessment_id="a" * 32,
        plan=DivePlan(**PLAN),
        site=site,
        connectors=[_Stuck()],
        engine=client.app.state.divesafe.engine,
        now=now,
        connector_timeout_seconds=0.05,
    )
    # The ceiling is below the 30 s default, so a budget that is not passed on fails here.
    record = asyncio.run(asyncio.wait_for(run, 5))
    assert any("stuck" in i and "Timeout" in i for i in record.evidence_issues)
    assert record.final_recommendation.value == "INSUFFICIENT EVIDENCE"


def test_a_deadline_shorter_than_the_providers_stage_is_refused_at_start() -> None:
    from divesafe.models import FakeProvider

    class _Slow(FakeProvider):
        stage_timeout_seconds = 900.0

    with pytest.raises(RuntimeError, match="stage timeout"):
        _build(provider=_Slow(), live=False, runs=RunLimits(deadline=timedelta(seconds=210)))
    _build(provider=_Slow(), live=False, runs=RunLimits(deadline=timedelta(seconds=960)))


def test_a_run_that_cannot_store_its_result_is_failed_as_storage_and_stores_nothing() -> None:
    from divesafe.services import RepositoryUnavailableError

    client, _, repo = _build()

    def broken(*args: object, **kwargs: object) -> bool:
        raise RepositoryUnavailableError("down")

    repo.complete = broken  # type: ignore[method-assign]
    r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    body = poll(client, r.headers["location"], AUTH_A).json()
    assert body["status"] == "FAILED" and body["failure_code"] == "storage"
    assert repo.get(r.json()["id"]) is None


def test_a_run_that_cannot_complete_is_failed_and_frees_its_slot() -> None:
    client, _, repo = _build(runs=RunLimits(max_in_flight=1, max_per_actor=1))
    repo.complete = lambda *a, **k: False  # type: ignore[method-assign]
    r = client.post("/v1/assessments", json=PLAN, headers=AUTH_A)
    body = poll(client, r.headers["location"], AUTH_A).json()
    assert body["status"] == "FAILED" and repo.get(r.json()["id"]) is None
    assert client.post("/v1/assessments", json=PLAN, headers=AUTH_A).status_code == 202


def test_a_stranger_gets_the_same_answer_for_a_run_and_a_missing_id_on_every_route() -> None:
    client, gate, _ = _held()
    client.app.state.divesafe.authenticator = ApiKeyAuthenticator(
        {
            _hash("key-for-alice-test-only"): KeyEntry("alice", frozenset(Role)),
            _hash("k-mallory"): KeyEntry("mallory", frozenset({Role.ASSESSOR, Role.DECIDER})),
        }
    )
    try:
        rid = client.post("/v1/assessments", json=PLAN, headers=AUTH_A).json()["id"]
        _wait(gate.entered)
        other, missing = {"Authorization": "Bearer k-mallory"}, "f" * 32
        for tail, body in (
            ("decision", {"decision": "GO"}),
            ("actual-conditions", {"observations": {"v": 1}}),
        ):
            a = client.post(f"/v1/assessments/{rid}/{tail}", json=body, headers=other)
            b = client.post(f"/v1/assessments/{missing}/{tail}", json=body, headers=other)
            assert (a.status_code, a.json()) == (b.status_code, b.json())
    finally:
        gate.release.set()


def test_a_non_viewer_cannot_read_another_actors_finished_record() -> None:
    from tests.safety.test_api_roles_and_limits import HEADERS, PLAN_D, _client

    client, make = _client()
    theirs = make()
    mine = HEADERS[Role.ASSESSOR]
    assert client.get(f"/v1/assessments/{theirs}", headers=mine).status_code == 404
    assert create_and_poll(client, PLAN_D, mine).status_code == 200


def test_the_status_notes_are_the_fixed_reviewed_wording() -> None:
    from divesafe.api.schemas import _RUN_NOTES

    assert _RUN_NOTES[RunState.RUNNING] == "No result yet. Nothing has been assessed. Do not act."
    failed = _RUN_NOTES[RunState.FAILED]
    assert "not a result and not INSUFFICIENT EVIDENCE" in failed
    assert "nothing was assessed" in failed


def test_a_timeout_raised_inside_the_work_is_internal_not_the_run_deadline() -> None:
    client, _, repo = _build()

    def boom(*args: object, **kwargs: object) -> Any:
        raise TimeoutError("a library timed out")

    client.app.state.divesafe.engine.assess = boom
    body = create_and_poll(client, PLAN, AUTH_A).json()
    assert body["status"] == "FAILED" and body["failure_code"] == "internal"
    assert repo._runs[body["id"]].failure_code is FailureCode.INTERNAL
    assert repo.get(body["id"]) is None


@pytest.mark.parametrize("bad", [0, -1, -0.001, float("nan"), float("inf"), 30.001, 3600], ids=str)
def test_a_connector_budget_that_is_not_positive_finite_and_within_the_ceiling_is_refused(
    bad: float,
) -> None:
    from datetime import UTC, datetime

    from divesafe.domain import DivePlan
    from divesafe.orchestration import assess_dive
    from divesafe.services.evidence import gather_evidence

    client, _, _ = _build()
    site = next(iter(client.app.state.divesafe.sites.values()))
    now = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
    soon = now + timedelta(hours=1)
    with pytest.raises(ValueError, match="connector budget"):
        asyncio.run(gather_evidence([], site, now, soon, now, timeout_seconds=bad))
    with pytest.raises(ValueError, match="connector budget"):
        asyncio.run(
            assess_dive(
                assessment_id="a" * 32,
                plan=DivePlan(**PLAN),
                site=site,
                connectors=[],
                engine=client.app.state.divesafe.engine,
                now=now,
                connector_timeout_seconds=bad,
            )
        )


def test_the_full_connector_budget_and_a_small_one_are_accepted() -> None:
    from datetime import UTC, datetime

    from divesafe.config.settings import CONNECTOR_BUDGET_SECONDS
    from divesafe.services.evidence import gather_evidence

    client, _, _ = _build()
    site = next(iter(client.app.state.divesafe.sites.values()))
    now = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
    for ok in (0.001, CONNECTOR_BUDGET_SECONDS):
        got = asyncio.run(gather_evidence([], site, now, now, now, timeout_seconds=ok))
        assert got.items == ()
