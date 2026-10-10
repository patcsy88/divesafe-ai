from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi import Request

from divesafe.api.auth import (
    ApiKeyAuthenticator,
    DevAuthenticator,
    KeyEntry,
    Role,
    build_authenticator,
    parse_api_key_hashes,
)
from divesafe.config import Settings
from divesafe.data import CachingRateLimitedGetter, ConnectorTransportError


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Inner:
    def __init__(self) -> None:
        self.calls = 0

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        self.calls += 1
        return {"n": self.calls}


def _get(g: CachingRateLimitedGetter, url: str = "https://api.data.gov.my/x", **p: str) -> Any:
    return asyncio.run(g.get_json(url, p))


def test_identical_requests_within_ttl_are_served_from_cache() -> None:
    inner, clock = _Inner(), _Clock()
    g = CachingRateLimitedGetter(inner, clock=clock, cache_ttl_seconds=60)
    assert _get(g, a="1") == {"n": 1}
    clock.t += 5
    assert _get(g, a="1") == {"n": 1}
    assert inner.calls == 1


def test_a_different_request_too_soon_fails_closed_then_succeeds_later() -> None:
    inner, clock = _Inner(), _Clock()
    g = CachingRateLimitedGetter(inner, clock=clock, min_interval_seconds={"api.data.gov.my": 15})
    _get(g, a="1")
    clock.t += 5
    with pytest.raises(ConnectorTransportError):
        _get(g, a="2")
    clock.t += 11
    assert _get(g, a="2") == {"n": 2}


def test_cache_expires_and_never_serves_old_data() -> None:
    inner, clock = _Inner(), _Clock()
    g = CachingRateLimitedGetter(inner, clock=clock, cache_ttl_seconds=60, min_interval_seconds={})
    _get(g, a="1")
    clock.t += 61
    assert _get(g, a="1") == {"n": 2}


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


KEY = "s3cret-test-key"
HASH = hashlib.sha256(KEY.encode()).hexdigest()


def test_api_key_accepts_only_the_right_bearer_key() -> None:
    auth = ApiKeyAuthenticator({HASH: KeyEntry("alice", frozenset({Role.VIEWER}))})
    assert auth.authenticate(_request({"Authorization": f"Bearer {KEY}"})).actor == "alice"  # type: ignore[union-attr]
    assert auth.authenticate(_request({"Authorization": "Bearer wrong"})) is None
    assert auth.authenticate(_request({"Authorization": f"Basic {KEY}"})) is None
    assert auth.authenticate(_request({})) is None
    assert auth.authenticate(_request({"X-Dev-Actor": "mallory"})) is None


def test_no_configured_keys_rejects_everything() -> None:
    auth = build_authenticator(Settings(auth_mode="api_key"))
    assert auth.authenticate(_request({"Authorization": f"Bearer {KEY}"})) is None


def test_dev_mode_reads_actor_header_and_rejects_blank() -> None:
    assert DevAuthenticator().authenticate(_request({"X-Dev-Actor": "bob"})).actor == "bob"  # type: ignore[union-attr]
    assert DevAuthenticator().authenticate(_request({"X-Dev-Actor": "  "})) is None


def test_dev_mode_grants_every_role_and_api_key_mode_only_the_configured_ones() -> None:
    dev = DevAuthenticator().authenticate(_request({"X-Dev-Actor": "bob"}))
    assert dev is not None and dev.roles == frozenset(Role)
    keyed = ApiKeyAuthenticator({HASH: KeyEntry("alice", frozenset({Role.VIEWER}))})
    principal = keyed.authenticate(_request({"Authorization": f"Bearer {KEY}"}))
    assert principal is not None and principal.roles == frozenset({Role.VIEWER})


