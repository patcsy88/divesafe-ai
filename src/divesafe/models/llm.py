"""Provider-neutral LLM interface.

Agents depend only on `LLMProvider`. Concrete adapters (OpenAI, Anthropic, or any
compatible server) are added later behind `register_provider`, so no vendor SDK is imported
here and the application is never hard-wired to one vendor.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from divesafe.config.settings import Settings


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[ChatMessage, ...]
    temperature: float = 0.0
    max_output_tokens: int | None = Field(default=None, ge=1, le=8000)
    json_schema: dict[str, object] | None = None


# Larger than any real reply or token count; a bigger "count" is treated as absent (None).
MAX_LOGGED_COUNT = 10**9


def plain_count(value: object) -> int | None:
    """`value` if it is a plain `int` from 0 to `MAX_LOGGED_COUNT` (never a bool, a float, a
    string or an int subclass); otherwise None. Never raises: a count is for logging only and
    decides nothing."""
    return value if type(value) is int and 0 <= value <= MAX_LOGGED_COUNT else None


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    provider: str
    model: str
    # The server's own count of output tokens, when it sent one (ADR 0018 amendment 2026-11-15).
    # Logged only; it never changes whether a reply is accepted. Strict: a bool is not a count.
    output_tokens: int | None = Field(default=None, strict=True, ge=0, le=MAX_LOGGED_COUNT)


class LLMProvider(Protocol):
    name: str
    model: str
    # True when the server's address is not on this machine (hosted APIs). False does NOT show the
    # prompt stays here: a loopback proxy or tunnel can forward it (ADR 0023). Callers must send
    # only public evidence and plan data, never actor identities or free-text decision content.
    external: bool

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class TokenLimitError(RuntimeError):
    """The model stopped at the output-token limit, so its reply is cut off and is refused.
    The message is fixed; no reply text is carried. `output_chars` (characters joined before the
    stop) and `output_tokens` (the server's usage count) are plain ints or None, for logging."""

    output_chars: int | None
    output_tokens: int | None

    def __init__(self, *, output_chars: object = None, output_tokens: object = None) -> None:
        super().__init__("the model stopped at the output-token limit")
        self.output_chars = plain_count(output_chars)
        self.output_tokens = plain_count(output_tokens)

    def __reduce__(
        self,
    ) -> tuple[type[TokenLimitError], tuple[()], dict[str, int | None]]:
        state = {
            "output_chars": plain_count(vars(self).get("output_chars")),
            "output_tokens": plain_count(vars(self).get("output_tokens")),
        }
        return (type(self), (), state)


class ProviderNotAvailableError(RuntimeError):
    """Raised when the configured provider has no registered adapter."""


ProviderFactory = Callable[[Settings], LLMProvider]
_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Import-time registration. Re-registering a name is refused."""
    if name in _REGISTRY:
        raise ValueError(f"LLM provider '{name}' is already registered")
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
    external = False
    # No server (ADR 0028 decision 3): never equal to a knowledge host.
    host: str | None = None

    def __init__(
        self, reply: str | Callable[[LLMRequest], str] = "", model: str = "fake-model"
    ) -> None:
        self.model = model
        self._reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = self._reply(request) if callable(self._reply) else self._reply
        return LLMResponse(text=text, provider=self.name, model=self.model)


register_provider("fake", lambda settings: FakeProvider(model=settings.llm_model or "fake-model"))
