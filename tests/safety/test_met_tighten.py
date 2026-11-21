"""ADR 0031: MET's daily ranges may only tighten. Every value here is SYNTHETIC; MET items are built
by the real connector's `parse` from a synthetic payload, so their shape is the production one."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import NOW
from tests.safety.test_degraded_support_policy import _pinned_ruleset, _settings
from tests.safety.test_rule_definitions import EXPIRES, defn
from tests.unit.test_ruleset_loader import tioman

from divesafe.api.state import build_engine
from divesafe.config import Settings
from divesafe.data import SITES, TIGER_REEF, TIOMAN_ISLAND, MetApiMarineConnector
from divesafe.data import met_api_marine as met
from divesafe.domain import (
    SUPPORTED_METRICS,
    DataCategory,
    DataKind,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskFactorKind,
    RuleResult,
    severity,
)
from divesafe.risk import (
    MET_PAIRED_METRICS,
    MET_RANGE_RULE_PREFIX,
    MET_TIGHTEN_ZONES,
    NOT_EVALUATED_MARKER,
    DefinitionRule,
    EvidencePolicy,
    MetRangeTightenRule,
    RiskRulesEngine,
)
from divesafe.risk.engine import RESERVED_RULE_PREFIXES

pytestmark = pytest.mark.safety
R = Recommendation
C = DataCategory
H = timedelta(hours=1)
SITE = TIOMAN_ISLAND.id
# 10:00 at +08:00 on the day after NOW, for one hour: one MET day.
DAY = (NOW.astimezone(met.MALAYSIA_TIME) + timedelta(days=1)).date()
START = datetime.combine(DAY, time(10), tzinfo=met.MALAYSIA_TIME)
PLAN = DivePlan(site_id=SITE, planned_start=START, planned_duration_minutes=60, max_depth_m=10)
# Two days: 22:00 to 02:00 at +08:00.
LATE = datetime.combine(DAY, time(22), tzinfo=met.MALAYSIA_TIME)
TWO_DAY_PLAN = PLAN.model_copy(update={"planned_start": LATE, "planned_duration_minutes": 240})
QUIET = (0.1, 0.2)
WAVE_ID = "met-range.synthetic.wave_height"
WIND_ID = "met-range.synthetic.wind"


def _window(plan: DivePlan) -> tuple[datetime, datetime]:
    return plan.planned_start, plan.planned_start + timedelta(minutes=plan.planned_duration_minutes)


def _range(pair: tuple[float, float]) -> str:
    return f"{pair[0]:g}-{pair[1]:g}"


def met_items(
    site_id: str,
    start: datetime,
    end: datetime,
    *,
    wind: tuple[float, float] | Sequence[tuple[float, float]] = QUIET,
    wave: tuple[float, float] | Sequence[tuple[float, float]] = QUIET,
    now: datetime = NOW,
) -> list[EvidenceItem]:
    """MET items for every +08:00 day of the window, via the connector's own parser. A single
    pair is used for every day; a sequence gives one pair per day."""
    first, last = met._days(start, end)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]

    def per_day(value: Any) -> list[tuple[float, float]]:
        return list(value) if isinstance(value[0], tuple) else [value] * len(days)

    winds, waves = per_day(wind), per_day(wave)
    zone = met.WATERS_ZONES[site_id]
    results: list[dict[str, Any]] = []
    for day, w, h in zip(days, winds, waves, strict=True):
        stamp = datetime.combine(day, time.min, tzinfo=met.MALAYSIA_TIME).isoformat()
        for datatype, value, unit in (
            ("FMWS", _range(w), "km/h"),
            ("FMWH", _range(h), "m"),
            ("FMWD", "VRB", None),
        ):
            results.append(
                {
                    "locationid": zone[0],
                    "locationname": zone[1],
                    "date": stamp,
                    "datatype": datatype,
                    "value": value,
                    "attributes": {"unit": unit},
                }
            )
    connector = MetApiMarineConnector(getter=None)  # type: ignore[arg-type]
    return list(connector.parse({"results": results}, SITES[site_id], start, end, now).items)


def _of(items: Sequence[EvidenceItem], category: DataCategory) -> list[EvidenceItem]:
    return [e for e in items if e.category == category]


def _wave_rule(**changes: Any) -> MetRangeTightenRule:
    changes.setdefault("scope", {"site_ids": [SITE, TIGER_REEF.id, "site-a"], "max_depth_m": 30})
    return MetRangeTightenRule(DefinitionRule(defn(**changes)))


def _wind_rule(**changes: Any) -> MetRangeTightenRule:
    base: dict[str, Any] = {
        "id": "synthetic.wind",
        "factor": "wind",
        "metric": "wind_speed_10m",
        "unit": "km/h",
        "no_go_when": {"comparison": ">", "value": 30.0},
        "caution_when": {"comparison": ">", "value": 20.0},
        "go_when": {"comparison": "<=", "value": 20.0},
        "forecast_margin": 5.0,
    }
    return _wave_rule(**{**base, **changes})


def _wave(high: float, plan: DivePlan = PLAN, low: float | None = None) -> list[EvidenceItem]:
    pair = (min(high, low if low is not None else high), high)
    return _of(met_items(plan.site_id, *_window(plan), wave=pair), C.WAVES_SWELL)


def _set_range(item: EvidenceItem, name: str, **changes: Any) -> EvidenceItem:
    value = copy.deepcopy(item.value)
    value["ranges"][name].update(changes)
    return item.model_copy(update={"value": value})


# --- bands -----------------------------------------------------------------------------------
# The synthetic wave definition: NO-GO > 3.0 m, CAUTION > 2.0 m, margin 0.5 m.


@pytest.mark.parametrize(
    ("high", "expected"),
    [
        (2.51, R.NO_GO),  # 3.01 after the margin
        (2.5, R.CAUTION),  # 3.0: not > 3.0, but > 2.0
        (1.51, R.CAUTION),  # 2.01
        (1.5, None),  # 2.0: not > 2.0
        (0.2, None),
        (29.0, R.NO_GO),
    ],
)
def test_the_definition_bands_with_the_margin_decide(high: float, expected: R | None) -> None:
    items = _wave(high)
    result = _wave_rule().evaluate(PLAN, items)
    if expected is None:
        assert result is None
        return
    assert result is not None and result.outcome is expected
    assert result.rule_id == WAVE_ID
    assert result.evidence_ids == tuple(sorted(e.id for e in items))
    assert "synthetic.wave_height" in result.rationale and f"{high + 0.5:g} m" in result.rationale


@pytest.mark.parametrize(
    ("high", "expected"), [(25.01, R.NO_GO), (25.0, R.CAUTION), (15.01, R.CAUTION), (15.0, None)]
)
def test_wind_uses_mets_wind_speed_with_its_own_definition(high: float, expected: R | None) -> None:
    items = _of(met_items(SITE, *_window(PLAN), wind=(1.0, high)), C.WIND)
    result = _wind_rule().evaluate(PLAN, items)
    assert (result.outcome if result else None) is expected
    if result is not None:
        assert result.rule_id == WIND_ID
        aspects = ", ".join(SUPPORTED_METRICS["wind_speed_10m"].not_evaluated)
        assert result.rationale.endswith(f" {NOT_EVALUATED_MARKER} {aspects}.")


def test_the_high_end_is_used_not_the_low_end() -> None:
    result = _wave_rule().evaluate(PLAN, _wave(2.6, low=0.1))
    assert result is not None and result.outcome is R.NO_GO


def test_a_lower_is_worse_definition_uses_the_low_end_minus_the_margin() -> None:
    rule = _wave_rule(
        worse_when="lower",
        no_go_when={"comparison": "<", "value": 1.0},
        caution_when={"comparison": "<", "value": 2.0},
        go_when={"comparison": ">=", "value": 2.0},
    )
    assert rule.evaluate(PLAN, _wave(9.0, low=1.4)).outcome is R.NO_GO  # type: ignore[union-attr]
    assert rule.evaluate(PLAN, _wave(9.0, low=2.4)).outcome is R.CAUTION  # type: ignore[union-attr]
    assert rule.evaluate(PLAN, _wave(9.0, low=2.5)) is None


@pytest.mark.parametrize("high", [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 10.0, 29.99])
def test_never_go_and_never_covers_a_factor(high: float) -> None:
    result = _wave_rule().evaluate(PLAN, _wave(high))
    if result is None:
        return
    assert result.outcome in (R.NO_GO, R.CAUTION, R.INSUFFICIENT_EVIDENCE)
    assert result.factor is None and result.threshold_status is None


def test_a_definition_without_a_caution_band_gives_no_caution() -> None:
    rule = _wave_rule(caution_when=None)
    assert rule.evaluate(PLAN, _wave(2.0)) is None
    assert rule.evaluate(PLAN, _wave(2.6)).outcome is R.NO_GO  # type: ignore[union-attr]


# --- where it applies ------------------------------------------------------------------------


def test_the_approved_links_are_exactly_the_owner_approved_two_and_match_the_connector() -> None:
    """ADR 0031 item 5: one site per owner decision, through review."""
    assert dict(MET_TIGHTEN_ZONES) == {
        "my-pahang-pulau-tioman": "LOCATION:499",
        "my-pahang-tiger-reef": "LOCATION:499",
    }
    assert {s: z[0] for s, z in met.WATERS_ZONES.items()} == dict(MET_TIGHTEN_ZONES)
    assert dict(MET_PAIRED_METRICS) == {
        "wind_speed_10m": "wind_speed",
        "wave_height": "wave_height",
    }
    assert {name for _, name, _ in met._RANGES.values()} == set(MET_PAIRED_METRICS.values())
    with pytest.raises(TypeError):
        MET_TIGHTEN_ZONES["site-a"] = "LOCATION:499"  # type: ignore[index]


def test_tiger_reef_is_checked_too() -> None:
    plan = PLAN.model_copy(update={"site_id": TIGER_REEF.id})
    result = _wave_rule().evaluate(plan, _wave(2.6, plan))
    assert result is not None and result.outcome is R.NO_GO


def test_a_site_outside_the_approved_list_gets_no_check() -> None:
    """In the definition's scope, with MET items in hand: still no check."""
    plan = PLAN.model_copy(update={"site_id": "site-a"})
    rule = _wave_rule()
    assert rule._rule.applies(plan)  # noqa: SLF001 - control: the definition applies
    assert rule.evaluate(plan, _wave(2.6)) is None
    assert rule.evaluate(plan, []) is None


