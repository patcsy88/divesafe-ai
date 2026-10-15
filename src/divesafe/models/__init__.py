"""Model abstractions: the LLM provider interface (and, later, the statistical risk model)."""

from divesafe.models import ollama as _ollama  # noqa: F401,E402  (registers the adapter)
from divesafe.models import openai_compatible as _openai_compatible  # noqa: F401,E402
from divesafe.models.llm import (
    ChatMessage,
    FakeProvider,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderNotAvailableError,
    TokenLimitError,
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
    "TokenLimitError",
    "create_provider",
    "register_provider",
]
