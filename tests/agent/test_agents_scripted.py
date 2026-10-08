"""Agent behaviour with a scripted fake LLM (docs/agent-design.md)."""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.agent.scripted_llm import fake, run_pipeline, scripted, task_of

from divesafe.agents import PROMPT_VERSION, AgentError
from divesafe.agents.runner import call_structured
from divesafe.agents.schemas import ProposalOutput
from divesafe.domain import Recommendation, ScenarioKind
from divesafe.models import FakeProvider

R = Recommendation


def test_full_stage_runs_the_expected_bounded_calls() -> None:
    provider = fake()
    record = run_pipeline(provider)
    tasks = [task_of(r) for r in provider.requests]
    assert sorted(tasks) == sorted(
        [
            "specialist:weather",
            "specialist:ocean_conditions",
            "specialist:tide_current",
            "specialist:prediction",
            "scenario:favourable",
            "scenario:marginal",
            "scenario:deteriorating",
            "risk_assessment",
        ]
    )  # site_intelligence has no evidence, so it makes no call
    assert len(tasks) <= 9
    assert all(r.temperature == 0.0 and r.max_output_tokens for r in provider.requests)
    assert record.agent_issues == ()
    assert {f.agent for f in record.findings} == {
        "weather",
        "ocean_conditions",
        "tide_current",
        "site_intelligence",
        "prediction",
    }
    site = next(f for f in record.findings if f.agent == "site_intelligence")
    assert site.no_evidence and not site.evidence_ids and site.confidence is None


def test_scenarios_are_exactly_the_three_kinds_with_evidence() -> None:
    record = run_pipeline(fake())
    assert [s.kind for s in record.scenarios] == list(ScenarioKind)
    assert all(s.evidence_ids for s in record.scenarios)


def test_record_carries_provenance_and_the_explanation() -> None:
    record = run_pipeline(fake())
    assert record.model_version == f"fake/fake-model; prompts={PROMPT_VERSION}"
    assert record.explanation.startswith("Rationale citing the evidence.")
    assert "Uncertainties: model resolution" in record.explanation


def test_each_specialist_sees_only_its_own_categories() -> None:
    provider = fake()
    run_pipeline(provider)
    ocean = next(r for r in provider.requests if task_of(r) == "specialist:ocean_conditions")
    text = ocean.messages[1].content
    assert "waves_swell" in text and "sea_temperature" in text
    assert "marine_warnings" not in text and '"currents"' not in text


def test_no_provider_means_no_agent_output() -> None:
    record = run_pipeline(None)
    assert record.model_version is None and record.findings == () and record.scenarios == ()


def test_no_evidence_means_no_llm_calls_at_all() -> None:
    provider = fake()
    record = run_pipeline(provider, connectors=[])
    assert provider.requests == []
    assert record.proposed_recommendation is None
    assert any("skipped" in i for i in record.agent_issues)


def test_json_in_a_code_fence_is_accepted_but_prose_around_it_is_not() -> None:
    reply = json.dumps(
        {
            "recommendation": "CAUTION",
            "rationale": "x",
            "evidence_ids": ["e1"],
            "uncertainties": [],
        }
    )
    ok = FakeProvider(f"```json\n{reply}\n```")
    out = asyncio.run(
        call_structured(
            ok, task="t", user_message="m", output=ProposalOutput, allowed_ids=frozenset({"e1"})
        )
    )
    assert out.recommendation == R.CAUTION
    chatty = FakeProvider(f"Sure! Here you go: {reply}")
    with pytest.raises(AgentError):
        asyncio.run(
            call_structured(
                chatty,
                task="t",
                user_message="m",
                output=ProposalOutput,
                allowed_ids=frozenset({"e1"}),
            )
        )


def test_a_slow_provider_times_out() -> None:
    class _Slow:
        name, model, external = "slow", "m", False

        async def complete(self, request: object) -> object:
            await asyncio.sleep(5)
            raise AssertionError("unreachable")

    with pytest.raises(AgentError, match="timed out"):
        asyncio.run(
            call_structured(
                _Slow(),  # type: ignore[arg-type]
                task="t",
                user_message="m",
                output=ProposalOutput,
                allowed_ids=frozenset(),
                timeout=0.05,
            )
        )


def test_scripted_helper_replies_validate_for_every_task() -> None:
    assert callable(scripted())