def test_no_check_where_the_definition_does_not_apply() -> None:
    deep = PLAN.model_copy(update={"max_depth_m": 31})
    assert _wave_rule().evaluate(deep, _wave(2.6)) is None
    other = _wave_rule(scope={"site_ids": [TIGER_REEF.id]})
    assert other.evaluate(PLAN, _wave(2.6)) is None


def test_only_paired_metrics_can_have_a_check() -> None:
    swell = DefinitionRule(
        defn(id="synthetic.swell", factor="swell", metric="swell_wave_height", unit="m")
    )
    with pytest.raises(ValueError, match="no MET range"):
        MetRangeTightenRule(swell)


# --- missing or unusable MET data: INSUFFICIENT EVIDENCE --------------------------------------


def _insufficient(result: RuleResult | None) -> None:
    assert result is not None and result.outcome is R.INSUFFICIENT_EVIDENCE
    assert result.factor is None and result.threshold_status is None
    assert result.rule_id == WAVE_ID


def test_no_met_item_is_insufficient() -> None:
    _insufficient(_wave_rule().evaluate(PLAN, []))


def test_an_item_from_another_source_does_not_count() -> None:
    fake = [e.model_copy(update={"source": "synthetic"}) for e in _wave(2.6)]
    _insufficient(_wave_rule().evaluate(PLAN, fake))


