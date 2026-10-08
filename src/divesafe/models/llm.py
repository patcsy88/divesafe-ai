"""Provider-neutral LLM interface.

Agents depend only on `LLMProvider`. Concrete adapters (OpenAI, Anthropic, Ollama or any
compatible server) are added later behind `register_provider`, so no vendor SDK is imported
here and the application is never hard-wired to one vendor.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from divesafe.config.settings import Settings


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[ChatMessage, ...]
    temperature: float = 0.0
    max_output_tokens: int | None = None
    json_schema: dict[str, object] | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    provider: str
    model: str


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class ProviderNotAvailableError(RuntimeError):
    """Raised when the configured provider has no registered adapter."""


ProviderFactory = Callable[[Settings], LLMProvider]
_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    _REGISTRY[name] = factory


def create_provider(settings: Settings) -> LLMProvider:
    try:
        factory = _REGISTRY[settings.llm_provider]
    except KeyError:
        raise ProviderNotAvailableError(
            f"No adapter registered for LLM provider '{settings.llm_provider}'."
        ) from None
    return factory(settings)


class FakeProvider:
    """Deterministic provider for tests and offline development."""

    name = "fake"

    def __init__(self, reply: str = "", model: str = "fake-model") -> None:
        self.model = model
        self._reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self._reply, provider=self.name, model=self.model)


register_provider("fake", lambda settings: FakeProvider(model=settings.llm_model or "fake-model"))