def test_dev_mode_is_refused_in_production() -> None:
    with pytest.raises(ValueError):
        Settings(environment="production", auth_mode="dev", llm_provider="ollama")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"short": "a"}',
        f'{{"{HASH}": "alice"}}',
        f'{{"{HASH}": 5}}',
        f'{{"{HASH}": {{"actor": "a", "roles": ["decider"]}}}}',
        f'{{"{HASH}": {{"actor": "", "roles": ["viewer"]}}}}',
        f'{{"{HASH}": {{"actor": "a", "roles": []}}}}',
        f'{{"{HASH}": {{"actor": "a", "roles": ["admin"]}}}}',
        f'{{"{HASH}": {{"actor": "a", "roles": "viewer"}}}}',
        f'{{"{HASH}": {{"actor": "a"}}}}',
        f'{{"{HASH}": {{"actor": "a", "roles": ["viewer"], "extra": 1}}}}',
    ],
)
def test_malformed_key_hash_config_fails_at_startup(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_api_key_hashes(raw)


def test_key_hash_config_round_trips() -> None:
    raw = f'{{"{HASH}": {{"actor": "alice", "roles": ["viewer", "decider"]}}}}'
    assert parse_api_key_hashes(raw) == {
        HASH: KeyEntry("alice", frozenset({Role.VIEWER, Role.DECIDER}))
    }


def test_cached_payload_cannot_be_corrupted_by_a_caller() -> None:
    inner, clock = _Inner(), _Clock()
    g = CachingRateLimitedGetter(inner, clock=clock, cache_ttl_seconds=60, min_interval_seconds={})
    first = _get(g, a="1")
    first["n"] = 999
    assert _get(g, a="1") == {"n": 1}


# --- combined provider quota (Open-Meteo documents 600/min, 5,000/hour, 10,000/day) -------------

from divesafe.data import OPEN_METEO_QUOTA, QuotaGroup  # noqa: E402

MARINE = "https://marine-api.open-meteo.com/v1/marine"
FORECAST = "https://api.open-meteo.com/v1/forecast"


def _free(clock: _Clock, **extra: Any) -> CachingRateLimitedGetter:
    return CachingRateLimitedGetter(
        _Inner(), clock=clock, cache_ttl_seconds=0, min_interval_seconds={}, **extra
    )


def _call(g: CachingRateLimitedGetter, url: str, n: int) -> Any:
    return asyncio.run(g.get_json(url, {"n": str(n)}))


def test_the_documented_open_meteo_limits_are_the_defaults() -> None:
    assert OPEN_METEO_QUOTA.limits == ((60.0, 600), (3600.0, 5000), (86400.0, 10000))
    assert {"marine-api.open-meteo.com", "api.open-meteo.com"} <= OPEN_METEO_QUOTA.hosts


def test_the_two_open_meteo_hosts_share_one_budget() -> None:
    clock = _Clock()
    group = QuotaGroup("t", OPEN_METEO_QUOTA.hosts, ((60.0, 4),))
    g = _free(clock, quotas=(group,))
    for i in range(2):
        _call(g, MARINE, i)
        _call(g, FORECAST, i)
    with pytest.raises(ConnectorTransportError, match="budget"):
        _call(g, FORECAST, 99)
    with pytest.raises(ConnectorTransportError, match="budget"):
        _call(g, MARINE, 99)


def test_the_budget_slides_and_recovers() -> None:
    clock = _Clock()
    g = _free(clock, quotas=(QuotaGroup("t", OPEN_METEO_QUOTA.hosts, ((60.0, 2),)),))
    _call(g, MARINE, 1)
    _call(g, MARINE, 2)
    with pytest.raises(ConnectorTransportError):
        _call(g, MARINE, 3)
    clock.t += 61
    assert _call(g, MARINE, 4)  # the earlier calls have left the window


def test_every_window_is_enforced_not_just_the_shortest() -> None:
    clock = _Clock()
    g = _free(clock, quotas=(QuotaGroup("t", OPEN_METEO_QUOTA.hosts, ((60.0, 10), (3600.0, 3))),))
    for i in range(3):
        _call(g, MARINE, i)
        clock.t += 120  # each call is outside the per-minute window of the others
    with pytest.raises(ConnectorTransportError):
        _call(g, MARINE, 9)  # but the hourly budget is spent


def test_cache_hits_do_not_spend_budget() -> None:
    clock = _Clock()
    g = CachingRateLimitedGetter(
        _Inner(),
        clock=clock,
        cache_ttl_seconds=60,
        min_interval_seconds={},
        quotas=(QuotaGroup("t", OPEN_METEO_QUOTA.hosts, ((60.0, 1),)),),
    )
    first = _call(g, MARINE, 1)
    for _ in range(5):
        assert _call(g, MARINE, 1) == first  # served from cache, budget untouched


def test_other_hosts_are_not_charged_to_the_open_meteo_budget() -> None:
    clock = _Clock()
    g = _free(clock, quotas=(QuotaGroup("t", OPEN_METEO_QUOTA.hosts, ((60.0, 1),)),))
    _call(g, MARINE, 1)
    for i in range(5):
        _call(g, "https://api.data.gov.my/weather/warning/", i)


def test_a_blocked_call_never_reaches_the_provider() -> None:
    clock = _Clock()
    inner = _Inner()
    g = CachingRateLimitedGetter(
        inner,
        clock=clock,
        cache_ttl_seconds=0,
        min_interval_seconds={},
        quotas=(QuotaGroup("t", OPEN_METEO_QUOTA.hosts, ((60.0, 1),)),),
    )
    _call(g, MARINE, 1)
    with pytest.raises(ConnectorTransportError):
        _call(g, MARINE, 2)
    assert inner.calls == 1