def test_items_of_the_other_category_do_not_count() -> None:
    wind_only = _of(met_items(SITE, *_window(PLAN), wind=(1.0, 99.0)), C.WIND)
    _insufficient(_wave_rule().evaluate(PLAN, wind_only))


def test_a_missing_day_of_a_two_day_window_is_insufficient() -> None:
    items = _of(met_items(SITE, *_window(TWO_DAY_PLAN), wave=[QUIET, QUIET]), C.WAVES_SWELL)
    assert len(items) == 2 and _wave_rule().evaluate(TWO_DAY_PLAN, items) is None  # control
    for kept in (items[:1], items[1:]):
        _insufficient(_wave_rule().evaluate(TWO_DAY_PLAN, kept))


def test_an_item_for_another_day_does_not_cover_the_window() -> None:
    later = PLAN.model_copy(update={"planned_start": START + timedelta(days=1)})
    _insufficient(_wave_rule().evaluate(later, _wave(0.2)))


@pytest.mark.parametrize(
    "changes",
    [
        {"unit": "ft"},
        {"unit": None},
        {"high": "2.6"},
        {"high": True},
        {"high": None},
        {"high": float("nan")},
        {"high": float("inf")},
        {"low": float("-inf")},
        {"low": "0.1"},
        {"high": 10**400},  # an int too large for a float
        {"low": 9.0},  # reversed: low above high
        {"low": -0.1},  # negative: impossible for wind speed or wave height
        {"low": -3.0, "high": -1.0},
    ],
    ids=repr,
)
def test_an_unusable_range_is_insufficient_never_skipped(changes: dict[str, Any]) -> None:
    """Even with a second, usable item for the same day: a bad item is never skipped."""
    good = _wave(0.2)
    bad = [_set_range(good[0], "wave_height", **changes).model_copy(update={"id": "bad"})]
    _insufficient(_wave_rule().evaluate(PLAN, bad))
    _insufficient(_wave_rule().evaluate(PLAN, [*good, *bad]))


