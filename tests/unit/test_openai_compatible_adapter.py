"""The OpenAI-compatible adapter with an injected transport: no network, no model."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import SecretStr

from divesafe.api.state import build_provider
from divesafe.config import Settings
from divesafe.models import ChatMessage, LLMRequest, create_provider
from divesafe.models.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleProvider,
    join_sse,
)

REQUEST = LLMRequest(
    messages=(
        ChatMessage(role="system", content="SECRET-SYSTEM-PROMPT"),
        ChatMessage(role="user", content="SECRET-USER-PROMPT"),
    ),
    temperature=0.0,
    max_output_tokens=50,
    json_schema={"type": "object"},
)


def _event(content: str | None = None, finish: str | None = None) -> str:
    delta = {} if content is None else {"content": content}
    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(chunk)}\n\n"


def _ok(*pieces: str) -> bytes:
    body = "".join(_event(p) for p in pieces or ('{"a": 1}',))
    return (body + _event(None, "stop") + "data: [DONE]\n\n").encode()


class _Recorder:
    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> bytes:
        self.calls.append((url, json.loads(body), timeout))
        return self.reply


def _provider(reply: bytes = b"", **kwargs: object) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "m1",
        "http://localhost:8000/v1",
        transport=_Recorder(reply or _ok()),
        **kwargs,  # type: ignore[arg-type]
    )


def _run(provider: OpenAICompatibleProvider) -> str:
    return asyncio.run(provider.complete(REQUEST)).text


def test_a_request_is_streamed_at_temperature_zero_with_the_schema() -> None:
    transport = _Recorder(_ok())
    provider = OpenAICompatibleProvider("m1", "http://localhost:8000/v1", transport=transport)
    assert _run(provider) == '{"a": 1}'
    url, body, timeout = transport.calls[0]
    assert url == "http://localhost:8000/v1/chat/completions"
    assert body["model"] == "m1" and body["stream"] is True
    assert body["temperature"] == 0.0 and body["max_tokens"] == 50
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "divesafe_output", "schema": {"type": "object"}},
    }
    assert [m["role"] for m in body["messages"]] == ["system", "user"]  # type: ignore[union-attr]
    assert timeout > 0


def test_pieces_are_joined_and_the_response_carries_provider_and_model() -> None:
    response = asyncio.run(_provider(_ok('{"a"', ": 1}")).complete(REQUEST))
    assert response.text == '{"a": 1}'
    assert (response.provider, response.model) == ("openai_compatible", "m1")


def test_a_trailing_slash_in_the_base_url_is_harmless() -> None:
    transport = _Recorder(_ok())
    provider = OpenAICompatibleProvider("m", "http://127.0.0.1:1/v1/", transport=transport)
    _run(provider)
    assert transport.calls[0][0] == "http://127.0.0.1:1/v1/chat/completions"


def test_keep_alive_comments_and_usage_only_events_are_allowed() -> None:
    usage = 'data: {"choices": [], "usage": {"total_tokens": 3}}\n\n'
    raw = (
        b": keep-alive\n\n"
        + _ok("hi")[: -len(b"data: [DONE]\n\n")]
        + usage.encode()
        + b"data: [DONE]\n\n"
    )
    assert join_sse(raw) == "hi"


# ---- everything odd is refused, never repaired


def _lines(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n\n",
        _event("x").encode(),  # no finish, no [DONE]: cut off
        (_event("x") + "data: [DONE]\n\n").encode(),  # [DONE] without a stop reason
        (_event("x") + _event(None, "stop")).encode(),  # no [DONE]
        (_event("x") + _event(None, "content_filter") + "data: [DONE]\n").encode(),
        (_event("x") + _event(None, "tool_calls") + "data: [DONE]\n").encode(),
        (_event("x") + _event(None, "stop") + "data: [DONE]\n" + _event("late")).encode(),
        (_event("x") + _event(None, "stop") + _event("more") + "data: [DONE]\n").encode(),
        _lines('data: {"error": {"message": "boom"}}'),
        _lines("data: not json", "data: [DONE]"),
        _lines('data: {"choices": "no"}', "data: [DONE]"),
        _lines('data: {"choices": [{"index": 0, "delta": {"content": 5}}]}', "data: [DONE]"),
        _lines('data: {"choices": [{"index": 0, "delta": []}]}', "data: [DONE]"),
        _lines('data: {"choices": [{"index": 1, "delta": {"content": "x"}}]}', "data: [DONE]"),
        _lines(
            'data: {"choices": [{"index": 0, "delta": {"content": "a"}}, '
            '{"index": 1, "delta": {"content": "b"}}]}',
            "data: [DONE]",
        ),
        _lines("event: message", "data: [DONE]"),
        # each of these is refused only by its own check, the rest of the stream being valid
        (_event("x") + _event(None, "stop")).encode()
        + b'data: {"error": {"message": "boom"}, "choices": []}\n\ndata: [DONE]\n',
        (_event("x") + _event(None, "stop") + "data: [DONE]\n").encode()
        + b'data: {"choices": []}\n',
        b'data: {"choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"}, '
        b'{"index": 1, "delta": {"content": "b"}}]}\ndata: [DONE]\n',
        _lines("data: []", "data: [DONE]"),
        b"\xff\xfe",
        b"data: " + b"[" * 100_000 + b"\n",
        ("data: [DONE]\n" * 30_000).encode(),
    ],
)
def test_an_odd_or_incomplete_stream_is_refused(raw: bytes) -> None:
    with pytest.raises(OpenAICompatibleError, match="expected shape"):
        join_sse(raw)


def test_reasoning_text_is_never_used() -> None:
    chunk = {
        "choices": [{"index": 0, "delta": {"content": "A", "reasoning_content": "SECRET-THOUGHT"}}]
    }
    raw = (
        f"data: {json.dumps(chunk)}\n\n".encode()
        + _event(None, "stop").encode()
        + b"data: [DONE]\n"
    )
    assert join_sse(raw) == "A"


# ---- construction: https and the host allow-list


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_a_loopback_server_may_use_plain_http_and_is_not_external(host: str) -> None:
    provider = OpenAICompatibleProvider("m", f"http://{host}:8000/v1")
    assert provider.external is False


def test_a_remote_host_needs_https_and_to_be_allowed() -> None:
    allowed = frozenset({"llm.example.org"})
    provider = OpenAICompatibleProvider("m", "https://llm.example.org/v1", allowed_hosts=allowed)
    assert provider.external is True
    with pytest.raises(ValueError, match="must use https"):
        OpenAICompatibleProvider("m", "http://llm.example.org/v1", allowed_hosts=allowed)
    with pytest.raises(ValueError, match="DIVESAFE_LLM_ALLOWED_HOSTS"):
        OpenAICompatibleProvider("m", "https://llm.example.org/v1")
    with pytest.raises(ValueError, match="DIVESAFE_LLM_ALLOWED_HOSTS"):
        OpenAICompatibleProvider("m", "https://other.example.org/v1", allowed_hosts=allowed)


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost.evil.example/v1",
        "https://llm.example.org.evil.example/v1",
        "https://sub.llm.example.org/v1",
        "https://wall-e.ch30ng.org/v1",
        "https://192.0.2.1/v1",
    ],
)
def test_look_alike_hosts_are_not_allowed(url: str) -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("m", url, allowed_hosts=frozenset({"llm.example.org"}))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost/v1",
        "http:///v1",
        "http://user:pw@localhost/v1",
        "http://localhost/v1?x=1",
        "http://localhost/v1#frag",
        "http://localhost:99999/v1",
        "not a url",
        "http://[::1/v1",
    ],
)
def test_bad_base_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("m", url)


def test_a_model_name_is_required() -> None:
    with pytest.raises(ValueError, match="model name"):
        OpenAICompatibleProvider("  ", "http://localhost:8000/v1")


@pytest.mark.parametrize("key", ["", "has space", "line\nbreak", "tab\t", "caf\u00e9", "x" * 513])
def test_an_implausible_api_key_is_refused_without_echoing_it(key: str) -> None:
    with pytest.raises(ValueError) as caught:
        OpenAICompatibleProvider("m", "http://localhost:8000/v1", api_key=SecretStr(key))
    assert key not in str(caught.value) or key == ""


def test_the_key_is_not_in_the_repr_or_the_dict() -> None:
    provider = OpenAICompatibleProvider(
        "m", "http://localhost:8000/v1", api_key=SecretStr("sk-SECRET-KEY-123")
    )
    assert "SECRET-KEY" not in repr(provider) + str(provider)
    assert "SECRET-KEY" not in "".join(
        str(v) for k, v in vars(provider).items() if k != "_transport"
    )


# ---- settings and the factory


def _settings(**kwargs: object) -> Settings:
    base: dict[str, object] = {"llm_provider": "openai_compatible", "llm_model": "m"}
    return Settings(**{**base, **kwargs})  # type: ignore[arg-type]


def test_the_provider_is_created_from_settings() -> None:
    provider = create_provider(_settings(llm_base_url="http://localhost:8000/v1"))
    assert isinstance(provider, OpenAICompatibleProvider)
    assert (provider.call_timeout_seconds, provider.stage_timeout_seconds) == (60.0, 150.0)


def test_the_base_url_is_required() -> None:
    with pytest.raises(ValueError, match="DIVESAFE_LLM_BASE_URL"):
        create_provider(_settings())


def test_a_remote_server_needs_the_allow_list_and_the_external_opt_in() -> None:
    url = "https://llm.example.org/v1"
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        create_provider(_settings(llm_base_url=url))
    listed = _settings(llm_base_url=url, llm_allowed_hosts="llm.example.org")
    assert create_provider(listed).external is True
    with pytest.raises(RuntimeError, match="off-machine"):
        build_provider(listed)
    opted_in = _settings(
        llm_base_url=url, llm_allowed_hosts="llm.example.org", allow_external_llm=True
    )
    assert build_provider(opted_in) is not None


def test_a_loopback_server_needs_a_declaration_or_the_opt_in() -> None:
    url = "http://127.0.0.1:8000/v1"
    with pytest.raises(RuntimeError, match="loopback"):
        build_provider(_settings(llm_base_url=url))
    assert build_provider(_settings(llm_base_url=url, llm_server_is_local=True)) is not None
    assert build_provider(_settings(llm_base_url=url, allow_external_llm=True)) is not None


@pytest.mark.parametrize(
    "bad",
    [
        "*.example.org",
        "10.0.0.1",
        "a b",
        "https://x.example",
        "x/y",
        "1234",
        "a..b",
        "0x7f000001",
        "x.0xa000001",
        "host.example.",
        "-a.example",
    ],
)
def test_the_allow_list_takes_only_exact_hostnames(bad: str) -> None:
    with pytest.raises(ValueError, match="exact hostnames"):
        Settings(llm_allowed_hosts=bad)


def test_the_allow_list_is_trimmed_lowercased_and_may_hold_several() -> None:
    s = Settings(llm_allowed_hosts=" A.example.org , b.example.org ,")
    assert s.llm_allowed_host_set() == frozenset({"a.example.org", "b.example.org"})
    assert Settings().llm_allowed_host_set() == frozenset()


def test_the_budgets_apply_to_this_adapter_too() -> None:
    slow = create_provider(
        _settings(
            llm_base_url="http://localhost:8000/v1",
            llm_call_timeout_seconds=200,
            llm_stage_timeout_seconds=900,
            llm_first_byte_timeout_seconds=90,
            llm_max_concurrent_calls=1,
        )
    )
    assert (slow.call_timeout_seconds, slow.stage_timeout_seconds) == (200.0, 900.0)  # type: ignore[attr-defined]
    assert slow._deadline == 109.0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "bad",
    [
        {"call_timeout_seconds": float("nan")},
        {"call_timeout_seconds": True},
        {"call_timeout_seconds": 601, "stage_timeout_seconds": 900},
        {"stage_timeout_seconds": 10, "call_timeout_seconds": 60},
        {"first_byte_timeout_seconds": 40, "call_timeout_seconds": 60},
        {"max_concurrent_calls": 0},
        {"max_concurrent_calls": 9},
    ],
)
def test_a_directly_built_provider_cannot_dodge_the_budget_limits(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("m", "http://localhost:8000/v1", **bad)  # type: ignore[arg-type]


def test_a_settings_error_never_echoes_a_secret() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        Settings(
            llm_api_key="SENTINEL-LLM-KEY",  # type: ignore[arg-type]
            database_url="postgresql://u:SENTINEL-DB-PASSWORD@h/db",  # type: ignore[arg-type]
            api_key_hashes="SENTINEL-HASHES",  # type: ignore[arg-type]
            llm_allowed_hosts="not a host",
        )
    # What a startup traceback prints. (`.errors()` still carries the input unless a caller passes
    # include_input=False; nothing in src logs it for Settings.)
    shown = str(caught.value) + repr(caught.value)
    assert "SENTINEL" not in shown
    assert "SENTINEL" not in caught.value.json(include_input=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.:8000/v1",
        "http://0.0.0.0:8000/v1",
        "http://[::]:8000/v1",
        "http://localhost.evil.example:8000/v1",
        "http://127.0.0.1.evil.example:8000/v1",
        "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54/v1",
    ],
)
def test_loopback_look_alikes_may_not_use_plain_http(url: str) -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("m", url)


def test_the_key_is_not_in_the_transport_repr() -> None:
    provider = OpenAICompatibleProvider(
        "m", "http://localhost:8000/v1", api_key=SecretStr("sk-SECRET-KEY-123")
    )
    assert "SECRET-KEY" not in repr(provider._transport)


@pytest.mark.parametrize("field", ["tool_calls", "function_call", "refusal"])
def test_a_tool_call_or_a_refusal_is_refused(field: str) -> None:
    value: object = {"tool_calls": [{"id": "x"}], "function_call": {"name": "x"}}.get(field, "no")
    chunk = {"choices": [{"index": 0, "delta": {"content": "a", field: value}}]}
    raw = f"data: {json.dumps(chunk)}\n\n".encode() + _ok()
    with pytest.raises(OpenAICompatibleError, match="expected shape"):
        join_sse(raw)


def test_a_unicode_line_separator_inside_content_is_not_a_line_break() -> None:
    chunk = {"choices": [{"index": 0, "delta": {"content": "a\u2028b\x85c"}}]}
    raw = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode() + _ok("")
    assert b"\xe2\x80\xa8" in raw  # the raw separator is in the stream, not an escape
    assert join_sse(raw) == "a\u2028b\x85c"


def test_more_than_the_line_cap_of_harmless_lines_is_refused() -> None:
    body = _ok("x")[: -len(b"data: [DONE]\n\n")] + b": keep-alive\n" * 20_001 + b"data: [DONE]\n"
    with pytest.raises(OpenAICompatibleError, match="expected shape"):
        join_sse(body)


def test_the_output_token_cap_comes_from_settings_for_both_adapters() -> None:
    from divesafe.models.ollama import OllamaProvider

    s = _settings(llm_base_url="http://localhost:8000/v1", llm_max_output_tokens=4000)
    assert create_provider(s).max_output_tokens == 4000  # type: ignore[attr-defined]
    assert _settings(llm_base_url="http://localhost:8000/v1").llm_max_output_tokens == 1500
    assert (
        OllamaProvider("m", "http://localhost:11434", max_output_tokens=3000).max_output_tokens
        == 3000
    )
    for bad in (0, -1, 8001, True, 1.5):
        with pytest.raises(ValueError):
            OpenAICompatibleProvider("m", "http://localhost:8000/v1", max_output_tokens=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            OllamaProvider("m", "http://localhost:11434", max_output_tokens=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, 255, 8001])
def test_the_settings_bound_the_output_token_cap(bad: int) -> None:
    with pytest.raises(ValueError):
        Settings(llm_max_output_tokens=bad)


def test_a_closed_provider_refuses_new_calls_for_both_adapters() -> None:
    import asyncio

    from divesafe.models import ChatMessage, LLMRequest
    from divesafe.models.ollama import OllamaError, OllamaProvider
    from divesafe.models.openai_compatible import OpenAICompatibleError

    request = LLMRequest(messages=(ChatMessage(role="user", content="x"),))
    for provider, error in (
        (OpenAICompatibleProvider("m", "http://localhost:8000/v1"), OpenAICompatibleError),
        (OllamaProvider("m", "http://localhost:11434"), OllamaError),
    ):
        provider.close()
        with pytest.raises(error, match="closed"):
            asyncio.run(provider.complete(request))
        provider.close()  # closing twice is harmless


def test_close_shuts_the_executor_and_cancels_a_queued_call() -> None:
    import asyncio
    import threading

    from divesafe.models import ChatMessage, LLMRequest

    release = threading.Event()

    def blocking(url: str, body: bytes, timeout: float) -> bytes:
        release.wait(5)
        return b"data: [DONE]\n\n"

    provider = OpenAICompatibleProvider(
        "m", "http://localhost:8000/v1", transport=blocking, max_concurrent_calls=1
    )
    request = LLMRequest(messages=(ChatMessage(role="user", content="x"),))

    async def scenario() -> list[object]:
        running = asyncio.ensure_future(provider.complete(request))
        queued = asyncio.ensure_future(provider.complete(request))
        await asyncio.sleep(0.2)
        provider.close()
        release.set()
        return await asyncio.gather(running, queued, return_exceptions=True)

    first, second = asyncio.run(scenario())
    assert isinstance(second, asyncio.CancelledError)
    assert provider._executor._shutdown  # type: ignore[attr-defined]
    assert first is not None


def test_a_reply_stopped_by_the_token_limit_is_refused_with_its_own_error() -> None:
    from divesafe.models import TokenLimitError

    raw = (_event("x") + _event(None, "length") + "data: [DONE]\n").encode()
    with pytest.raises(TokenLimitError) as caught:
        join_sse(raw)
    assert isinstance(caught.value, OpenAICompatibleError)
    assert str(caught.value) == "the model stopped at the output-token limit"


# ---- fixed failure labels per refusal shape (group E-3 item 4; new tests only) -----------------


def _reason_of(raw: bytes) -> str:
    from divesafe.models.openai_compatible import failure_reason

    with pytest.raises(OpenAICompatibleError) as info:
        join_sse(raw)
    return failure_reason(info.value)


def test_an_error_event_is_labelled_error_event() -> None:
    raw = b'data: {"error": {"message": "x"}}\n\ndata: [DONE]\n\n'
    assert _reason_of(raw) == "error_event"


def test_a_stream_with_no_done_marker_is_labelled_no_done_marker() -> None:
    raw = _event("hi") + _event(None, "stop")
    assert _reason_of(raw.encode()) == "no_done_marker"


def test_an_event_line_is_labelled_event_line() -> None:
    raw = b"event: message\n" + _ok("hi")
    assert _reason_of(raw) == "event_line"


def test_non_json_data_is_labelled_bad_event_json() -> None:
    raw = b"data: {not json\n\ndata: [DONE]\n\n"
    assert _reason_of(raw) == "bad_event_json"


def test_a_tool_call_delta_is_labelled_tool_or_refusal() -> None:
    chunk = {"choices": [{"index": 0, "delta": {"tool_calls": [{"id": "t"}]}}]}
    raw = f"data: {json.dumps(chunk)}\n\n".encode() + _event(None, "stop").encode()
    raw += b"data: [DONE]\n\n"
    assert _reason_of(raw) == "tool_or_refusal"


def test_an_empty_body_is_labelled_empty_stream() -> None:
    assert _reason_of(b"") == "empty_stream"


def test_a_length_finish_is_labelled_token_limit() -> None:
    raw = _event("partial", "length").encode() + b"data: [DONE]\n\n"
    assert _reason_of(raw) == "token_limit"


def test_the_normal_stream_gives_no_error() -> None:
    assert join_sse(_ok("hello")) == "hello"


def test_failure_reasons_contains_every_label_the_adapter_uses() -> None:
    from divesafe.models.openai_compatible import FAILURE_REASONS

    for label in (
        "event_line",
        "error_event",
        "no_done_marker",
        "bad_event_json",
        "tool_or_refusal",
        "empty_stream",
        "bad_shape",
        "token_limit",
        "unreachable",
        "http_error",
        "read_timeout",
        "connection_error",
        "deadline",
        "too_large",
        "slots_full",
        "closed",
        "unclassified",
    ):
        assert label in FAILURE_REASONS, label


def test_failure_reason_of_a_plain_exception_is_unclassified() -> None:
    from divesafe.models.openai_compatible import failure_reason

    assert failure_reason(RuntimeError("anything at all")) == "unclassified"
    assert failure_reason(OpenAICompatibleError("x", reason="not-a-label")) == "unclassified"


def test_a_data_line_after_done_is_labelled_data_after_done() -> None:
    # A finished event and [DONE], then one more data line: refused with its own label, not as a
    # missing [DONE] (the stream does have one).
    raw = _event("hi") + _event(None, "stop") + "data: [DONE]\n\n" + _event("late")
    assert _reason_of(raw.encode()) == "data_after_done"


def test_a_stream_that_ends_with_no_done_marker_stays_no_done_marker() -> None:
    raw = _event("hi") + _event(None, "stop")
    assert _reason_of(raw.encode()) == "no_done_marker"


def test_a_subclass_that_never_sets_reason_is_unclassified_and_does_not_raise() -> None:
    from divesafe.models.openai_compatible import failure_reason

    class _NoReason(OpenAICompatibleError):
        def __init__(self) -> None:
            # Skips OpenAICompatibleError.__init__, so `reason` is never set on the instance.
            RuntimeError.__init__(self, "fixed message")

    exc = _NoReason()
    assert not hasattr(exc, "reason")
    assert failure_reason(exc) == "unclassified"


def test_a_reason_outside_the_label_set_is_unclassified() -> None:
    from divesafe.models.openai_compatible import failure_reason

    exc = OpenAICompatibleError("fixed message", reason="sk-SECRET")
    assert failure_reason(exc) == "unclassified"


# ---- failure_reason reads only a plain str label from the instance (hardening) -----------------


def test_a_str_subclass_with_a_label_text_is_unclassified_and_the_result_is_plain_str() -> None:
    from divesafe.models.openai_compatible import failure_reason

    class _Tagged(str):
        pass

    exc = OpenAICompatibleError("fixed message")
    exc.reason = _Tagged("event_line")  # type: ignore[assignment]
    result = failure_reason(exc)
    assert result == "unclassified"
    assert type(result) is str


def test_an_unhashable_reason_is_unclassified_and_does_not_raise() -> None:
    from divesafe.models.openai_compatible import failure_reason

    exc = OpenAICompatibleError("fixed message")
    exc.reason = ["event_line"]  # type: ignore[assignment]
    assert failure_reason(exc) == "unclassified"


def test_a_reason_property_that_raises_is_unclassified_and_does_not_raise() -> None:
    from divesafe.models.openai_compatible import failure_reason

    class _Raising(OpenAICompatibleError):
        def __init__(self) -> None:
            # Bypasses the parent __init__, which would assign `reason` and fail on the property.
            RuntimeError.__init__(self, "fixed message")

        @property  # type: ignore[misc]
        def reason(self) -> str:
            raise RuntimeError("reading the reason failed")

    assert failure_reason(_Raising()) == "unclassified"


class _AlwaysEqual:
    """Equal to every label: a `==` test on it would pass for any label."""

    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    "value",
    [3, None, b"event_line", ("event_line",), _AlwaysEqual()],
    ids=["int", "none", "bytes", "tuple", "always_equal"],
)
def test_a_non_string_reason_is_unclassified(value: object) -> None:
    from divesafe.models.openai_compatible import failure_reason

    exc = OpenAICompatibleError("fixed message")
    exc.reason = value  # type: ignore[assignment]
    assert failure_reason(exc) == "unclassified"


def test_a_valid_label_set_on_the_instance_is_returned_as_plain_str() -> None:
    from divesafe.models.openai_compatible import failure_reason

    exc = OpenAICompatibleError("fixed message")
    exc.reason = "event_line"
    result = failure_reason(exc)
    assert result == "event_line"
    assert type(result) is str


# ---- _post labels a transport failure by its class (read_timeout, connection_error) ------------

_FIXED_TRANSPORT_MESSAGE = "the model server could not be reached or refused the request"


class _RaisingOpener:
    """Stands in for the urllib opener: `open` raises the given exception, nothing is sent."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def open(self, *_args: object, **_kwargs: object) -> object:
        raise self._exc


