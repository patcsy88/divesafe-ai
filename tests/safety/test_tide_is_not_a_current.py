"""A tide prediction is never evidence of a current, slack water or flow timing (ADR 0021)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.agent.scripted_llm import fake, task_of

from divesafe.agents.prompts import CLOSE_TAG, OPEN_TAG, tide_note
from divesafe.agents.risk_agent import propose
from divesafe.agents.specialists import SPECIALISTS, run_specialist
from divesafe.agents.tot import KINDS, _one
from divesafe.api.schemas import AssessmentView
from divesafe.data import TIOMAN_ISLAND, PhnEtideConnector
from divesafe.domain import (
    FACTOR_METRICS,
    SUPPORTED_METRICS,
    AssessmentRecord,
    DataCategory,
    DivePlan,
    Recommendation,
    RiskFactorKind,
)
from divesafe.risk import placeholder_rules

pytestmark = pytest.mark.safety

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures/phn_etide_tioman_recorded_2026-10-17.json"
    ).read_text()
)
NOW = datetime(2026, 10, 17, 8, 2, tzinfo=UTC)
START = datetime(2026, 10, 17, 1, 0, tzinfo=UTC)
PLAN = DivePlan(
    site_id=TIOMAN_ISLAND.id, planned_start=START, planned_duration_minutes=60, max_depth_m=18
)
PROHIBITION = "never infer current speed, direction, slack water or flow timing"


class _Getter:
    async def get_json(self, url: str, params: object) -> object:
        return FIXTURE


def _items():  # type: ignore[no-untyped-def]
    connector = PhnEtideConnector(_Getter())
    return list(
        asyncio.run(connector.fetch(TIOMAN_ISLAND, START, START + timedelta(hours=2), NOW)).items
    )


def _user_message(provider) -> str:  # type: ignore[no-untyped-def]
    (request,) = provider.requests
    return str(request.messages[1].content)


def _outside_the_untrusted_block(message: str) -> str:
    return message[: message.index(OPEN_TAG)] + message[message.index(CLOSE_TAG) :]


def _assert_prohibition_is_trusted(message: str) -> None:
    assert PROHIBITION in _outside_the_untrusted_block(message)
    start, end = message.index(OPEN_TAG), message.index(CLOSE_TAG)
    assert PROHIBITION not in message[start:end]


TIDE_READERS = [s.name for s in SPECIALISTS if DataCategory.TIDES in s.categories]


@pytest.mark.parametrize("name", TIDE_READERS)
def test_every_specialist_that_reads_tides_is_told_a_tide_is_not_a_current(name: str) -> None:
    items = _items()
    assert items
    provider = fake()
    spec = next(s for s in SPECIALISTS if s.name == name)
    asyncio.run(run_specialist(provider, spec, PLAN, items))
    _assert_prohibition_is_trusted(_user_message(provider))


def test_the_tide_current_focus_no_longer_invites_slack_water_from_tide_times() -> None:
    spec = next(s for s in SPECIALISTS if s.name == "tide_current")
    assert "if the evidence shows it" not in spec.focus
    assert "Never infer current speed, direction, slack water" in spec.focus


def test_the_scenario_and_risk_prompts_carry_the_same_prohibition() -> None:
    items = _items()
    assert items
    for kind in KINDS:
        provider = fake()
        asyncio.run(_one(provider, kind, PLAN, items, [], [], []))
        _assert_prohibition_is_trusted(_user_message(provider))
    provider = fake()
    asyncio.run(propose(provider, PLAN, items, [], [], [], Recommendation.INSUFFICIENT_EVIDENCE))
    assert task_of(provider.requests[0]) == "risk_assessment"
    _assert_prohibition_is_trusted(_user_message(provider))


def test_the_prohibition_is_absent_when_there_is_no_tide_item() -> None:
    assert tide_note([]) == ""
    assert {"tide_current", "prediction"} <= set(TIDE_READERS)
    other = _items()[0].model_copy(update={"category": DataCategory.CURRENTS})
    assert tide_note([other]) == ""
    assert tide_note(_items()) != ""


def test_no_signed_limit_can_ever_be_expressed_on_a_tide_metric() -> None:
    """Tidal current has no metric at all, and no metric of any factor reads the tides category."""
    assert not FACTOR_METRICS.get(RiskFactorKind.TIDAL_CURRENT, frozenset())
    assert all(m.category is not DataCategory.TIDES for m in SUPPORTED_METRICS.values())


def test_the_tidal_current_placeholder_does_not_cite_tide_predictions() -> None:
    rule = next(r for r in placeholder_rules() if r.factor is RiskFactorKind.TIDAL_CURRENT)
    assert DataCategory.TIDES not in rule.categories
    result = rule.evaluate(PLAN, _items())
    assert result.evidence_ids == ()


def test_the_user_view_credits_phn() -> None:
    from divesafe.orchestration import assess_dive
    from divesafe.risk import EvidencePolicy, RiskRulesEngine

    engine = RiskRulesEngine(
        list(placeholder_rules()),
        EvidencePolicy(frozenset({DataCategory.TIDES}), timedelta(hours=1)),
        "synthetic",
    )
    later = DivePlan(
        site_id=TIOMAN_ISLAND.id,
        planned_start=NOW + timedelta(hours=1),
        planned_duration_minutes=60,
        max_depth_m=18,
    )
    record: AssessmentRecord = asyncio.run(
        assess_dive(
            assessment_id="a-1",
            plan=later,
            site=TIOMAN_ISLAND,
            connectors=[PhnEtideConnector(_Getter())],
            engine=engine,
            now=NOW,
        )
    )
    assert record.evidence
    assert "Pusat Hidrografi Nasional (e-Tide)" in AssessmentView.of(record).attributions


def _redated(day: int, *, invert: bool = False) -> dict:  # type: ignore[type-arg]
    """SYNTHETIC: the recorded day moved to another date (optionally upside down)."""
    block = json.loads(json.dumps(FIXTURE["current_day"]))
    label = f"{day:02d}-Oct-26"
    block["date"] = f"{day} October 2026"
    for row in block["data"]:
        row["date"] = label
        if invert:
            row["height"] = round(3.0 - row["height"], 2)
    for entry in block["MAX_LOW_Tide"]["HL"]:
        entry["date"] = label
        if invert:
            entry["height"] = round(3.0 - entry["height"], 2)
            entry["stat"] = 1 - entry["stat"]
    return block


def _fetch(payload: object, hours: int = 2, days: int = 0):  # type: ignore[no-untyped-def]
    class _G:
        async def get_json(self, url: str, params: object) -> object:
            return payload

    end = START + timedelta(days=days, hours=hours)
    return asyncio.run(PhnEtideConnector(_G()).fetch(TIOMAN_ISLAND, START, end, NOW))


def test_a_list_missing_a_high_or_low_is_never_passed_as_a_complete_day() -> None:
    from divesafe.data import ConnectorResponseError

    for keep in ([0, 3], [1, 2, 3], [0, 1, 2]):
        payload = json.loads(json.dumps(FIXTURE))
        hl = payload["current_day"]["MAX_LOW_Tide"]["HL"]
        payload["current_day"]["MAX_LOW_Tide"]["HL"] = [hl[i] for i in keep]
        with pytest.raises(ConnectorResponseError):
            _fetch(payload)
    assert _fetch(FIXTURE).items


def test_highs_and_lows_that_do_not_alternate_across_midnight_are_refused() -> None:
    from divesafe.data import ConnectorResponseError

    payload = json.loads(json.dumps(FIXTURE))
    payload["prev_day"] = _redated(18, invert=True)
    with pytest.raises(ConnectorResponseError, match="across midnight"):
        _fetch(payload, hours=0, days=1)
