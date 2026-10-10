"""Roles and per-actor rate limits over HTTP. A key may do only what its roles allow."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from tests.safety.test_api_human_gate import (
    PLAN,
    _build,
    _hash,
)

from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role, parse_api_key_hashes
from divesafe.api.ratelimit import Limit, RateLimiter
from divesafe.api.state import default_limits

pytestmark = pytest.mark.safety

PLAN_D = {**PLAN, "decider": "decider-user"}
KEYS = {r: f"key-{r.value}-test-only" for r in Role}
HEADERS = {r: {"Authorization": f"Bearer {k}"} for r, k in KEYS.items()}


def _client(limits: dict[str, Limit] | None = None) -> tuple[TestClient, Callable[[], str]]:
    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    entries = {_hash(KEYS[r]): KeyEntry(f"{r.value}-user", frozenset({r})) for r in Role}
    entries[_hash("key-all")] = KeyEntry("everyone", frozenset(Role))
    state.authenticator = ApiKeyAuthenticator(entries)
    state.limiter = RateLimiter(limits or default_limits())
    return client, lambda: client.post(
        "/v1/assessments", json=PLAN_D, headers={"Authorization": "Bearer key-all"}
    ).json()["record"]["id"]


def test_a_viewer_can_read_but_cannot_create_or_decide() -> None:
    client, make = _client()
    aid = make()
    h = HEADERS[Role.VIEWER]
    assert client.get(f"/v1/assessments/{aid}", headers=h).status_code == 200
    assert client.get(f"/v1/assessments/{aid}/evidence", headers=h).status_code == 200
    assert client.get("/v1/sites/my-pahang-pulau-tioman", headers=h).status_code == 200
    assert client.post("/v1/assessments", json=PLAN_D, headers=h).status_code == 403
    decision = {"decision": "INSUFFICIENT EVIDENCE"}
    assert (
        client.post(f"/v1/assessments/{aid}/decision", json=decision, headers=h).status_code == 403
    )
    obs = {"observations": {"v": 1}}
    assert (
        client.post(f"/v1/assessments/{aid}/actual-conditions", json=obs, headers=h).status_code
        == 403
    )


def test_only_a_decider_can_record_the_human_decision() -> None:
    client, make = _client()
    aid = make()
    url = f"/v1/assessments/{aid}/decision"
    body = {"decision": "INSUFFICIENT EVIDENCE"}
    for role in (Role.VIEWER, Role.ASSESSOR):
        assert client.post(url, json=body, headers=HEADERS[role]).status_code == 403
    everyone = {"Authorization": "Bearer key-all"}
    stored = client.get(f"/v1/assessments/{aid}", headers=everyone).json()
    assert stored["record"]["human_decision"] is None
    assert client.post(url, json=body, headers=HEADERS[Role.DECIDER]).status_code == 200


def test_an_assessor_can_create_but_not_read_or_decide() -> None:
    client, make = _client()
    aid = make()
    h = HEADERS[Role.ASSESSOR]
    assert client.post("/v1/assessments", json=PLAN_D, headers=h).status_code == 201
    assert client.get(f"/v1/assessments/{aid}", headers=h).status_code == 403


def test_a_decider_cannot_create_assessments() -> None:
    client, _ = _client()
    assert (
        client.post("/v1/assessments", json=PLAN_D, headers=HEADERS[Role.DECIDER]).status_code
        == 403
    )


def test_the_decision_actor_is_the_key_holder_not_the_role_name() -> None:
    client, make = _client()
    aid = make()
    done = client.post(
        f"/v1/assessments/{aid}/decision",
        json={"decision": "INSUFFICIENT EVIDENCE"},
        headers=HEADERS[Role.DECIDER],
    )
    assert done.json()["record"]["human_decision"]["decided_by"] == "decider-user"


def test_authentication_is_checked_before_the_role_and_a_bad_key_is_401_not_403() -> None:
    client, _ = _client()
    assert client.post("/v1/assessments", json=PLAN).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/v1/assessments", json=PLAN_D, headers=bad).status_code == 401


def test_health_stays_open() -> None:
    client, _ = _client()
    assert client.get("/health").status_code == 200


# --- rate limits ---------------------------------------------------------------------------------


def test_creating_assessments_is_limited_per_actor_with_retry_after() -> None:
    client, _ = _client(default_limits(create_per_minute=2))
    h = HEADERS[Role.ASSESSOR]
    assert [
        client.post("/v1/assessments", json=PLAN_D, headers=h).status_code for _ in range(2)
    ] == [
        201,
        201,
    ]
    limited = client.post("/v1/assessments", json=PLAN_D, headers=h)
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert limited.json() == {
        "detail": "nothing was recorded; an assessment without a recorded decision is still "
        "pending. too many requests; slow down"
    }


def test_one_actors_limit_does_not_throttle_another() -> None:
    client, _ = _client(default_limits(create_per_minute=1))
    assert (
        client.post("/v1/assessments", json=PLAN_D, headers=HEADERS[Role.ASSESSOR]).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/assessments", json=PLAN_D, headers={"Authorization": "Bearer key-all"}
        ).status_code
        == 201
    )


def test_a_forbidden_request_is_not_counted_against_the_limit() -> None:
    client, _ = _client(default_limits(create_per_minute=1))
    for _ in range(5):
        assert (
            client.post("/v1/assessments", json=PLAN_D, headers=HEADERS[Role.VIEWER]).status_code
            == 403
        )
    assert (
        client.post("/v1/assessments", json=PLAN_D, headers=HEADERS[Role.ASSESSOR]).status_code
        == 201
    )


def test_unauthenticated_requests_cannot_exhaust_an_actors_limit() -> None:
    client, _ = _client(default_limits(create_per_minute=1))
    for _ in range(5):
        assert client.post("/v1/assessments", json=PLAN).status_code == 401
    assert (
        client.post("/v1/assessments", json=PLAN_D, headers=HEADERS[Role.ASSESSOR]).status_code
        == 201
    )


def test_a_limited_decision_attempt_records_nothing() -> None:
    client, make = _client(default_limits(decide_per_minute=1))
    first, second = make(), make()
    h = HEADERS[Role.DECIDER]
    body = {"decision": "INSUFFICIENT EVIDENCE"}
    assert client.post(f"/v1/assessments/{first}/decision", json=body, headers=h).status_code == 200
    assert (
        client.post(f"/v1/assessments/{second}/decision", json=body, headers=h).status_code == 429
    )
    reader = {"Authorization": "Bearer key-all"}
    assert (
        client.get(f"/v1/assessments/{second}", headers=reader).json()["record"]["human_decision"]
        is None
    )


# --- the limiter itself --------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_the_window_slides_and_recovers() -> None:
    clock = _Clock()
    limiter = RateLimiter({"k": Limit(2, 60)}, clock=clock)
    assert limiter.check("k", "a") is None
    clock.t = 10
    assert limiter.check("k", "a") is None
    assert limiter.check("k", "a") == 50
    clock.t = 60  # the first hit has left the window
    assert limiter.check("k", "a") is None
    assert limiter.check("k", "a") is not None


def test_denied_requests_are_not_counted_so_waiting_always_recovers() -> None:
    clock = _Clock()
    limiter = RateLimiter({"k": Limit(1, 60)}, clock=clock)
    assert limiter.check("k", "a") is None
    for _ in range(100):
        assert limiter.check("k", "a") is not None
    clock.t = 60
    assert limiter.check("k", "a") is None


def test_limit_keys_are_independent() -> None:
    limiter = RateLimiter({"x": Limit(1, 60), "y": Limit(1, 60)}, clock=_Clock())
    assert limiter.check("x", "a") is None
    assert limiter.check("y", "a") is None


def test_memory_is_bounded_when_many_actors_pass_through() -> None:
    clock = _Clock()
    limiter = RateLimiter({"k": Limit(1, 60)}, clock=clock, max_tracked=100)
    for i in range(1000):
        clock.t += 61  # every earlier hit has expired
        limiter.check("k", f"actor-{i}")
    assert len(limiter._hits) <= 101  # noqa: SLF001


def test_an_unknown_limit_key_fails_loudly_rather_than_allowing() -> None:
    with pytest.raises(KeyError):
        RateLimiter({"k": Limit(1, 60)}).check("typo", "a")


def test_a_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Limit(0, 60)
    with pytest.raises(ValueError):
        Limit(1, 0)


# --- gaps found in review ------------------------------------------------------------------------


def test_every_route_except_health_requires_a_role_and_a_limit() -> None:
    from fastapi.routing import APIRoute

    client, _ = _client()
    open_paths = {"/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
    checked = 0
    for route in client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute) or route.path in open_paths:
            continue
        calls = [d.call for d in route.dependant.dependencies]
        assert any(getattr(c, "__qualname__", "").startswith("require_role") for c in calls), (
            route.path
        )
        checked += 1
    assert checked >= 6


def test_a_non_decider_cannot_decide_override_or_report_even_with_a_bad_body() -> None:
    client, make = _client()
    aid = make()
    override = {"decision": "GO", "rationale": "I know better"}
    for role in (Role.VIEWER, Role.ASSESSOR):
        h = HEADERS[role]
        for url, body in (
            (f"/v1/assessments/{aid}/decision", override),
            (f"/v1/assessments/{aid}/actual-conditions", {"observations": {"v": 1}}),
        ):
            assert client.post(url, json=body, headers=h).status_code == 403
        assert client.post(f"/v1/assessments/{aid}/decision", json={}, headers=h).status_code in (
            403,
            422,
        )
    everyone = {"Authorization": "Bearer key-all"}
    stored = client.get(f"/v1/assessments/{aid}", headers=everyone).json()["record"]
    assert stored["human_decision"] is None and stored["actual_conditions"] is None


def test_actual_conditions_are_rate_limited_and_share_the_decide_bucket() -> None:
    client, make = _client(default_limits(decide_per_minute=1))
    aid = make()
    h = HEADERS[Role.DECIDER]
    body = {"decision": "INSUFFICIENT EVIDENCE"}
    assert client.post(f"/v1/assessments/{aid}/decision", json=body, headers=h).status_code == 200
    url = f"/v1/assessments/{aid}/actual-conditions"
    assert client.post(url, json={"observations": {"v": 1}}, headers=h).status_code == 429


def test_a_forbidden_response_says_nothing_was_recorded_and_the_gate_is_still_pending() -> None:
    client, make = _client()
    aid = make()
    refused = client.post(
        f"/v1/assessments/{aid}/decision",
        json={"decision": "GO", "rationale": "x"},
        headers=HEADERS[Role.VIEWER],
    )
    assert refused.status_code == 403
    assert "nothing was recorded" in refused.text and "still pending" in refused.text


def test_a_live_counter_survives_an_eviction_pass_and_still_limits() -> None:
    clock = _Clock()
    limiter = RateLimiter({"k": Limit(1, 60)}, clock=clock, max_tracked=2)
    assert limiter.check("k", "victim") is None
    for i in range(10):  # many live counters force eviction passes
        limiter.check("k", f"other-{i}")
    assert limiter.check("k", "victim") is not None


def test_a_misspelt_limit_key_fails_at_build_time_not_per_request() -> None:
    from divesafe.api.auth import require_role

    with pytest.raises(ValueError):
        require_role(Role.VIEWER, limit_key="raed")


# --- key configuration is refused, never guessed ------------------------------------------------

H1, H2 = "a" * 64, "b" * 64


@pytest.mark.parametrize(
    "raw",
    [
        f'{{"{H1}": "alice"}}',
        f'{{"{H1}": {{"actor": "alice", "roles": []}}}}',
        f'{{"{H1}": {{"actor": "alice", "roles": ["root"]}}}}',
        f'{{"{H1}": {{"actor": "alice", "roles": ["decider"]}}}}',
        f'{{"{H1}": {{"actor": "alice", "roles": ["viewer"]}}, "{H2}": '
        f'{{"actor": "Alice", "roles": ["viewer"]}}}}',
        f'{{"{H1}": {{"actor": "a", "roles": ["viewer"]}}, "{H1}": '
        f'{{"actor": "b", "roles": ["viewer", "decider"]}}}}',
        f'{{"{H1}\n": {{"actor": "alice", "roles": ["viewer"]}}}}',
    ],
)
def test_unsafe_key_configuration_is_refused(raw: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse_api_key_hashes(raw)
    assert H1 not in str(caught.value)
