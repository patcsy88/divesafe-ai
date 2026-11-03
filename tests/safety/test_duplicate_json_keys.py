"""A model reply that repeats a JSON key is ambiguous. It is refused, never resolved to the last
value (AGENTS.md section 12: validate, never repair)."""

from __future__ import annotations

import asyncio

import pytest

from divesafe.agents.runner import AgentError, call_structured
from divesafe.domain import Finding
from divesafe.models import LLMRequest, LLMResponse

pytestmark = pytest.mark.safety

VALID = '{"agent": "a", "summary": "s", "no_evidence": true}'


class _Reply:
    name, model, external = "reply", "m", False

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.text, provider=self.name, model=self.model)


def _call(text: str) -> Finding:
    return asyncio.run(
        call_structured(
            _Reply(text),  # type: ignore[arg-type]
            task="t",
            user_message="x",
            output=Finding,
            allowed_ids=frozenset(),
        )
    )


def test_a_reply_without_repeated_keys_is_accepted() -> None:
    assert _call(VALID).agent == "a"


@pytest.mark.parametrize(
    "text",
    [
        '{"agent": "a", "agent": "b", "summary": "s", "no_evidence": true}',
        '{"agent": "a", "summary": "s", "no_evidence": false, "no_evidence": true}',
        '{"agent": "a", "summary": "s", "no_evidence": true, "x": {"k": 1, "k": 2}}',
    ],
)
def test_a_reply_that_repeats_a_key_is_refused_with_the_schema_label(text: str) -> None:
    with pytest.raises(AgentError) as caught:
        _call(text)
    assert caught.value.label == "schema"
    assert text not in str(caught.value)


def test_a_refused_reply_carries_no_parser_cause() -> None:
    with pytest.raises(AgentError) as caught:
        _call('{"agent": "a", "agent": "b"}')
    assert caught.value.kind == "rejected"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_nested_duplicate_keys_are_refused_by_the_hook_itself() -> None:
    import json

    from divesafe.agents.runner import _no_duplicate_keys

    with pytest.raises(ValueError):
        json.loads('{"x": {"k": 1, "k": 2}}', object_pairs_hook=_no_duplicate_keys)
    assert json.loads('{"x": {"k": 1}}', object_pairs_hook=_no_duplicate_keys) == {"x": {"k": 1}}


def test_a_whitespace_padded_unterminated_fence_is_rejected_quickly() -> None:
    import time

    start = time.monotonic()
    with pytest.raises(AgentError) as caught:
        _call("```" + " " * 19_990 + "x")
    assert time.monotonic() - start < 10.0  # a loose bound: catches a hang, not a slow runner
    assert caught.value.label == "schema"


@pytest.mark.parametrize("wrapper", ["```json\n{}\n```", "```\n{}\n```", "```json{}```"])
def test_a_fenced_reply_is_still_unwrapped(wrapper: str) -> None:
    from divesafe.agents.runner import _json_text

    assert _json_text(wrapper) == "{}"


def test_a_fenced_reply_with_a_repeated_key_is_refused() -> None:
    with pytest.raises(AgentError) as caught:
        _call('```json\n{"agent": "a", "agent": "b", "summary": "s", "no_evidence": true}\n```')
    assert caught.value.kind == "rejected"
    assert caught.value.label == "schema"


def test_a_risk_proposal_that_repeats_the_recommendation_is_refused() -> None:
    from divesafe.agents.schemas import ProposalOutput

    text = (
        '{"recommendation": "NO-GO", "rationale": "r", "evidence_ids": ["e"],'
        ' "recommendation": "GO"}'
    )
    with pytest.raises(AgentError) as caught:
        asyncio.run(
            call_structured(
                _Reply(text),  # type: ignore[arg-type]
                task="t",
                user_message="x",
                output=ProposalOutput,
                allowed_ids=frozenset({"e"}),
            )
        )
    assert caught.value.kind == "rejected"
    assert caught.value.label == "schema"


def test_a_rejected_reply_leaves_no_reply_text_in_the_exception_context() -> None:
    """The error is raised outside the `except`, so `__context__` cannot carry model text."""
    secret = "SECRET-REPLY-TEXT"
    for text in (f'{{"summary": "{secret}", "summary": "b"}}', f"not json {secret}"):
        with pytest.raises(AgentError) as caught:
            _call(text)
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None
        assert secret not in str(caught.value)


@pytest.mark.parametrize("failure", ["error", "timeout", "token_limit"])
def test_a_provider_error_leaves_no_provider_text_in_the_exception_context(failure: str) -> None:
    from divesafe.models import TokenLimitError

    class _Boom:
        name, model, external = "boom", "m", False

        async def complete(self, request: LLMRequest) -> LLMResponse:
            if failure == "timeout":
                raise TimeoutError("SECRET-PROVIDER-TEXT")
            if failure == "token_limit":
                raise TokenLimitError("SECRET-PROVIDER-TEXT")
            raise RuntimeError("SECRET-PROVIDER-TEXT")

    with pytest.raises(AgentError) as caught:
        asyncio.run(
            call_structured(
                _Boom(), task="t", user_message="m", output=Finding, allowed_ids=frozenset()
            )
        )
    assert caught.value.__context__ is None
    assert "SECRET" not in str(caught.value)
