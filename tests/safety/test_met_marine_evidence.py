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


def _engine_result(*, may_support_go: bool):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.safety.test_rule_definitions import _iso, person
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
    # Sign-off dates relative to THIS file's clock, so the rule is in force here.
    signed = _iso(now - timedelta(days=2))
    definition = _wind_definition(
        reviews=[{"reviewer": person("Reviewer Two"), "reviewed_on": signed}],
        readback_confirmed_on=signed,
        signed_off_on=signed,
        expires_on=_iso(now + timedelta(days=30)),
    )
    rules = [DefinitionRule(definition)]
    engine = RiskRulesEngine(
        rules, policy, "synthetic", required_factors=frozenset({RiskFactorKind.WIND})
    )
    return engine.assess(plan, list(_items()), now)


class _GoRule:
    """Synthetic: always says GO and cites the MET wind item, as a careless rule might."""

    rule_id = "synthetic.go"
    citation = "test fixture (synthetic, not a real safety source)"
    expires_at = datetime(2027, 1, 1, tzinfo=UTC)

    def evaluate(self, plan, evidence):  # type: ignore[no-untyped-def]
        from divesafe.domain import (
            DataCategory,
            Recommendation,
            RiskFactorKind,
            RuleResult,
            ThresholdStatus,
        )

        cited = tuple(e.id for e in evidence if e.category is DataCategory.WIND)
        return RuleResult(
            rule_id=self.rule_id,
            outcome=Recommendation.GO,
            rationale="synthetic",
            evidence_ids=cited,
            factor=RiskFactorKind.WIND,
            threshold_status=ThresholdStatus.VALIDATED,
        )


def _go_result(*, may_support_go: bool):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from divesafe.domain import DataCategory, DivePlan, RiskFactorKind
    from divesafe.risk import EvidencePolicy, RiskRulesEngine

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
    engine = RiskRulesEngine(
        [_GoRule()], policy, "synthetic", required_factors=frozenset({RiskFactorKind.WIND})
    )
    return engine.assess(plan, list(_items()), now)


def test_a_go_resting_on_met_daily_items_is_blocked_by_the_quality_guard_alone() -> None:
    blocked = _go_result(may_support_go=False)
    assert blocked.recommendation.value == "INSUFFICIENT EVIDENCE"
    guard = next(
        r for r in blocked.rule_results if r.rule_id == "evidence.quality_insufficient_for_go"
    )
    assert "wind" in guard.rationale and "waves_swell" in guard.rationale
    # Control, not an endorsement: with the guard switched off by policy the same rule gives GO, so
    # the guard is what stopped it. When the rule that a coarse daily item cannot stand in for a
    # time-resolved one lands (docs/risk-model.md, MET known gap), this control must become non-GO;
    # changing it then is the intended tightening, not a weakening.
    control = _go_result(may_support_go=True)
    assert control.recommendation.value == "GO"
    assert all(r.rule_id != "evidence.quality_insufficient_for_go" for r in control.rule_results)


def test_a_signed_wind_limit_cannot_be_evaluated_from_a_met_range() -> None:
    result = _engine_result(may_support_go=True)
    assert result.recommendation.value == "INSUFFICIENT EVIDENCE"
    assert not any(r.rule_id.startswith("rule.error") for r in result.rule_results)
    (wind,) = [r for r in result.rule_results if r.rule_id == "definition.synthetic.wind_speed"]
    assert wind.outcome.value == "INSUFFICIENT EVIDENCE" and not wind.evidence_ids
    assert wind.rationale.startswith("No 'wind_speed_10m' evidence overlaps")