@pytest.mark.parametrize(
    "value",
    [
        {"date": "x", "location_id": "LOCATION:499"},
        {"ranges": "2.6", "location_id": "LOCATION:499"},
        {"ranges": {"wave_height": "2.6"}, "location_id": "LOCATION:499"},
        {"ranges": {"swell": {"low": 0, "high": 1, "unit": "m"}}, "location_id": "LOCATION:499"},
    ],
    ids=["no_ranges", "ranges_not_a_mapping", "range_not_a_mapping", "other_range_name"],
)
def test_a_malformed_item_is_insufficient(value: dict[str, Any]) -> None:
    bad = [_wave(0.2)[0].model_copy(update={"value": value})]
    _insufficient(_wave_rule().evaluate(PLAN, bad))


@pytest.mark.parametrize("zone", ["LOCATION:500", None, ""])
def test_an_item_for_another_zone_is_insufficient(zone: str | None) -> None:
    item = _wave(2.6)[0]
    other = item.model_copy(update={"value": {**item.value, "location_id": zone}})
    _insufficient(_wave_rule().evaluate(PLAN, [other]))


def test_an_instant_met_item_is_insufficient() -> None:
    item = _wave(0.2)[0]
    instant = item.model_copy(update={"valid_at": START, "valid_until": None, "id": "instant"})
    _insufficient(_wave_rule().evaluate(PLAN, [item, instant]))


# --- the window ------------------------------------------------------------------------------


def test_a_multi_day_window_uses_the_worst_day() -> None:
    for days in ([QUIET, (1.0, 2.6)], [(1.0, 2.6), QUIET]):
        items = _of(met_items(SITE, *_window(TWO_DAY_PLAN), wave=days), C.WAVES_SWELL)
        result = _wave_rule().evaluate(TWO_DAY_PLAN, items)
        assert result is not None and result.outcome is R.NO_GO
        assert result.evidence_ids == tuple(sorted(e.id for e in items))


