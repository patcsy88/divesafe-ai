"""The adapter's output-token count (`usage.completion_tokens`, ADR 0018 amendment 2026-11-15).

The count is logged only: it never changes what is accepted or refused. No network, no model.
"""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
from typing import Any

import pytest

from divesafe.models import ChatMessage, LLMRequest, LLMResponse, TokenLimitError
from divesafe.models.llm import plain_count
from divesafe.models.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleProvider,
    OpenAICompatibleTokenLimitError,
    failure_reason,
    join_sse,
    join_sse_counted,
)

REQUEST = LLMRequest(
    messages=(
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="usr"),
    ),
    temperature=0.0,
    max_output_tokens=50,
    json_schema={"type": "object"},
)

DONE = "data: [DONE]\n\n"


def _data(event: object) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _chunk(content: str | None = None, finish: str | None = None, **extra: Any) -> str:
    delta = {} if content is None else {"content": content}
    return _data({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra})


def _usage_only(usage: object) -> str:
    return _data({"choices": [], "usage": usage})


def _ok(*middle: str, text: str = '{"a": 1}') -> bytes:
    return (_chunk(text) + "".join(middle) + _chunk(None, "stop") + DONE).encode()


# ---- reading the count -------------------------------------------------------------------------


def test_the_count_is_read_from_a_usage_only_event() -> None:
    raw = _chunk("hi") + _chunk(None, "stop") + _usage_only({"completion_tokens": 5}) + DONE
    assert join_sse_counted(raw.encode()) == ("hi", 5)


def test_the_count_is_read_from_the_final_choice_event() -> None:
    raw = _chunk("hi") + _chunk(None, "stop", usage={"completion_tokens": 7}) + DONE
    assert join_sse_counted(raw.encode()) == ("hi", 7)


def test_the_last_non_null_usage_wins_and_null_usage_is_ignored() -> None:
    raw = (
        _chunk("a", usage={"completion_tokens": 3})
        + _chunk("b", usage=None)
        + _chunk(None, "stop", usage={"completion_tokens": 9})
        + _usage_only(None)
        + DONE
    )
    assert join_sse_counted(raw.encode()) == ("ab", 9)


def test_null_usage_on_every_chunk_gives_none() -> None:
    raw = _chunk("a", usage=None) + _chunk(None, "stop", usage=None) + DONE
    assert join_sse_counted(raw.encode()) == ("a", None)


def test_a_later_unreadable_usage_replaces_an_earlier_count_with_none() -> None:
    """The count comes from the LAST event carrying a usage object; a bad one is None."""
    raw = _ok(_usage_only({"completion_tokens": 4}), _usage_only({"completion_tokens": -1}))
    assert join_sse_counted(raw) == ('{"a": 1}', None)


def test_zero_is_a_valid_count() -> None:
    assert join_sse_counted(_ok(_usage_only({"completion_tokens": 0}))) == ('{"a": 1}', 0)


def test_no_usage_gives_none() -> None:
    assert join_sse_counted(_ok()) == ('{"a": 1}', None)


BAD_USAGE: list[object] = [
    {"completion_tokens": -1},
    {"completion_tokens": True},
    {"completion_tokens": False},
    {"completion_tokens": 5.0},
    {"completion_tokens": 5.5},
    {"completion_tokens": "5"},
    {"completion_tokens": None},
    {"completion_tokens": {"n": 5}},
    {"completion_tokens": [5]},
    {"prompt_tokens": 5},
    {},
    "usage",
    5,
    [{"completion_tokens": 5}],
    True,
]


@pytest.mark.parametrize("usage", BAD_USAGE, ids=repr)
def test_a_bad_count_gives_none_and_the_reply_is_still_accepted(usage: object) -> None:
    for raw in (
        _ok(_usage_only(usage)),
        (_chunk("x") + _chunk(None, "stop", usage=usage) + DONE).encode(),
    ):
        text, tokens = join_sse_counted(raw)
        assert text in ('{"a": 1}', "x") and tokens is None
        assert join_sse(raw) == text


def test_join_sse_returns_the_same_text_as_join_sse_counted() -> None:
    raw = _ok(_usage_only({"completion_tokens": 5}))
    assert join_sse(raw) == join_sse_counted(raw)[0] == '{"a": 1}'


def test_complete_fills_output_tokens() -> None:
    def transport(url: str, body: bytes, timeout: float) -> bytes:
        return _ok(_usage_only({"completion_tokens": 11}))

    provider = OpenAICompatibleProvider("m1", "http://localhost:8000/v1", transport=transport)
    try:
        response = asyncio.run(provider.complete(REQUEST))
    finally:
        provider.close()
    assert (response.text, response.output_tokens) == ('{"a": 1}', 11)


