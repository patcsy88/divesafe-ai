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
