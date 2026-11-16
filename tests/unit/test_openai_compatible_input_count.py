"""The adapter's input-token count (`usage.prompt_tokens`, ADR 0027 amendment step 4).

Read under the same rule as `completion_tokens`: from the last event whose `usage` is not null,
through `plain_count`. It is traced only and never changes what is accepted or refused.
No network, no model.
"""

from __future__ import annotations

import asyncio
import copy
import json
import pickle

import pytest
from tests.unit.test_openai_compatible_usage import (
    BAD_USAGE,
    DONE,
    REFUSED,
    REQUEST,
    _chunk,
    _ok,
    _refusal,
    _stopped,
    _usage_only,
    _with_usage,
)

from divesafe.models import LLMResponse, TokenLimitError
from divesafe.models.openai_compatible import (
    OpenAICompatibleError,
    OpenAICompatibleProvider,
    OpenAICompatibleTokenLimitError,
    join_sse,
    join_sse_counted,
    join_sse_usage,
)

BOTH = {"completion_tokens": 5, "prompt_tokens": 40}


def _complete(raw: bytes) -> LLMResponse:
    provider = OpenAICompatibleProvider("m1", "http://localhost:8000/v1", transport=lambda *_: raw)
    try:
        return asyncio.run(provider.complete(REQUEST))
    finally:
        provider.close()


# ---- reading the count -------------------------------------------------------------------------


def test_both_counts_are_read_from_a_usage_only_event() -> None:
    assert join_sse_usage(_ok(_usage_only(BOTH))) == ('{"a": 1}', 5, 40)


def test_both_counts_are_read_from_the_final_choice_event() -> None:
    raw = (_chunk('{"a": 1}') + _chunk(None, "stop", usage=BOTH) + DONE).encode()
    assert join_sse_usage(raw) == ('{"a": 1}', 5, 40)


def test_the_last_non_null_usage_wins_for_both_counts() -> None:
    raw = _ok(
        _usage_only({"completion_tokens": 1, "prompt_tokens": 2}),
        _chunk(None, usage=None),
        _usage_only({"completion_tokens": 3}),
    )
    # The later usage object has no `prompt_tokens`: the earlier 2 is not kept.
    assert join_sse_usage(raw) == ('{"a": 1}', 3, None)


def test_a_prompt_count_alone_is_read() -> None:
    assert join_sse_usage(_ok(_usage_only({"prompt_tokens": 7}))) == ('{"a": 1}', None, 7)


def test_zero_and_no_usage() -> None:
    assert join_sse_usage(_ok(_usage_only({"prompt_tokens": 0}))) == ('{"a": 1}', None, 0)
    assert join_sse_usage(_ok()) == ('{"a": 1}', None, None)


def test_join_sse_counted_and_join_sse_are_unchanged_by_the_prompt_count() -> None:
    raw = _ok(_usage_only(BOTH))
    assert join_sse_counted(raw) == ('{"a": 1}', 5)
    assert join_sse(raw) == '{"a": 1}'


BAD_PROMPT_USAGE: list[object] = [
    {"prompt_tokens": -1},
    {"prompt_tokens": True},
    {"prompt_tokens": False},
    {"prompt_tokens": 5.0},
    {"prompt_tokens": "5"},
    {"prompt_tokens": None},
    {"prompt_tokens": {"n": 5}},
    {"prompt_tokens": [5]},
    {"prompt_tokens": 10**9 + 1},
    {"completion_tokens": 5},
    *[u for u in BAD_USAGE if not (isinstance(u, dict) and "prompt_tokens" in u)],
]


@pytest.mark.parametrize("usage", BAD_PROMPT_USAGE, ids=repr)
def test_a_bad_prompt_count_gives_none_and_the_reply_is_still_accepted(usage: object) -> None:
    for raw in (
        _ok(_usage_only(usage)),
        (_chunk("x") + _chunk(None, "stop", usage=usage) + DONE).encode(),
    ):
        text, _, prompt = join_sse_usage(raw)
        assert text in ('{"a": 1}', "x") and prompt is None
        assert join_sse(raw) == text


