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


def test_a_request_is_streamed_at_temperature_zero_with_the_schema() -> None:
    transport = _Recorder(_ok())
    provider = OllamaProvider("m1", transport=transport)
    assert _run(provider) == '{"a": 1}'
    url, body, timeout = transport.calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert body["model"] == "m1" and body["stream"] is True
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
        b'{"message": {"content": "x"}}',  # no `done`: a cut-off stream
        b'{"message": {"content": "x"}, "done": false}',
        b'{"message": {"content": "a"}, "done": true}\n{"message": {"content": "b"}, "done": true}',
        b'{"message": {"content": "a"}, "done": true}\n{"message": {"content": "b"}}',
        b'{"message": {}, "done": true}',
        b'{"message": {"content": 5}, "done": true}',
        b'{"message": {"content": "a"}, "done": false}\n{"done": true}',
        b'{"error": "model failed"}',
        b'{"message": {"content": "a"}}\n{"error": "oops"}',
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


def test_thinking_is_switched_off_by_default() -> None:
    transport = _Recorder(_ok())
    _run(OllamaProvider("m1", transport=transport))
    assert transport.calls[0][1]["think"] is False


def test_thinking_can_be_switched_on_or_left_to_the_server() -> None:
    on, unset = _Recorder(_ok()), _Recorder(_ok())
    _run(OllamaProvider("m1", transport=on, think=True))
    _run(OllamaProvider("m1", transport=unset, think=None))
    assert on.calls[0][1]["think"] is True
    assert "think" not in unset.calls[0][1]


def test_streamed_lines_are_joined_in_order() -> None:
    lines = [
        {"message": {"content": '{"a"'}, "done": False},
        {"message": {"content": ": 1}"}, "done": False},
        {"message": {"content": ""}, "done": True},
    ]
    reply = "\n".join(json.dumps(x) for x in lines).encode() + b"\n"
    assert _run(OllamaProvider("m1", transport=_Recorder(reply))) == '{"a": 1}'


def test_a_stream_cut_off_before_done_is_refused_not_used_partly() -> None:
    reply = b'{"message": {"content": "{\\"a\\""}, "done": false}\n'
    with pytest.raises(OllamaError):
        _run(OllamaProvider("m1", transport=_Recorder(reply)))


def test_plain_http_to_a_remote_host_needs_that_exact_hostname_listed() -> None:
    url = "http://ollama.example.org:11434"
    with pytest.raises(ValueError, match="https"):
        OllamaProvider("m", url)
    with pytest.raises(ValueError, match="https"):
        OllamaProvider("m", url, plain_http_hosts=frozenset({"other.example.org"}))
    with pytest.raises(ValueError, match="https"):
        OllamaProvider(
            "m",
            "http://evil.ollama.example.org",
            plain_http_hosts=frozenset({"ollama.example.org"}),
        )
    allowed = OllamaProvider("m", url, plain_http_hosts=frozenset({"ollama.example.org"}))
    assert allowed.external is True


def test_the_plain_http_list_comes_from_settings_and_is_still_external() -> None:
    settings = Settings(
        llm_provider="ollama",
        llm_model="m",
        llm_base_url="http://ollama.example.org:11434",  # type: ignore[arg-type]
        ollama_plain_http_hosts=" Ollama.Example.org ,",
    )
    provider = create_provider(settings)
    assert provider.external is True
    with pytest.raises(RuntimeError, match="off-machine"):
        build_provider(settings)
    assert build_provider(settings.model_copy(update={"allow_external_llm": True})) is not None
    with pytest.raises(ValueError, match="https"):
        create_provider(settings.model_copy(update={"ollama_plain_http_hosts": None}))


@pytest.mark.parametrize("bad", ["*", "10.0.0.0/8", "a b", "http://x", "x/y", "-x"])
def test_a_malformed_plain_http_list_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="exact hostnames"):
        Settings(ollama_plain_http_hosts=bad)


def test_production_refuses_plain_http_hosts() -> None:
    with pytest.raises(ValueError, match="production"):
        Settings(
            environment="production",
            llm_provider="ollama",
            api_key_hashes='{"a": {"actor": "x", "roles": ["viewer"]}}',  # type: ignore[arg-type]
            ollama_plain_http_hosts="ollama.example.org",
        )


def _stream(*lines: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode()


def test_a_reply_stopped_by_the_token_limit_is_refused() -> None:
    cut = _stream({"message": {"content": '{"a"'}, "done": True, "done_reason": "length"})
    with pytest.raises(OllamaError):
        _run(OllamaProvider("m1", transport=_Recorder(cut)))
    ok = _stream({"message": {"content": "x"}, "done": True, "done_reason": "stop"})
    assert _run(OllamaProvider("m1", transport=_Recorder(ok))) == "x"


def test_thinking_text_is_never_part_of_the_answer() -> None:
    reply = _stream(
        {"message": {"content": "", "thinking": "SECRET-REASONING"}, "done": False},
        {"message": {"content": "answer", "thinking": "more"}, "done": True},
    )
    assert _run(OllamaProvider("m1", transport=_Recorder(reply), think=True)) == "answer"


def test_a_line_after_the_final_one_and_an_endless_stream_are_refused() -> None:
    after = _stream(
        {"message": {"content": "a"}, "done": True}, {"message": {"content": "b"}, "done": False}
    )
    with pytest.raises(OllamaError):
        _run(OllamaProvider("m1", transport=_Recorder(after)))
    many = b'{"message": {"content": ""}, "done": false}\n' * 30_000 + (
        b'{"message": {"content": "x"}, "done": true}\n'
    )
    with pytest.raises(OllamaError):
        _run(OllamaProvider("m1", transport=_Recorder(many)))


@pytest.mark.parametrize(
    "url",
    [
        "http://@ollama.example.org",
        "http://ollama.example.org.",
        "http://[bad",
    ],
)
def test_odd_urls_are_refused_even_for_a_listed_host(url: str) -> None:
    with pytest.raises(ValueError) as caught:
        OllamaProvider("m", url, plain_http_hosts=frozenset({"ollama.example.org"}))
    assert "bad" not in str(caught.value).replace("not a valid", "")


@pytest.mark.parametrize("bad", ["10.0.0.5", "167772165", "host.123"])
def test_ip_addresses_and_all_digit_names_cannot_be_listed(bad: str) -> None:
    with pytest.raises(ValueError, match="exact hostnames"):
        Settings(ollama_plain_http_hosts=bad)
