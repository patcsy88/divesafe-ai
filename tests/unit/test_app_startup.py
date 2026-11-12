from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from divesafe.api.app import create_app
from divesafe.config import Settings
from divesafe.models import FakeProvider, ProviderNotAvailableError
from divesafe.models import llm as llm_module

_KEYS = '{"' + "a" * 64 + '": {"actor": "ops", "roles": ["viewer", "decider"]}}'


def _settings(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


def _production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(llm_module._REGISTRY, "openai_compatible", lambda s: FakeProvider())
    monkeypatch.setattr(
        "divesafe.api.app.get_settings",
        lambda: _settings(
            environment="production",
            llm_provider="openai_compatible",
            llm_base_url="https://llm.example/v1",
            api_key_hashes=_KEYS,
        ),
    )
    monkeypatch.setattr("divesafe.api.state.create_provider", lambda s: FakeProvider())


def test_production_refuses_non_durable_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_app`'s own refusal, reached with a knowledge connector present: since the owner
    decision of 2026-11-12 a production start-up without a database is refused earlier, in
    `build_default_state`, for the missing knowledge connector (next test)."""
    _production(monkeypatch)
    monkeypatch.setattr("divesafe.api.state.build_knowledge_connector", lambda s: object())
    with pytest.raises(RuntimeError, match="non-durable"):
        create_app()


def test_production_without_a_database_is_refused_for_the_knowledge_connector_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production(monkeypatch)
    with pytest.raises(RuntimeError, match="without the knowledge connector"):
        create_app()


def test_a_real_provider_without_an_adapter_fails_loudly_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("divesafe.api.app.get_settings", lambda: _settings(llm_provider="openai"))
    with pytest.raises(ProviderNotAvailableError):
        create_app()


def test_docs_are_available_in_development() -> None:
    assert TestClient(create_app()).get("/openapi.json").status_code == 200