@pytest.mark.parametrize(("count", "expected"), [(10**9, 10**9), (10**9 + 1, None), (10**20, None)])
def test_the_prompt_count_ceiling(count: int, expected: int | None) -> None:
    raw = _ok(_usage_only({"prompt_tokens": count}))
    assert join_sse_usage(raw) == ('{"a": 1}', None, expected)
    response = _complete(raw)
    assert (response.text, response.input_tokens) == ('{"a": 1}', expected)


# ---- usage never changes a refusal, with `prompt_tokens` too -----------------------------------


def _with_prompt_usage(stream: str, usage: str) -> list[str]:
    """`_with_usage`, with `prompt_tokens` also on the usage put on the first choice event."""
    variants = _with_usage(stream, usage)
    first_end = stream.index("\n\n") + 2
    first = json.loads(stream[len("data: ") : first_end].strip())
    first["usage"] = {"completion_tokens": 5, "prompt_tokens": 40}
    return [*variants, "data: " + json.dumps(first) + "\n\n" + stream[first_end:]]


@pytest.mark.parametrize(("name", "stream"), REFUSED, ids=[n for n, _ in REFUSED])
def test_a_prompt_count_never_changes_a_refusal(name: str, stream: str) -> None:
    expected = _refusal(stream.encode())
    for usage in (
        _usage_only(BOTH),
        _usage_only({"prompt_tokens": 40}),
        _usage_only({"prompt_tokens": -3}),
        _usage_only({"prompt_tokens": 10**20}),
    ):
        for variant in _with_prompt_usage(stream, usage):
            assert _refusal(variant.encode()) == expected
            with pytest.raises(OpenAICompatibleError) as caught:
                join_sse_usage(variant.encode())
            assert (type(caught.value), str(caught.value)) == expected[::2]


# ---- the response field ------------------------------------------------------------------------


def test_complete_fills_input_tokens() -> None:
    response = _complete(_ok(_usage_only(BOTH)))
    assert (response.output_tokens, response.input_tokens) == (5, 40)
    assert _complete(_ok()).input_tokens is None


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "3", 10**9 + 1, [1], {"n": 1}], ids=repr)
def test_the_response_field_maps_any_bad_value_to_none_and_never_fails(bad: object) -> None:
    response = LLMResponse(text="", provider="p", model="m", input_tokens=bad)  # type: ignore[arg-type]
    assert response.input_tokens is None


def test_the_response_field_keeps_a_plain_count() -> None:
    assert LLMResponse(text="", provider="p", model="m").input_tokens is None
    for ok in (0, 40, 10**9):
        assert LLMResponse(text="", provider="p", model="m", input_tokens=ok).input_tokens == ok


# ---- the token-limit stop ----------------------------------------------------------------------


def test_a_length_stop_carries_the_prompt_count() -> None:
    error = _stopped(
        _chunk("abc") + _chunk(None, "length", usage={"completion_tokens": 9, "prompt_tokens": 40})
    )
    assert (error.output_chars, error.output_tokens, error.input_tokens) == (3, 9, 40)
    assert str(error) == "the model stopped at the output-token limit"


def test_a_length_stop_before_any_usage_has_no_prompt_count() -> None:
    error = _stopped(_chunk("abc") + _chunk(None, "length") + _usage_only(BOTH) + DONE)
    assert (error.output_tokens, error.input_tokens) == (None, None)


@pytest.mark.parametrize("cls", [TokenLimitError, OpenAICompatibleTokenLimitError])
def test_pickle_and_copy_keep_the_prompt_count(cls: type[TokenLimitError]) -> None:
    original = cls(output_chars=12, output_tokens=34, input_tokens=56)
    for clone in (
        pickle.loads(pickle.dumps(original)),  # noqa: S301 (our own bytes)
        copy.copy(original),
        copy.deepcopy(original),
    ):
        assert type(clone) is cls
        assert (clone.output_chars, clone.output_tokens, clone.input_tokens) == (12, 34, 56)


def test_the_error_keeps_only_a_plain_prompt_count() -> None:
    for bad in (True, -2, "9", 1.0, 10**9 + 1):
        assert TokenLimitError(input_tokens=bad).input_tokens is None
    assert TokenLimitError().input_tokens is None
    clone = pickle.loads(pickle.dumps(TokenLimitError(input_tokens="9")))  # noqa: S301
    assert clone.input_tokens is None
