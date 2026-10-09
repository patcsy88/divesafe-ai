"""Wind evidence in the engine and in rule definitions. Limits here are SYNTHETIC test inputs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.safety.test_rule_definitions import _iso, person, raw

from divesafe.data import (
    REDANG_ISLAND,
    DataGovMyWarningConnector,
    OpenMeteoMarineConnector,
    OpenMeteoWindConnector,
)
from divesafe.domain import DataCategory, DivePlan, Recommendation, RiskFactorKind, RuleDefinition
from divesafe.risk import DefinitionRule, EvidencePolicy, RiskRulesEngine
from divesafe.services import gather_evidence

pytestmark = pytest.mark.safety
R = Recommendation
FIXTURES = Path(__file__).parent.parent / "fixtures"
MARINE = json.loads((FIXTURES / "open_meteo_marine_redang_recorded_2026-10-08.json").read_text())
WIND = json.loads((FIXTURES / "open_meteo_wind_redang_recorded_2026-10-08.json").read_text())
WARNINGS = json.loads((FIXTURES / "data_gov_my_warning_recorded_2026-10-09.json").read_text())
NOW = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
START = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
END = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
PLAN = DivePlan(
    site_id=REDANG_ISLAND.id, planned_start=START, planned_duration_minutes=120, max_depth_m=18
)


class _Getter:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        return self._payload


def _evidence() -> list[Any]:
    connectors = [
        OpenMeteoMarineConnector(_Getter(MARINE)),
        OpenMeteoWindConnector(_Getter(WIND)),
        DataGovMyWarningConnector(_Getter(WARNINGS)),
    ]
    gathered = asyncio.run(gather_evidence(connectors, REDANG_ISLAND, START, END, NOW))
    assert gathered.issues == ()
    return list(gathered.items)


def _wind_definition(**changes: Any) -> RuleDefinition:
    base = raw(
        id="synthetic.wind_speed",
        factor="wind",
        metric="wind_speed_10m",
        unit="km/h",
        scope={"site_ids": [REDANG_ISLAND.id]},
        forecast_margin=0.0,
        no_go_when={"comparison": ">", "value": 200.0},
        caution_when={"comparison": ">", "value": 100.0},
        go_when={"comparison": "<=", "value": 100.0},
        # synthetic sign-off dated relative to THIS file's clock, so the rule is not expired
        reviews=[
            {"reviewer": person("Reviewer Two"), "reviewed_on": _iso(NOW - timedelta(days=2))}
        ],
        readback_confirmed_on=_iso(NOW - timedelta(days=2)),
        signed_off_on=_iso(NOW - timedelta(days=2)),
        expires_on=_iso(NOW + timedelta(days=30)),
    )
    base.update(changes)
    return RuleDefinition.model_validate(base)


def _engine(*rules: DefinitionRule, required: set[DataCategory]) -> RiskRulesEngine:
    policy = EvidencePolicy(frozenset(required), timedelta(hours=1), degraded_may_support_go=True)
    return RiskRulesEngine(
        rules, policy, "synthetic", required_factors=frozenset({RiskFactorKind.WIND})
    )


def test_wind_evidence_satisfies_a_wind_requirement_and_only_that() -> None:
    evidence = _evidence()
    present = {e.category for e in evidence}
    assert DataCategory.WIND in present and DataCategory.TIDES not in present
    engine = _engine(required={DataCategory.WIND, DataCategory.TIDES})
    result = engine.assess(PLAN, evidence, NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    reasons = [r.rule_id for r in result.rule_results]
    assert "evidence.required.tides" in reasons and "evidence.required.wind" not in reasons


def test_wind_is_encodable_by_speed_only() -> None:
    assert _wind_definition().metric == "wind_speed_10m"
    for metric in ("wind_gusts_10m", "wind_direction_10m"):
        with pytest.raises(ValidationError, match="no data source"):
            _wind_definition(metric=metric)
    with pytest.raises(ValidationError, match="differs from the data's unit"):
        _wind_definition(unit="m/s")  # no conversion, ever


def test_a_synthetic_wind_definition_reads_the_real_recorded_forecast() -> None:
    evidence = _evidence()
    rule = DefinitionRule(_wind_definition())
    result = rule.evaluate(PLAN, evidence)
    assert result is not None and result.outcome == R.GO
    assert result.factor == RiskFactorKind.WIND
    assert result.evidence_ids and all(
        i.startswith("open-meteo-wind:") for i in result.evidence_ids
    )


def test_the_worst_recorded_wind_decides_not_an_average() -> None:
    evidence = _evidence()
    worst = max(WIND["hourly"]["wind_speed_10m"][1:5])  # 18:00 to 21:00 (bracketing included)
    caution_just_below = _wind_definition(
        no_go_when={"comparison": ">", "value": worst + 50.0},
        caution_when={"comparison": ">", "value": worst - 0.01},
        go_when={"comparison": "<=", "value": worst - 0.01},
    )
    result = DefinitionRule(caution_just_below).evaluate(PLAN, evidence)
    assert result is not None and result.outcome == R.CAUTION  # the single worst hour triggers it
    above = _wind_definition(
        no_go_when={"comparison": ">", "value": worst + 50.0},
        caution_when={"comparison": ">", "value": worst + 0.01},
        go_when={"comparison": "<=", "value": worst + 0.01},
    )
    assert DefinitionRule(above).evaluate(PLAN, evidence).outcome == R.GO  # type: ignore[union-attr]


def test_a_wind_definition_reaches_go_through_the_engine_on_real_data() -> None:
    evidence = _evidence()
    engine = _engine(DefinitionRule(_wind_definition()), required={DataCategory.WIND})
    result = engine.assess(PLAN, evidence, NOW)
    assert result.recommendation == R.GO  # synthetic limits; scope-limited required factor
    assert not any(r.rule_id.startswith("rule.cites_unusable") for r in result.rule_results)


def test_a_wind_no_go_cannot_be_relaxed_by_a_proposal() -> None:
    from divesafe.risk import reconcile

    strict = _wind_definition(
        no_go_when={"comparison": ">", "value": 1.0},
        caution_when={"comparison": ">", "value": 0.5},
        go_when={"comparison": "<=", "value": 0.5},
    )
    engine = _engine(DefinitionRule(strict), required={DataCategory.WIND})
    verdict = engine.assess(PLAN, _evidence(), NOW).recommendation
    assert verdict == R.NO_GO and reconcile(verdict, R.GO).final == R.NO_GO


def test_the_production_policy_still_cannot_give_go_with_wind_present() -> None:
    from divesafe.api.state import build_engine
    from divesafe.config import Settings

    engine = build_engine(Settings(evidence_max_age_minutes=60))
    assert engine is not None
    result = engine.assess(PLAN, _evidence(), NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    ids = [r.rule_id for r in result.rule_results]
    assert "evidence.required.wind" not in ids  # wind now counts as present
    assert "evidence.required.tides" in ids  # tides still do not
    assert RiskFactorKind.WIND in result.unevaluated_factors  # no signed-off limit yet


# --- second review: timing, defaults, disclosure --------------------------------------------------


def _instant(ident: str, at: datetime, speed: float) -> Any:
    base = _evidence()[0]
    category = base.category
    assert category in (DataCategory.WAVES_SWELL, DataCategory.WIND, DataCategory.CURRENTS)
    wind = next(e for e in _evidence() if e.category == DataCategory.WIND)
    return wind.model_copy(
        update={
            "id": ident,
            "valid_at": at,
            "value": {
                **wind.value,
                "variables": {**wind.value["variables"], "wind_speed_10m": speed},
            },
        }
    )


def test_a_spike_at_the_first_sample_after_the_window_counts_as_exposure() -> None:
    """A dive 10:30-11:30 sits between samples stamped 10:00 and 12:00; both bracket it."""
    base = START.replace(hour=10, minute=0)
    plan = PLAN.model_copy(
        update={"planned_start": base + timedelta(minutes=30), "planned_duration_minutes": 60}
    )
    rule = DefinitionRule(_wind_definition())
    calm = [_instant("a", base, 5.0), _instant("b", base + timedelta(hours=1), 5.0)]
    spike_after = [*calm, _instant("c", base + timedelta(hours=2), 250.0)]
    spike_before = [_instant("z", base - timedelta(hours=1), 250.0), *calm]
    assert rule.evaluate(plan, calm).outcome == R.GO  # type: ignore[union-attr]
    assert rule.evaluate(plan, spike_after).outcome == R.NO_GO  # type: ignore[union-attr]
    assert rule.evaluate(plan, spike_before).outcome == R.GO  # type: ignore[union-attr]  # nearest is calm


def test_the_same_wind_definition_cannot_give_go_under_the_default_degraded_policy() -> None:
    policy = EvidencePolicy(frozenset({DataCategory.WIND}), timedelta(hours=1))  # defaults
    engine = RiskRulesEngine(
        [DefinitionRule(_wind_definition())],
        policy,
        "synthetic",
        required_factors=frozenset({RiskFactorKind.WIND}),
    )
    result = engine.assess(PLAN, _evidence(), NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id == "evidence.quality_insufficient_for_go" for r in result.rule_results)


def test_a_wind_result_says_gusts_and_direction_were_not_evaluated() -> None:
    result = DefinitionRule(_wind_definition()).evaluate(PLAN, _evidence())
    assert result is not None
    assert "Not evaluated by this rule: gusts, direction." in result.rationale
    from divesafe.domain import SUPPORTED_METRICS

    assert SUPPORTED_METRICS["wind_speed_10m"].not_evaluated == ("gusts", "direction")


def test_the_api_note_reports_partly_evaluated_factors() -> None:
    from tests.agent.scripted_llm import run_pipeline

    from divesafe.api.schemas import AssessmentView

    wind_rule = DefinitionRule(
        _wind_definition(scope={"site_ids": [REDANG_ISLAND.id]}, forecast_margin=0.0)
    )
    policy = EvidencePolicy(
        frozenset({DataCategory.WIND}), timedelta(hours=1), degraded_may_support_go=True
    )
    engine = RiskRulesEngine(
        [wind_rule], policy, "synthetic", required_factors=frozenset({RiskFactorKind.WIND})
    )
    note = AssessmentView.of(run_pipeline(None, risk_engine=engine)).outcome_note
    assert "Partly evaluated (aspects not evaluated): wind: gusts, direction." in note
