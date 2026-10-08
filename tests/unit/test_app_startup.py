from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from divesafe.api.app import create_app
from divesafe.config import Settings


def test_production_refuses_non_durable_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "divesafe.api.app.get_settings",
        lambda: Settings(environment="production", llm_provider="ollama"),
    )
    with pytest.raises(RuntimeError, match="non-durable"):
        create_app()


def test_docs_are_available_in_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert TestClient(create_app()).get("/openapi.json").status_code == 200
