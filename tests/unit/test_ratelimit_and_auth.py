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
    auth = ApiKeyAuthenticator({HASH: "alice"})
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


def test_dev_mode_is_refused_in_production() -> None:
    with pytest.raises(ValueError):
        Settings(environment="production", auth_mode="dev", llm_provider="ollama")


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", '{"short": "a"}', f'{{"{HASH}": ""}}', f'{{"{HASH}": 5}}'],
)
def test_malformed_key_hash_config_fails_at_startup(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_api_key_hashes(raw)


def test_key_hash_config_round_trips() -> None:
    assert parse_api_key_hashes(f'{{"{HASH}": "alice"}}') == {HASH: "alice"}


def test_cached_payload_cannot_be_corrupted_by_a_caller() -> None:
    inner, clock = _Inner(), _Clock()
    g = CachingRateLimitedGetter(inner, clock=clock, cache_ttl_seconds=60, min_interval_seconds={})
    first = _get(g, a="1")
    first["n"] = 999
    assert _get(g, a="1") == {"n": 1}