def test_complete_without_usage_gives_none() -> None:
    provider = OpenAICompatibleProvider(
        "m1", "http://localhost:8000/v1", transport=lambda *_: _ok()
    )
    try:
        assert asyncio.run(provider.complete(REQUEST)).output_tokens is None
    finally:
        provider.close()


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "3"], ids=repr)
def test_the_response_field_is_a_strict_non_negative_int(bad: object) -> None:
    with pytest.raises(ValueError):
        LLMResponse(text="", provider="p", model="m", output_tokens=bad)  # type: ignore[arg-type]
    assert LLMResponse(text="", provider="p", model="m").output_tokens is None
    assert LLMResponse(text="", provider="p", model="m", output_tokens=0).output_tokens == 0


def test_plain_count_accepts_only_plain_non_negative_ints() -> None:
    class Sub(int):
        pass

    assert [plain_count(v) for v in (0, 3)] == [0, 3]
    for bad in (-1, True, False, 1.0, "1", None, Sub(2), [1]):
        assert plain_count(bad) is None


# ---- usage never changes a refusal -------------------------------------------------------------

USAGE = _usage_only({"completion_tokens": 5})

# Refused streams, each starting with one choice event (the usage variants below rely on that).
REFUSED: list[tuple[str, str]] = [
    ("bad_json", _chunk("x") + "data: {not json\n\n" + DONE),
    ("error_event", _chunk("x") + _data({"error": {"message": "m"}}) + DONE),
    ("no_done", _chunk("x") + _chunk(None, "stop")),
    ("after_done", _chunk("x") + _chunk(None, "stop") + DONE + _chunk("y")),
    ("bad_finish", _chunk("x") + _chunk(None, "content_filter") + DONE),
    ("bad_shape", _chunk("x") + _data({"choices": "no"}) + DONE),
]


def _with_usage(stream: str, usage: str) -> list[str]:
    """`stream` with `usage` before the first event, after the first event, and with the usage
    put on the first choice event itself."""
    first_end = stream.index("\n\n") + 2
    first = json.loads(stream[len("data: ") : first_end].strip())
    first["usage"] = {"completion_tokens": 5}
    return [
        usage + stream,
        stream[:first_end] + usage + stream[first_end:],
        _data(first) + stream[first_end:],
    ]


def _refusal(raw: bytes) -> tuple[type[BaseException], str, str]:
    with pytest.raises(OpenAICompatibleError) as caught:
        join_sse_counted(raw)
    return type(caught.value), failure_reason(caught.value), str(caught.value)


@pytest.mark.parametrize(("name", "stream"), REFUSED, ids=[n for n, _ in REFUSED])
def test_usage_never_changes_a_refusal(name: str, stream: str) -> None:
    expected = _refusal(stream.encode())
    assert expected[1] != "unclassified"
    for usage in (USAGE, _usage_only({"completion_tokens": -3}), _usage_only("x")):
        for variant in _with_usage(stream, usage):
            assert _refusal(variant.encode()) == expected
            with pytest.raises(OpenAICompatibleError) as caught:
                join_sse(variant.encode())
            assert (type(caught.value), str(caught.value)) == expected[::2]


# ---- the token-limit stop ----------------------------------------------------------------------


def _stopped(raw: str) -> OpenAICompatibleTokenLimitError:
    with pytest.raises(OpenAICompatibleTokenLimitError) as caught:
        join_sse_counted(raw.encode())
    return caught.value


def test_a_length_stop_carries_the_chars_joined_and_the_count() -> None:
    error = _stopped(
        _chunk("SECRET-PART-1")
        + _usage_only({"completion_tokens": 40})
        + _chunk("ab")
        + _chunk(None, "length")
        + DONE
    )
    assert (error.output_chars, error.output_tokens) == (len("SECRET-PART-1ab"), 40)
    assert isinstance(error, TokenLimitError) and isinstance(error, OpenAICompatibleError)
    assert str(error) == "the model stopped at the output-token limit"
    assert error.args == ("the model stopped at the output-token limit",)
    assert failure_reason(error) == "token_limit"
    assert not hasattr(error, "text")
    assert set(vars(error)) == {"output_chars", "output_tokens", "reason"}
    assert "SECRET-PART" not in repr(vars(error)) + repr(error) + str(error)


def test_a_length_stop_reads_usage_on_the_stopping_event_itself() -> None:
    error = _stopped(_chunk("abc") + _chunk("d", "length", usage={"completion_tokens": 50}) + DONE)
    assert (error.output_chars, error.output_tokens) == (4, 50)


def test_a_length_stop_with_no_content_or_usage() -> None:
    error = _stopped(_chunk(None, "length") + DONE)
    assert (error.output_chars, error.output_tokens) == (0, None)


@pytest.mark.parametrize("usage", BAD_USAGE, ids=repr)
def test_a_bad_count_at_a_length_stop_gives_none(usage: object) -> None:
    error = _stopped(_chunk("abc") + _chunk(None, "length", usage=usage) + DONE)
    assert (error.output_chars, error.output_tokens) == (3, None)


