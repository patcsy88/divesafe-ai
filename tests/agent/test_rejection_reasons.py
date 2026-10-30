"""Rejected agent output carries a fixed reason label, and only a rejection does (no live calls)."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.agent import scripted_llm as sl

from divesafe.agents import run_agent_stage
from divesafe.agents.runner import AgentError, call_structured
from divesafe.agents.schemas import ProposalOutput
from divesafe.agents.stage import REJECTION_REASONS, RISK_TASK, TaskOutcome
from divesafe.domain import Recommendation
from divesafe.models import FakeProvider, LLMRequest, LLMResponse

R = Recommendation
KWARGS = {"task": "t", "user_message": "x", "output": ProposalOutput}


class _Raw:
    """A provider that returns a fixed text (or sleeps past its own call budget)."""

    name, model, external = "raw", "m", False

    def __init__(self, text: str = "", delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        if delay:
            self.call_timeout_seconds = 0.05

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self.delay:
            await asyncio.sleep(self.delay)
        return LLMResponse(text=self.text, provider=self.name, model=self.model)


def _proposal(ids: list[str]) -> str:
    return json.dumps(
        {
            "recommendation": "INSUFFICIENT EVIDENCE",
            "rationale": "r",
            "evidence_ids": ids,
            "uncertainties": [],
        }
    )


def _label_of(provider: _Raw, allowed: frozenset[str] = frozenset({"ev-1"})) -> AgentError:
    with pytest.raises(AgentError) as caught:
        asyncio.run(call_structured(provider, allowed_ids=allowed, **KWARGS))  # type: ignore[arg-type]
    return caught.value


def test_a_reply_that_fails_the_schema_is_labelled_schema() -> None:
    error = _label_of(_Raw("not json at all"))
    assert (error.kind, error.label) == ("rejected", "schema")
    assert "not valid for the schema" in str(error)


def test_an_unknown_evidence_id_is_labelled_unknown_evidence_id() -> None:
    error = _label_of(_Raw(_proposal(["ev-ghost"])))
    assert (error.kind, error.label) == ("rejected", "unknown_evidence_id")
    assert "unknown evidence id" in str(error)


def test_an_oversized_reply_is_labelled_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("divesafe.agents.runner.MAX_OUTPUT_CHARS", 10)
    error = _label_of(_Raw("x" * 11))
    assert (error.kind, error.label) == ("rejected", "too_long")


def test_a_timeout_is_not_labelled_and_keeps_its_kind() -> None:
    error = _label_of(_Raw(delay=2.0))
    assert (error.kind, error.label) == ("timed_out", "")


def test_a_valid_reply_raises_nothing() -> None:
    result = asyncio.run(
        call_structured(_Raw(_proposal(["ev-1"])), allowed_ids=frozenset({"ev-1"}), **KWARGS)  # type: ignore[arg-type]
    )
    assert result.evidence_ids == ("ev-1",)


def test_the_default_label_is_empty_so_existing_constructors_still_work() -> None:
    error = AgentError("plain")
    assert (error.kind, error.label) == ("failed", "")


def _stage_outcomes(provider: FakeProvider | _Raw) -> dict[str, TaskOutcome]:
    record = sl.run_pipeline(sl.fake())  # evidence and rule results from a clean run
    stage = asyncio.run(
        run_agent_stage(
            provider, sl.PLAN, record.evidence, record.rule_results, R.INSUFFICIENT_EVIDENCE
        )
    )
    return {t.task: t for t in stage.task_outcomes}


def test_a_specialist_reply_that_fails_the_schema_records_reason_schema() -> None:
    provider = FakeProvider(sl.scripted(overrides={"specialist:weather": "garbage"}))
    outcomes = _stage_outcomes(provider)
    assert outcomes["weather"] == TaskOutcome("weather", "failed", reason="schema")
    assert outcomes["ocean_conditions"].reason == ""


def test_a_specialist_citing_an_unknown_evidence_id_records_that_reason() -> None:
    ghost = json.dumps(
        {
            "summary": "s",
            "risk_factors": [],
            "conflicting_signals": [],
            "evidence_ids": ["ev-ghost"],
            "confidence": 0.5,
        }
    )
    provider = FakeProvider(sl.scripted(overrides={"specialist:weather": ghost}))
    assert _stage_outcomes(provider)["weather"] == TaskOutcome(
        "weather", "failed", reason="unknown_evidence_id"
    )


def test_a_timed_out_specialist_records_no_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    record = sl.run_pipeline(sl.fake())
    stage = asyncio.run(
        run_agent_stage(
            _Raw(delay=2.0),
            sl.PLAN,
            record.evidence,
            record.rule_results,
            R.INSUFFICIENT_EVIDENCE,
        )
    )
    weather = next(t for t in stage.task_outcomes if t.task == "weather")
    assert weather.outcome == "failed" and weather.reason == ""


def test_reasons_are_only_the_fixed_labels() -> None:
    assert {"schema", "unknown_evidence_id", "too_long"} == REJECTION_REASONS
    assert TaskOutcome("x", "ok").reason == ""
    assert RISK_TASK == "risk_assessment"


def test_an_error_with_an_unfixed_label_records_no_reason() -> None:
    from divesafe.agents.stage import _rejection_reason

    assert _rejection_reason(AgentError("m", kind="rejected", label="free text")) == ""
    assert _rejection_reason(AgentError("m", kind="failed", label="schema")) == ""
    assert _rejection_reason(AgentError("m", kind="rejected", label="schema")) == "schema"