def test_a_day_outside_the_window_is_not_used() -> None:
    today = _wave(0.2)
    tomorrow = _of(
        met_items(SITE, *_window(PLAN.model_copy(update={"planned_start": START + 24 * H}))),
        C.WAVES_SWELL,
    )
    rough = [_set_range(tomorrow[0], "wave_height", high=9.0)]
    assert _wave_rule().evaluate(PLAN, [*today, *rough]) is None


def test_a_dive_ending_at_midnight_does_not_need_the_next_day() -> None:
    midnight = datetime.combine(DAY + timedelta(days=1), time.min, tzinfo=met.MALAYSIA_TIME)
    plan = PLAN.model_copy(update={"planned_start": midnight - H})
    items = _of(met_items(SITE, *_window(plan)), C.WAVES_SWELL)
    assert len(items) == 1 and items[0].valid_until == midnight
    assert _wave_rule().evaluate(plan, items) is None
    # a rough next day is not part of this dive
    next_plan = plan.model_copy(update={"planned_start": midnight})
    rough = _of(met_items(SITE, *_window(next_plan), wave=(1.0, 2.6)), C.WAVES_SWELL)
    assert _wave_rule().evaluate(plan, [*items, *rough]) is None
    # nor is a rough previous day for a dive starting at midnight
    result = _wave_rule().evaluate(next_plan, [*items, *rough])
    assert result is not None and result.outcome is R.NO_GO
    calm_next = _of(met_items(SITE, *_window(next_plan)), C.WAVES_SWELL)
    rough_today = [_set_range(items[0], "wave_height", high=9.0)]
    assert _wave_rule().evaluate(next_plan, [*rough_today, *calm_next]) is None


def test_a_dive_crossing_midnight_needs_both_days() -> None:
    midnight = datetime.combine(DAY + timedelta(days=1), time.min, tzinfo=met.MALAYSIA_TIME)
    plan = PLAN.model_copy(update={"planned_start": midnight - H, "planned_duration_minutes": 61})
    items = _of(met_items(SITE, *_window(plan)), C.WAVES_SWELL)
    assert len(items) == 2
    _insufficient(_wave_rule().evaluate(plan, items[:1]))


# --- in the engine ---------------------------------------------------------------------------


class _Fixed:
    rule_id = "synthetic.fixed"
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self, outcome: Recommendation) -> None:
        self._outcome = outcome

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        return RuleResult(
            rule_id=self.rule_id,
            outcome=self._outcome,
            rationale="synthetic",
            evidence_ids=tuple(e.id for e in evidence),
        )


def _engine(*rules: Any, degraded: bool, all_factors: bool = False) -> RiskRulesEngine:
    policy = EvidencePolicy(frozenset(), timedelta(hours=1), degraded_may_support_go=degraded)
    if all_factors:  # the default required factors, as in production
        return RiskRulesEngine(list(rules), policy, "synthetic")
    return RiskRulesEngine(list(rules), policy, "synthetic", required_factors=frozenset())


@pytest.mark.parametrize("all_factors", [False, True], ids=["no_factors", "default_factors"])
@pytest.mark.parametrize("degraded", [False, True])
@pytest.mark.parametrize("outcome", list(Recommendation), ids=lambda r: r.name)
@pytest.mark.parametrize("high", [None, 0.2, 1.51, 2.6, float("nan")], ids=repr)
def test_the_check_never_lowers_a_result(
    outcome: R, high: float | None, degraded: bool, all_factors: bool
) -> None:
    if high is None:
        items: list[EvidenceItem] = []
    elif high != high:  # NaN: an unusable range
        items = [_set_range(_wave(0.2)[0], "wave_height", high=high)]
    else:
        items = _wave(high)
    flags = {"degraded": degraded, "all_factors": all_factors}
    without = _engine(_Fixed(outcome), **flags).assess(PLAN, items, NOW)
    with_met = _engine(_Fixed(outcome), _wave_rule(), **flags).assess(PLAN, items, NOW)
    assert severity(with_met.recommendation) >= severity(without.recommendation)
    assert with_met.recommendation is not R.GO or without.recommendation is R.GO
    assert with_met.unevaluated_factors == without.unevaluated_factors


