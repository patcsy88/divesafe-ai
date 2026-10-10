"""The Ollama adapter, with an injected transport: no network, no model."""

from __future__ import annotations

import asyncio
import json
import traceback

import pytest

from divesafe.api.state import build_provider
from divesafe.config import Settings
from divesafe.models import ChatMessage, LLMRequest, create_provider
from divesafe.models.ollama import OllamaError, OllamaProvider

REQUEST = LLMRequest(
    messages=(
        ChatMessage(role="system", content="SECRET-SYSTEM-PROMPT"),
        ChatMessage(role="user", content="SECRET-USER-PROMPT"),
    ),
    temperature=0.0,
    max_output_tokens=50,
    json_schema={"type": "object"},
)


class _Recorder:
    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> bytes:
        self.calls.append((url, json.loads(body), timeout))
        return self.reply


def _ok(text: str = '{"a": 1}') -> bytes:
    return json.dumps({"message": {"role": "assistant", "content": text}, "done": True}).encode()


def _run(provider: OllamaProvider) -> str:
    return asyncio.run(provider.complete(REQUEST)).text


def test_a_request_is_sent_non_streaming_at_temperature_zero_with_the_schema() -> None:
    transport = _Recorder(_ok())
    provider = OllamaProvider("m1", transport=transport)
    assert _run(provider) == '{"a": 1}'
    url, body, timeout = transport.calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert body["model"] == "m1" and body["stream"] is False
    assert body["format"] == {"type": "object"}
    assert body["options"] == {"temperature": 0.0, "num_predict": 50}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]  # type: ignore[union-attr]
    assert timeout > 0


def test_the_response_carries_provider_and_model() -> None:
    response = asyncio.run(OllamaProvider("m1", transport=_Recorder(_ok())).complete(REQUEST))
    assert (response.provider, response.model) == ("ollama", "m1")


@pytest.mark.parametrize(
    "reply",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"message": {}}',
        b'{"message": {"content": 5}}',
        b'{"message": 3}',
    ],
)
def test_an_unexpected_response_shape_is_refused_without_echoing_it(reply: bytes) -> None:
    with pytest.raises(OllamaError) as caught:
        _run(OllamaProvider("m1", transport=_Recorder(reply)))
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    ("url", "external"),
    [
        ("http://localhost:11434", False),
        ("http://127.0.0.1:11434", False),
        ("http://[::1]:11434", False),
        ("https://ollama.example.org", True),
    ],
)
def test_only_a_loopback_server_counts_as_local(url: str, external: bool) -> None:
    assert OllamaProvider("m", url).external is external


@pytest.mark.parametrize(
    "url",
    [
        "http://ollama.example.org",
        "http://10.0.0.5:11434",
        "ftp://localhost",
        "file:///etc/passwd",
        "http://user:pw@localhost:11434",
        "http://localhost:11434/?x=1",
        "http://localhost:11434/#f",
        "localhost:11434",
        "http://",
    ],
)
def test_unsafe_base_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        OllamaProvider("m", url)


def test_a_lookalike_hostname_is_not_loopback() -> None:
    assert OllamaProvider("m", "https://localhost.evil.example").external is True
    with pytest.raises(ValueError):
        OllamaProvider("m", "http://localhost.evil.example")


def test_a_model_name_is_required() -> None:
    with pytest.raises(ValueError):
        OllamaProvider("  ")
    with pytest.raises(ValueError):
        create_provider(Settings(llm_provider="ollama"))


def test_the_factory_reads_the_settings() -> None:
    provider = create_provider(Settings(llm_provider="ollama", llm_model="qwen-test"))
    assert (provider.name, provider.model, provider.external) == ("ollama", "qwen-test", False)
    remote = create_provider(
        Settings(
            llm_provider="ollama",
            llm_model="m",
            llm_base_url="https://o.example.org",  # type: ignore[arg-type]
        )
    )
    assert remote.external is True


def test_a_remote_server_needs_the_external_opt_in() -> None:
    settings = Settings(
        llm_provider="ollama",
        llm_model="m",
        llm_base_url="https://o.example.org",  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="off-machine"):
        build_provider(settings)
    allowed = settings.model_copy(update={"allow_external_llm": True})
    assert build_provider(allowed) is not None
    assert build_provider(Settings(llm_provider="ollama", llm_model="m")) is not None
