from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from divesafe.api.app import create_app
from divesafe.config import Settings
from divesafe.models import FakeProvider, ProviderNotAvailableError
from divesafe.models import llm as llm_module


def _settings(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


def test_production_refuses_non_durable_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(llm_module._REGISTRY, "ollama", lambda s: FakeProvider())
    monkeypatch.setattr(
        "divesafe.api.app.get_settings",
        lambda: _settings(environment="production", llm_provider="ollama"),
    )
    monkeypatch.setattr("divesafe.api.state.create_provider", lambda s: FakeProvider())
    with pytest.raises(RuntimeError, match="non-durable"):
        create_app()


def test_a_real_provider_without_an_adapter_fails_loudly_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("divesafe.api.app.get_settings", lambda: _settings(llm_provider="openai"))
    with pytest.raises(ProviderNotAvailableError):
        create_app()


def test_docs_are_available_in_development() -> None:
    assert TestClient(create_app()).get("/openapi.json").status_code == 200