def test_the_check_never_covers_a_factor_in_the_engine() -> None:
    policy = EvidencePolicy(frozenset(), timedelta(hours=1))
    engine = RiskRulesEngine([_wave_rule()], policy, "synthetic")
    result = engine.assess(PLAN, _wave(2.6), NOW)
    assert result.recommendation is R.NO_GO
    assert RiskFactorKind.WAVE_HEIGHT in result.unevaluated_factors


def test_the_rule_id_is_not_reserved_and_the_engine_accepts_it() -> None:
    assert not MET_RANGE_RULE_PREFIX.lower().startswith(RESERVED_RULE_PREFIXES)
    assert _wave_rule().rule_id.startswith(MET_RANGE_RULE_PREFIX)
    _engine(_wave_rule(), degraded=False)


def test_the_check_expires_with_its_definition() -> None:
    rule = _wave_rule()
    assert rule.expires_at == EXPIRES
    result = _engine(rule, degraded=False).assess(PLAN, _wave(0.2), EXPIRES)
    expired = [r for r in result.rule_results if r.rule_id == f"rule.expired.{WAVE_ID}"]
    assert [r.outcome for r in expired] == [R.INSUFFICIENT_EVIDENCE]


# --- build_engine ----------------------------------------------------------------------------


def _ruleset(tmp_path: Path, *definitions: dict[str, Any]) -> tuple[Path, str]:
    return _pinned_ruleset(tmp_path, *definitions)


def _all_metrics() -> list[dict[str, Any]]:
    return [
        tioman(),
        tioman(
            id="synthetic.wind",
            factor="wind",
            metric="wind_speed_10m",
            unit="km/h",
            no_go_when={"comparison": ">", "value": 30.0},
            caution_when={"comparison": ">", "value": 20.0},
            go_when={"comparison": "<=", "value": 20.0},
        ),
        tioman(id="synthetic.wave_deep", scope={"site_ids": [SITE], "max_depth_m": 40}),
        tioman(id="synthetic.swell", factor="swell", metric="swell_wave_height", unit="m"),
        tioman(
            id="synthetic.current",
            factor="current",
            metric="ocean_current_velocity",
            unit="km/h",
            no_go_when={"comparison": ">", "value": 3.0},
            caution_when={"comparison": ">", "value": 2.0},
            go_when={"comparison": "<=", "value": 2.0},
        ),
    ]


@pytest.mark.parametrize("on", [False, True], ids=["flag_off", "flag_on"])
def test_build_engine_adds_exactly_one_check_per_paired_definition(
    on: bool, tmp_path: Path
) -> None:
    engine = build_engine(_settings(*_ruleset(tmp_path, *_all_metrics()), on=on), now=NOW)
    assert engine is not None
    ids = [r.rule_id for r in engine._rules]  # noqa: SLF001
    met_ids = [i for i in ids if i.startswith(MET_RANGE_RULE_PREFIX)]
    assert met_ids == [
        "met-range.synthetic.wave_height",
        "met-range.synthetic.wind",
        "met-range.synthetic.wave_deep",
    ]
    checks = [r for r in engine._rules if isinstance(r, MetRangeTightenRule)]  # noqa: SLF001
    assert [r.rule_id for r in checks] == met_ids
    # each check sits beside its own definition rule, which always gives a result where the
    # check applies, so the check can only add severity to it
    for met_id in met_ids:
        assert "definition." + met_id.removeprefix(MET_RANGE_RULE_PREFIX) in ids