def _post_raising(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> OpenAICompatibleError:
    """Calls _post with a fake opener that raises `exc`; returns the error _post raised."""
    import threading

    from divesafe.models import openai_compatible as module

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_a: _RaisingOpener(exc))
    with pytest.raises(OpenAICompatibleError) as info:
        module._post(
            "http://127.0.0.1:1/v1/chat/completions",
            b"{}",
            5.0,
            api_key=None,
            loopback=False,
            slots=threading.BoundedSemaphore(1),
            socket_timeout=5.0,
        )
    return info.value


def test_a_bare_timeout_is_labelled_read_timeout_with_the_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from divesafe.models.openai_compatible import failure_reason

    error = _post_raising(monkeypatch, TimeoutError("socket read timed out"))
    assert failure_reason(error) == "read_timeout"
    assert str(error) == _FIXED_TRANSPORT_MESSAGE
    assert "timed out" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_a_url_error_is_labelled_connection_error_and_hides_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    from divesafe.models.openai_compatible import failure_reason

    error = _post_raising(monkeypatch, urllib.error.URLError("refused"))
    assert failure_reason(error) == "connection_error"
    # The fixed message is the only text; the URLError's own text is not carried.
    assert str(error) == _FIXED_TRANSPORT_MESSAGE
    assert str(error) != "refused"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_a_connection_reset_is_labelled_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from divesafe.models.openai_compatible import failure_reason

    error = _post_raising(monkeypatch, ConnectionResetError("reset by peer"))
    assert failure_reason(error) == "connection_error"
    assert str(error) == _FIXED_TRANSPORT_MESSAGE
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_a_non_loopback_peer_is_labelled_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from divesafe.models.loopback import NotOnThisMachineError
    from divesafe.models.openai_compatible import failure_reason

    error = _post_raising(monkeypatch, NotOnThisMachineError())
    assert failure_reason(error) == "unreachable"
    assert str(error) == "the model server is not on this machine"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_an_http_error_is_labelled_http_error_with_the_fixed_message_and_no_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import urllib.error

    from divesafe.models.openai_compatible import failure_reason

    http_error = urllib.error.HTTPError(
        "http://127.0.0.1:1/v1/chat/completions", 500, "SERVER-DETAIL", {}, io.BytesIO(b"x")
    )
    error = _post_raising(monkeypatch, http_error)
    assert failure_reason(error) == "http_error"
    assert str(error) == _FIXED_TRANSPORT_MESSAGE
    assert "SERVER-DETAIL" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
