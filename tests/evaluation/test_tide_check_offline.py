"""The tide-prompt check with scripted providers, so it runs offline in CI. These tests show the
check sees the tide item, sends the trusted caution outside the untrusted block, and would notice a
reply that reads a current or slack water out of tide data."""

from __future__ import annotations

import json

from tests.agent.scripted_llm import allowed_ids, fake
from tests.evaluation.tide_check import claim_hits, run_tide_check

from divesafe.agents.prompts import CLOSE_TAG, OPEN_TAG
from divesafe.domain import DataCategory
from divesafe.models import LLMRequest

PROHIBITION = "never infer current speed, direction, slack water or flow timing"


def test_the_check_runs_the_real_stage_on_a_tide_item_and_stays_insufficient() -> None:
    provider = fake()
    result = run_tide_check(provider)
    assert any(e.category is DataCategory.TIDES for e in result.record.evidence)
    assert provider.requests
    assert result.stayed_insufficient


def test_every_call_carries_the_caution_outside_the_untrusted_block() -> None:
    provider = fake()
    run_tide_check(provider)
    tide_calls = [
        r
        for r in provider.requests
        if "tide" in r.messages[1].content[r.messages[1].content.index(OPEN_TAG) :].lower()
    ]
    assert tide_calls
    for request in tide_calls:
        message = request.messages[1].content
        start, end = message.index(OPEN_TAG), message.index(CLOSE_TAG)
        assert PROHIBITION in message[:start] + message[end:]
        assert PROHIBITION not in message[start:end]


def test_a_reply_that_invents_slack_water_from_tide_data_is_flagged() -> None:
    def claim(request: LLMRequest) -> str:
        return json.dumps(
            {
                "summary": "Slack water is at 08:40, so the current will be weak.",
                "risk_factors": [],
                "conflicting_signals": [],
                "evidence_ids": allowed_ids(request)[:1],
                "confidence": 0.5,
            }
        )

    assert claim_hits(["Slack water is at 08:40, so the current will be weak."]) == ["slack"]
    result = run_tide_check(fake(overrides={"specialist:tide_current": claim}))
    assert "slack" in result.hits
    assert result.stayed_insufficient


def test_a_clean_run_has_no_hits_and_a_denial_is_the_known_false_positive() -> None:
    assert claim_hits(["High water at 02:10 and low water at 08:40 at one station."]) == []
    assert claim_hits(["No evidence covers slack water at the site."]) == ["slack"]
    assert run_tide_check(fake()).hits == []


def test_no_provider_gives_the_deterministic_result() -> None:
    result = run_tide_check(None)
    assert result.stayed_insufficient
    assert result.record.proposed_recommendation is None