def test_build_engine_adds_no_check_without_a_ruleset() -> None:
    engine = build_engine(Settings(_env_file=None, evidence_max_age_minutes=60), now=NOW)
    assert engine is not None
    assert not any(isinstance(r, MetRangeTightenRule) for r in engine._rules)  # noqa: SLF001


@pytest.mark.parametrize("on", [False, True], ids=["flag_off", "flag_on"])
@pytest.mark.parametrize("site", [TIOMAN_ISLAND.id, TIGER_REEF.id])
def test_production_wiring_turns_a_rough_met_day_into_no_go(
    on: bool, site: str, tmp_path: Path
) -> None:
    """The hourly data is calm (its definition gives GO); only MET's range is in the NO-GO
    band, and the tighter of the two decides."""
    definition = tioman(scope={"site_ids": [TIOMAN_ISLAND.id, TIGER_REEF.id], "max_depth_m": 30})
    engine = build_engine(_settings(*_ruleset(tmp_path, definition), on=on), now=NOW)
    assert engine is not None
    plan = PLAN.model_copy(update={"site_id": site})
    hourly = _hourly_wave(plan, 0.2)
    calm = [hourly, *met_items(site, *_window(plan), wave=(0.1, 0.2))]
    rough = [hourly, *met_items(site, *_window(plan), wave=(1.0, 2.6))]
    calm_result = engine.assess(plan, calm, NOW)
    rough_result = engine.assess(plan, rough, NOW)
    definition_id = "definition.synthetic.wave_height"
    for result in (calm_result, rough_result):  # control: the hourly definition says GO
        outcomes = {r.rule_id: r.outcome for r in result.rule_results}
        assert outcomes[definition_id] is R.GO
    assert WAVE_ID not in {r.rule_id for r in calm_result.rule_results}  # control
    met_results = [r for r in rough_result.rule_results if r.rule_id == WAVE_ID]
    assert [r.outcome for r in met_results] == [R.NO_GO]
    assert calm_result.recommendation is not R.NO_GO  # control
    assert rough_result.recommendation is R.NO_GO


def _hourly_wave(plan: DivePlan, height: float) -> EvidenceItem:
    """A SYNTHETIC hourly wave forecast covering the dive window (not a MET item)."""
    start, end = _window(plan)
    return EvidenceItem(
        id="synthetic-hourly-wave",
        category=C.WAVES_SWELL,
        source="synthetic",
        retrieved_at=NOW,
        valid_at=start,
        valid_until=end,
        is_forecast=True,
        data_kind=DataKind.FORECAST,
        value={"variables": {"wave_height": height}, "units": {"wave_height": "m"}},
    )


@pytest.mark.parametrize(("high", "expected"), [(2.6, R.NO_GO), (0.2, None)])
def test_a_stale_met_item_still_tightens_and_a_stale_calm_one_adds_nothing(
    high: float, expected: R | None
) -> None:
    """ADR 0031 Known gaps: the check does not see the item's age. Pinned so that a change to
    how age is handled is a visible decision."""
    old = NOW - timedelta(days=2)
    items = _of(met_items(SITE, *_window(PLAN), wave=(0.1, high), now=old), C.WAVES_SWELL)
    assert items and all(e.retrieved_at == old for e in items)  # older than max_age (1 h)
    result = _engine(_wave_rule(), degraded=False).assess(PLAN, items, NOW)
    met_results = [r.outcome for r in result.rule_results if r.rule_id == WAVE_ID]
    assert met_results == ([] if expected is None else [expected])
    if expected is not None:
        assert result.recommendation is expected


def test_the_check_has_no_date_shortcut() -> None:
    """It reads validity windows, never the `date` text, so a wrong date string changes nothing."""
    item = _wave(2.6)[0]
    relabelled = item.model_copy(update={"value": {**item.value, "date": date.min.isoformat()}})
    assert _wave_rule().evaluate(PLAN, [relabelled]).outcome is R.NO_GO  # type: ignore[union-attr]
