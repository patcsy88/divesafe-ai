from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from divesafe.api.app import create_app
from divesafe.config import Settings
from divesafe.models import (
    ChatMessage,
    LLMRequest,
    ProviderNotAvailableError,
    create_provider,
)


def test_health() -> None:
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_fake_provider_is_selectable_by_settings() -> None:
    provider = create_provider(Settings(llm_provider="fake"))
    request = LLMRequest(messages=(ChatMessage(role="user", content="hi"),))
    assert asyncio.run(provider.complete(request)).provider == "fake"


def test_unregistered_provider_fails_loudly() -> None:
    with pytest.raises(ProviderNotAvailableError):
        create_provider(Settings(llm_provider="openai"))
