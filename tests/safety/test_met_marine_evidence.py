"""MET daily marine ranges must never act as an hourly reading, a limit input or a point value."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from divesafe.agents.prompts import evidence_payload
from divesafe.data import TIOMAN_ISLAND, MetApiMarineConnector
from divesafe.domain import observation_from_evidence

pytestmark = pytest.mark.safety

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures/met_api_marine_tioman_recorded_2026-10-16.json"
    ).read_text()
)


class _Getter:
    async def get_json(self, url: str, params: object) -> object:
        return FIXTURE


def _items():  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
    start = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
    end = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    return asyncio.run(MetApiMarineConnector(_Getter()).fetch(TIOMAN_ISLAND, start, end, now)).items


def test_met_items_have_no_typed_reading() -> None:
    for item in _items():
        assert observation_from_evidence(item) is None


def test_met_items_reach_the_agents_with_their_range_and_never_as_empty_conditions() -> None:
    for payload in evidence_payload(_items()):
        assert payload.get("conditions") != {}
        ranges = payload["value"]["ranges"]
        (entry,) = ranges.values()
        assert (
            entry["low"] <= entry["high"]
            and set(entry) == {"low", "high", "unit", "upper"}
            and entry["unit"] in {"m", "km/h"}
        )
        assert payload["value"]["temporal_scope"].startswith("one value for the whole calendar day")
        assert "not a site maximum" in " ".join(payload["quality_notes"])
        assert "not the dive site" in payload["value"]["spatial_scope"]
        assert payload["valid_until"] is not None
        assert payload["upstream_known"] is False and payload["quality"] == "degraded"


def test_met_values_are_never_stored_under_the_hourly_keys() -> None:
    for item in _items():
        assert "variables" not in item.value and "units" not in item.value
        assert all("worst_case" not in r for r in item.value["ranges"].values())


def _engine_result(*, may_support_go: bool, with_rules: bool):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.safety.test_wind_evidence_safety import _wind_definition

    from divesafe.domain import DataCategory, DivePlan, RiskFactorKind
    from divesafe.risk import DefinitionRule, EvidencePolicy, RiskRulesEngine

    now = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
    plan = DivePlan(
        site_id=TIOMAN_ISLAND.id,
        planned_start=datetime(2026, 10, 16, 2, 0, tzinfo=UTC),
        planned_duration_minutes=60,
        max_depth_m=18,
    )
    policy = EvidencePolicy(
        frozenset({DataCategory.WIND, DataCategory.WAVES_SWELL}),
        timedelta(hours=1),
        degraded_may_support_go=may_support_go,
    )
    rules = [DefinitionRule(_wind_definition())] if with_rules else []
    engine = RiskRulesEngine(
        rules, policy, "synthetic", required_factors=frozenset({RiskFactorKind.WIND})
    )
    return engine.assess(plan, list(_items()), now)


def test_met_daily_items_alone_never_give_go_or_caution_under_the_default_policy() -> None:
    result = _engine_result(may_support_go=False, with_rules=False)
    assert result.recommendation.value == "INSUFFICIENT EVIDENCE"
    assert all(r.outcome.value not in {"GO", "CAUTION"} for r in result.rule_results)


def test_a_signed_wind_limit_cannot_be_evaluated_from_a_met_range() -> None:
    result = _engine_result(may_support_go=True, with_rules=True)
    assert result.recommendation.value == "INSUFFICIENT EVIDENCE"
    assert all(r.outcome.value not in {"GO", "CAUTION"} for r in result.rule_results)
    assert not any(r.rule_id.startswith("rule.error") for r in result.rule_results)
    assert all(
        not r.evidence_ids for r in result.rule_results if r.rule_id == "synthetic.wind_speed"
    )
