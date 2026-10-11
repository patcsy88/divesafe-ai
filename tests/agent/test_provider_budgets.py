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
