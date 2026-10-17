"""PHN tide predictions must never act as a current, a reading, a limit input or a route to GO."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from divesafe.agents.prompts import evidence_payload
from divesafe.data import TIOMAN_ISLAND, PhnEtideConnector
from divesafe.domain import (
    DataCategory,
    DivePlan,
    Recommendation,
    RiskFactorKind,
    RuleResult,
    ThresholdStatus,
    observation_from_evidence,
)
from divesafe.risk import EvidencePolicy, RiskRulesEngine, placeholder_rules

pytestmark = pytest.mark.safety

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures/phn_etide_tioman_recorded_2026-10-17.json"
    ).read_text()
)
NOW = datetime(2026, 10, 17, 8, 2, tzinfo=UTC)
START = datetime(2026, 10, 17, 1, 0, tzinfo=UTC)
END = datetime(2026, 10, 17, 5, 0, tzinfo=UTC)


class _Getter:
    async def get_json(self, url: str, params: object) -> object:
        return FIXTURE


def _items():  # type: ignore[no-untyped-def]
    return asyncio.run(PhnEtideConnector(_Getter()).fetch(TIOMAN_ISLAND, START, END, NOW)).items


def _plan() -> DivePlan:
    return DivePlan(
        site_id=TIOMAN_ISLAND.id,
        planned_start=START,
        planned_duration_minutes=60,
        max_depth_m=18,
    )


def test_tide_items_have_no_typed_reading() -> None:
    for item in _items():
        assert observation_from_evidence(item) is None


def test_tide_items_reach_the_agents_as_a_prediction_that_is_not_a_current() -> None:
    for payload in evidence_payload(_items()):
        assert payload.get("conditions") != {}
        assert payload["category"] == "tides" and payload["data_kind"] == "prediction"
        assert payload["is_forecast"] is True and payload["quality"] == "degraded"
        assert payload["upstream_known"] is False
        value = payload["value"]
        assert "no current speed" in value["not_a_current"]
        assert "not the dive site" in value["spatial_scope"]
        assert value["tide"]["unit"] == "m" and value["tide"]["extremes"]
        assert "no current speed" in " ".join(payload["quality_notes"])
        assert "current_speed" not in json.dumps(value) and "slack" not in value["tide"]


def test_no_tide_value_uses_a_current_or_hourly_key() -> None:
    for item in _items():
        assert "variables" not in item.value and "units" not in item.value
        assert not any("current" in key and key != "not_a_current" for key in item.value)


def _result(rules, *, may_support_go: bool = False):  # type: ignore[no-untyped-def]
    policy = EvidencePolicy(
        frozenset({DataCategory.TIDES}),
        timedelta(hours=1),
        degraded_may_support_go=may_support_go,
    )
    engine = RiskRulesEngine(
        rules, policy, "synthetic", required_factors=frozenset({RiskFactorKind.TIDAL_CURRENT})
    )
    return engine.assess(_plan(), list(_items()), NOW)


def test_tide_evidence_alone_never_gives_go_or_caution_under_the_placeholders() -> None:
    result = _result(list(placeholder_rules()))
    assert result.recommendation not in (Recommendation.GO, Recommendation.CAUTION)
    tidal = [r for r in result.rule_results if r.factor is RiskFactorKind.TIDAL_CURRENT]
    assert tidal and all(r.outcome is Recommendation.INSUFFICIENT_EVIDENCE for r in tidal)
    assert all(r.threshold_status is ThresholdStatus.TBD for r in tidal)


class _GoRule:
    """Synthetic: says GO for tidal current and cites the tide item, as a careless rule might."""

    rule_id = "synthetic.go"
    citation = "test fixture (synthetic, not a real safety source)"
    expires_at = datetime(2027, 1, 1, tzinfo=UTC)

    def evaluate(self, plan, evidence):  # type: ignore[no-untyped-def]
        return RuleResult(
            rule_id=self.rule_id,
            outcome=Recommendation.GO,
            rationale="synthetic",
            evidence_ids=tuple(e.id for e in evidence if e.category is DataCategory.TIDES),
            factor=RiskFactorKind.TIDAL_CURRENT,
            threshold_status=ThresholdStatus.VALIDATED,
        )


def test_a_go_resting_on_a_tide_prediction_is_blocked_by_the_quality_guard_alone() -> None:
    blocked = _result([_GoRule()])
    assert blocked.recommendation is Recommendation.INSUFFICIENT_EVIDENCE
    guard = next(
        r for r in blocked.rule_results if r.rule_id == "evidence.quality_insufficient_for_go"
    )
    assert "tides" in guard.rationale
    # Control, not an endorsement: with the guard off by policy the same rule gives GO, so the
    # guard is what stopped it. A rule that a tide height cannot stand in for a current speed
    # must make this control non-GO when it lands; changing it then is a tightening.
    control = _result([_GoRule()], may_support_go=True)
    assert control.recommendation is Recommendation.GO


def test_a_tide_item_is_never_accepted_for_a_stale_or_future_clock() -> None:
    policy = EvidencePolicy(frozenset({DataCategory.TIDES}), timedelta(hours=1))
    engine = RiskRulesEngine(
        list(placeholder_rules()),
        policy,
        "synthetic",
        required_factors=frozenset({RiskFactorKind.TIDAL_CURRENT}),
    )
    for clock in (NOW + timedelta(hours=2), NOW - timedelta(minutes=5)):
        result = engine.assess(_plan(), list(_items()), clock)
        assert result.recommendation is Recommendation.INSUFFICIENT_EVIDENCE
        assert any(r.rule_id.startswith("evidence.required") for r in result.rule_results)


def test_a_tide_item_does_not_cover_a_dive_on_a_day_it_was_not_issued_for() -> None:
    later = DivePlan(
        site_id=TIOMAN_ISLAND.id,
        planned_start=START + timedelta(days=1),
        planned_duration_minutes=60,
        max_depth_m=18,
    )
    policy = EvidencePolicy(frozenset({DataCategory.TIDES}), timedelta(hours=1))
    engine = RiskRulesEngine(
        list(placeholder_rules()),
        policy,
        "synthetic",
        required_factors=frozenset({RiskFactorKind.TIDAL_CURRENT}),
    )
    result = engine.assess(later, list(_items()), NOW)
    assert result.recommendation is Recommendation.INSUFFICIENT_EVIDENCE
    assert any("does not cover" in r.rationale for r in result.rule_results)


def test_the_default_connector_refuses_a_window_beyond_the_horizon_so_no_item_exists() -> None:
    from divesafe.data import ConnectorResponseError

    far = START + timedelta(days=5)
    with pytest.raises(ConnectorResponseError):
        asyncio.run(
            PhnEtideConnector(_Getter()).fetch(TIOMAN_ISLAND, far, far + timedelta(hours=2), NOW)
        )
