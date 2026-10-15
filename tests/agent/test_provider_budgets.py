"""A provider may declare longer time budgets; the agents honour them and never shorten them."""

from __future__ import annotations

import asyncio

import pytest

from divesafe.agents import runner
from divesafe.agents.runner import AgentError, call_structured, call_timeout, stage_timeout
from divesafe.agents.specialists import Finding
from divesafe.models import LLMRequest, LLMResponse


class _Slow:
    name, model, external = "slow", "m", False

    def __init__(self, delay: float, **budgets: object) -> None:
        self.delay = delay
        for key, value in budgets.items():
            setattr(self, key, value)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(self.delay)
        return LLMResponse(text="{}", provider=self.name, model=self.model)


def test_defaults_apply_when_a_provider_declares_nothing_or_nonsense() -> None:
    for bad in (None, 0, -5, "9", True, float("inf"), float("nan")):
        assert call_timeout(_Slow(0, call_timeout_seconds=bad)) == runner.CALL_TIMEOUT_SECONDS
        assert stage_timeout(_Slow(0, stage_timeout_seconds=bad)) == runner.STAGE_TIMEOUT_SECONDS
    assert call_timeout(_Slow(0)) == runner.CALL_TIMEOUT_SECONDS


def test_a_declared_budget_is_used() -> None:
    p = _Slow(0, call_timeout_seconds=200, stage_timeout_seconds=900.5)
    assert (call_timeout(p), stage_timeout(p)) == (200.0, 900.5)


def test_a_call_waits_for_the_declared_budget_and_times_out_at_it() -> None:
    kwargs = dict(task="t", user_message="x", output=Finding, allowed_ids=frozenset())
    with pytest.raises(AgentError, match="timed out"):
        asyncio.run(call_structured(_Slow(0.3, call_timeout_seconds=0.05), **kwargs))  # type: ignore[arg-type]
    with pytest.raises(AgentError) as caught:  # waits past the 0.05 s above, so it did not time out
        asyncio.run(call_structured(_Slow(0.1, call_timeout_seconds=5), **kwargs))  # type: ignore[arg-type]
    assert "timed out" not in str(caught.value)


def test_an_absurd_declared_budget_is_capped_not_trusted() -> None:
    assert call_timeout(_Slow(0, call_timeout_seconds=1e9)) == runner.CALL_TIMEOUT_CEILING_SECONDS
    assert (
        stage_timeout(_Slow(0, stage_timeout_seconds=1e9)) == runner.STAGE_TIMEOUT_CEILING_SECONDS
    )
    assert call_timeout(_Slow(0, call_timeout_seconds=10**400)) == runner.CALL_TIMEOUT_SECONDS


def test_the_output_token_cap_defaults_and_ignores_nonsense() -> None:
    from divesafe.agents.runner import output_tokens

    assert output_tokens(_Slow(0)) == runner.MAX_OUTPUT_TOKENS
    for bad in (None, 0, -1, True, 1.5, "4000", float("nan")):
        assert output_tokens(_Slow(0, max_output_tokens=bad)) == runner.MAX_OUTPUT_TOKENS


def test_a_declared_output_cap_is_used_and_capped_at_the_ceiling() -> None:
    from divesafe.agents.runner import output_tokens

    assert output_tokens(_Slow(0, max_output_tokens=4000)) == 4000
    assert output_tokens(_Slow(0, max_output_tokens=10**9)) == runner.MAX_OUTPUT_TOKENS_CEILING


def test_the_request_carries_the_providers_output_cap() -> None:
    seen: list[int | None] = []

    class _Spy(_Slow):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            seen.append(request.max_output_tokens)
            return await super().complete(request)

    kwargs = dict(task="t", user_message="x", output=Finding, allowed_ids=frozenset())
    for provider in (_Spy(0), _Spy(0, max_output_tokens=4000)):
        with pytest.raises(AgentError):
            asyncio.run(call_structured(provider, **kwargs))  # type: ignore[arg-type]
    assert seen == [runner.MAX_OUTPUT_TOKENS, 4000]


def test_the_output_cap_boundaries_and_ceilings_agree() -> None:
    from divesafe.agents.runner import output_tokens
    from divesafe.config.settings import Settings
    from divesafe.models import ollama
    from divesafe.models.ollama import OllamaProvider
    from divesafe.models.openai_compatible import OpenAICompatibleProvider

    assert runner.MAX_OUTPUT_TOKENS_CEILING == ollama.MAX_OUTPUT_TOKENS_CEILING == 8000
    assert Settings(_env_file=None).llm_max_output_tokens == runner.MAX_OUTPUT_TOKENS
    for ok in (256, 8000):
        assert Settings(_env_file=None, llm_max_output_tokens=ok).llm_max_output_tokens == ok
    for ok in (1, 8000):
        assert output_tokens(_Slow(0, max_output_tokens=ok)) == ok
        assert OllamaProvider("m", "http://localhost:11434", max_output_tokens=ok)
        assert OpenAICompatibleProvider("m", "http://localhost:8000/v1", max_output_tokens=ok)


@pytest.mark.parametrize("bad", [float("nan"), 1500.0, 10**9, "1500", None, True])
def test_both_constructors_refuse_a_bad_output_cap(bad: object) -> None:
    from divesafe.models.ollama import OllamaProvider
    from divesafe.models.openai_compatible import OpenAICompatibleProvider

    with pytest.raises(ValueError):
        OllamaProvider("m", "http://localhost:11434", max_output_tokens=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("m", "http://localhost:8000/v1", max_output_tokens=bad)  # type: ignore[arg-type]


def test_a_request_bounds_its_output_tokens() -> None:
    from pydantic import ValidationError

    for bad in (0, -1, 8001):
        with pytest.raises(ValidationError):
            LLMRequest(messages=(), max_output_tokens=bad)


def test_a_reply_stopped_at_the_token_limit_has_its_own_fixed_message() -> None:
    from divesafe.models.llm import TokenLimitError

    class _Cut:
        name, model, external = "cut", "m", False

        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise TokenLimitError

    kwargs = dict(task="t", user_message="x", output=Finding, allowed_ids=frozenset())
    with pytest.raises(AgentError) as caught:
        asyncio.run(call_structured(_Cut(), **kwargs))  # type: ignore[arg-type]
    assert str(caught.value) == "t: output stopped at the token limit"