@pytest.mark.parametrize("cls", [TokenLimitError, OpenAICompatibleTokenLimitError])
def test_pickle_and_copy_keep_the_counts(cls: type[TokenLimitError]) -> None:
    original = cls(output_chars=12, output_tokens=34)
    for clone in (
        pickle.loads(pickle.dumps(original)),  # noqa: S301 (our own bytes)
        copy.copy(original),
        copy.deepcopy(original),
    ):
        assert type(clone) is cls
        assert (clone.output_chars, clone.output_tokens) == (12, 34)
        assert str(clone) == "the model stopped at the output-token limit"
        if isinstance(clone, OpenAICompatibleError):
            assert failure_reason(clone) == "token_limit"


def test_the_error_keeps_only_plain_counts() -> None:
    error = TokenLimitError(output_chars=True, output_tokens=-2)
    assert (error.output_chars, error.output_tokens) == (None, None)
    assert TokenLimitError().output_chars is None
    clone = pickle.loads(pickle.dumps(TokenLimitError(output_chars="9", output_tokens=1.0)))  # noqa: S301
    assert (clone.output_chars, clone.output_tokens) == (None, None)


# ---- the request body is unchanged -------------------------------------------------------------

GOLDEN_BODY = (
    b'{"model": "m1", "messages": [{"role": "system", "content": "sys"}, '
    b'{"role": "user", "content": "usr"}], "stream": true, "temperature": 0.0, '
    b'"max_tokens": 50, "response_format": {"type": "json_schema", "json_schema": '
    b'{"name": "divesafe_output", "schema": {"type": "object"}}}}'
)


def test_the_request_body_is_byte_for_byte_unchanged_and_asks_for_no_usage() -> None:
    provider = OpenAICompatibleProvider("m1", "http://localhost:8000/v1", transport=lambda *_: b"")
    try:
        body = provider._body(REQUEST)
    finally:
        provider.close()
    assert body == GOLDEN_BODY
    assert b"stream_options" not in body and b"include_usage" not in body
    decoded = json.loads(body)
    assert "stream_options" not in decoded
    assert set(decoded) == {
        "model",
        "messages",
        "stream",
        "temperature",
        "max_tokens",
        "response_format",
    }


# ---- the logged-count ceiling (MAX_LOGGED_COUNT) -----------------------------------------------


def test_the_ceiling_is_one_billion() -> None:
    from divesafe.models.llm import MAX_LOGGED_COUNT

    assert MAX_LOGGED_COUNT == 10**9


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (1, 1), (10**9 - 1, 10**9 - 1), (10**9, 10**9), (10**9 + 1, None), (10**20, None)],
)
def test_plain_count_boundaries(value: int, expected: int | None) -> None:
    assert plain_count(value) == expected
    assert plain_count(-value - 1) is None


@pytest.mark.parametrize(("count", "expected"), [(10**9, 10**9), (10**9 + 1, None), (10**20, None)])
def test_a_count_above_the_ceiling_gives_none_and_the_reply_is_still_accepted(
    count: int, expected: int | None
) -> None:
    raw = _ok(_usage_only({"completion_tokens": count}))
    assert join_sse_counted(raw) == ('{"a": 1}', expected)
    assert join_sse(raw) == '{"a": 1}'

    def transport(url: str, body: bytes, timeout: float) -> bytes:
        return raw

    provider = OpenAICompatibleProvider("m1", "http://localhost:8000/v1", transport=transport)
    try:
        response = asyncio.run(provider.complete(REQUEST))
    finally:
        provider.close()
    assert (response.text, response.output_tokens) == ('{"a": 1}', expected)


@pytest.mark.parametrize(("count", "expected"), [(10**9, 10**9), (10**9 + 1, None), (10**20, None)])
def test_a_count_above_the_ceiling_at_a_length_stop_gives_none(
    count: int, expected: int | None
) -> None:
    error = _stopped(_chunk("abc") + _chunk(None, "length", usage={"completion_tokens": count}))
    assert (error.output_chars, error.output_tokens) == (3, expected)


def test_the_response_field_refuses_a_count_above_the_ceiling() -> None:
    assert LLMResponse(text="", provider="p", model="m", output_tokens=10**9).output_tokens == 10**9
    for bad in (10**9 + 1, 10**20):
        with pytest.raises(ValueError):
            LLMResponse(text="", provider="p", model="m", output_tokens=bad)


def test_the_token_limit_error_drops_a_count_above_the_ceiling() -> None:
    error = TokenLimitError(output_chars=10**9 + 1, output_tokens=10**20)
    assert (error.output_chars, error.output_tokens) == (None, None)
    kept = TokenLimitError(output_chars=10**9, output_tokens=10**9)
    assert (kept.output_chars, kept.output_tokens) == (10**9, 10**9)


# ---- the body `complete()` actually sends ------------------------------------------------------


def test_complete_sends_exactly_the_golden_body() -> None:
    sent: list[bytes] = []

    def capturing(url: str, body: bytes, timeout: float) -> bytes:
        sent.append(body)
        return _ok(_usage_only({"completion_tokens": 3}))

    provider = OpenAICompatibleProvider("m1", "http://localhost:8000/v1", transport=capturing)
    try:
        asyncio.run(provider.complete(REQUEST))
    finally:
        provider.close()
    assert sent == [GOLDEN_BODY]
    assert "stream_options" not in json.loads(sent[0])
