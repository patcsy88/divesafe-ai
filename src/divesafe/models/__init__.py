"""Model abstractions: the LLM provider interface (and, later, the statistical risk model)."""

from divesafe.models import ollama as _ollama  # noqa: F401,E402  (registers the adapter)
from divesafe.models.llm import (
    ChatMessage,
    FakeProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderNotAvailableError,
    create_provider,
    register_provider,
)

__all__ = [
    "ChatMessage",
    "FakeProvider",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "ProviderNotAvailableError",
    "create_provider",
    "register_provider",
]
