"""Signed-off rule definitions (ADR 0009). Every number here is SYNTHETIC test input that
exercises the mechanism; none is a safety limit and none is used outside tests."""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import NOW

from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskFactorKind,
    RuleDefinition,
    ThresholdStatus,
)
from divesafe.risk import DefinitionRule, EvidencePolicy, RiskRulesEngine, reconcile

pytestmark = pytest.mark.safety
R = Recommendation
K = RiskFactorKind
H = timedelta(hours=1)
START = NOW - 3 * H
END = START + H
PLAN = DivePlan(site_id="site-a", planned_start=START, planned_duration_minutes=60, max_depth_m=20)
SIGNED = NOW - 30 * H
EXPIRES = NOW + 100 * H


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def person(name: str) -> dict[str, str]:
    return {"name": name, "role": "synthetic role", "qualification": "synthetic qualification"}


def raw(**changes: Any) -> dict[str, Any]:
    """A complete, VALIDATED, synthetic definition for a HIGHER-is-worse metric."""
    base: dict[str, Any] = {
        "id": "synthetic.wave_height",
        "factor": "wave_height",
        "metric": "wave_height",
        "worse_when": "higher",
        "unit": "m",
        "no_go_when": {"comparison": ">", "value": 3.0},
        "caution_when": {"comparison": ">", "value": 2.0},
        "go_when": {"comparison": "<=", "value": 2.0},
        "forecast_margin": 0.5,
        "scope": {"site_ids": ["site-a"], "max_depth_m": 30},
        "source": {
            "document": "SYNTHETIC test document",
            "version_or_date": "v0",
            "location_in_document": "s0",
            "publicly_available": False,
        },
        "document_version": "synthetic-v0",
        "status": "VALIDATED",
        "prepared_by": person("Preparer One"),
        "reviews": [{"reviewer": person("Reviewer Two"), "reviewed_on": _iso(SIGNED)}],
        "readback_confirmed_by": person("Preparer One"),
        "readback_confirmed_on": _iso(SIGNED),
        "signed_off_on": _iso(SIGNED),
        "expires_on": _iso(EXPIRES),
    }
    base.update(changes)
    return base


def defn(**changes: Any) -> RuleDefinition:
    return RuleDefinition.model_validate(raw(**changes))


def evidence(
    values: dict[str, float],
    *,
    variable: str = "wave_height",
    unit: str = "m",
    category: DataCategory = DataCategory.WAVES_SWELL,
    retrieved_at: datetime = NOW - timedelta(minutes=5),
) -> list[EvidenceItem]:
    """One model item per hour in the dive window, one value per hour."""
    items = []
    for i, value in enumerate(values.values()):
        at = START + i * H
        items.append(
            EvidenceItem(
                id=f"e-{i}",
                category=category,
                source="synthetic",
                retrieved_at=retrieved_at,
                valid_at=at,
                is_forecast=True,
                data_kind=DataKind.MODEL,
                quality=DataQuality.DEGRADED,
                value={"variables": {variable: value}, "units": {variable: unit}},
            )
        )
    return items


def outcome(definition: RuleDefinition, series: list[float], plan: DivePlan = PLAN) -> R:
    items = evidence({f"h{i}": v for i, v in enumerate(series)})
    result = DefinitionRule(definition).evaluate(plan, items)
    assert result is not None
    return result.outcome


# --- the model refuses inconsistent definitions ---------------------------------------------


def test_a_complete_definition_is_accepted() -> None:
    assert defn().status == ThresholdStatus.VALIDATED


@pytest.mark.parametrize(
    "changes",
    [
        {"no_go_when": {"comparison": ">", "value": 1.0}},  # NO-GO less severe than CAUTION
        {"caution_when": {"comparison": ">", "value": 3.5}},  # CAUTION beyond NO-GO
        {"go_when": {"comparison": "<=", "value": 2.5}},  # GO range beyond CAUTION
        {"no_go_when": {"comparison": "<", "value": 3.0}},  # contradicts worse_when=higher
        {"go_when": {"comparison": ">", "value": 1.0}},
    ],
)
def test_swapped_or_contradictory_bands_cannot_be_built(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        defn(**changes)


def test_the_lower_is_worse_direction_mirrors_the_ordering() -> None:
    mirrored = {
        "worse_when": "lower",
        "no_go_when": {"comparison": "<", "value": 1.0},
        "caution_when": {"comparison": "<", "value": 2.0},
        "go_when": {"comparison": ">=", "value": 2.0},
    }
    assert defn(**mirrored).worse_when.value == "lower"
    with pytest.raises(ValidationError):
        defn(**{**mirrored, "no_go_when": {"comparison": "<", "value": 3.0}})


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"unit": "ft"}, "unit differs from the data and no conversion is done"),
        ({"unit": "km/h"}, "unit of a different quantity"),
        ({"factor": "wind", "metric": "wave_height"}, "no data source for wind"),
        ({"factor": "tidal_current", "metric": "ocean_current_velocity"}, "no tide source"),
        ({"factor": "weather", "metric": "wave_height"}, "no weather data"),
        ({"factor": "swell", "metric": "wave_height"}, "metric not for this factor"),
        ({"metric": "wave_direction"}, "direction is not a supported metric"),
        ({"factor": "data_freshness"}, "not a threshold factor"),
        ({"no_go_when": None, "caution_when": None, "go_when": None}, "no limit at all"),
        ({"forecast_margin": -0.1}, "a negative margin would relax the limit"),
        ({"forecast_margin": math.inf}, "non-finite margin"),
        ({"no_go_when": {"comparison": ">", "value": math.nan}}, "NaN limit"),
        ({"scope": {"site_ids": []}}, "no wildcard: a site must be named"),
        ({"scope": {"site_ids": ["x"], "max_depth_m": 0}}, "non-positive depth"),
        ({"unexpected": 1}, "unknown field"),
        ({"id": "has space"}, "bad id"),
    ],
)
def test_definitions_the_software_cannot_apply_faithfully_are_refused(
    changes: dict[str, Any], why: str
) -> None:
    with pytest.raises(ValidationError):
        defn(**changes)


def test_the_error_for_an_unsupported_factor_says_there_is_no_data_source() -> None:
    with pytest.raises(ValidationError, match="no data source"):
        defn(factor="wind")


# --- VALIDATED needs the whole sign-off ----------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"unrepresented_conditions": ["diver qualification"]},
        {"reviews": []},  # GO/CAUTION needs an independent reviewer
        {"reviews": [{"reviewer": person("preparer one"), "reviewed_on": _iso(SIGNED)}]},
        {
            "reviews": [
                {"reviewer": person("Reviewer Two"), "reviewed_on": _iso(SIGNED)},
                {"reviewer": person("reviewer two"), "reviewed_on": _iso(SIGNED)},
            ]
        },
        {"readback_confirmed_by": None},
        {"readback_confirmed_on": None},
        {"signed_off_on": None},
        {"expires_on": None},
        {"expires_on": _iso(SIGNED)},
        {"expires_on": _iso(SIGNED - H)},
    ],
)
def test_an_incomplete_signoff_cannot_be_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="cannot be VALIDATED"):
        defn(**changes)


def test_a_no_go_only_limit_needs_a_reviewer_or_a_recorded_reason() -> None:
    no_go_only: dict[str, Any] = {"caution_when": None, "go_when": None, "reviews": []}
    with pytest.raises(ValidationError, match="no independent reviewer"):
        defn(**no_go_only)
    assert defn(**no_go_only, no_second_reviewer_reason="synthetic: no peer available")
    with pytest.raises(ValidationError):
        defn(**no_go_only, no_second_reviewer_reason="   ")


def test_an_unvalidated_draft_can_be_incomplete() -> None:
    draft = defn(
        status="REQUIRES DOMAIN VALIDATION",
        reviews=[],
        readback_confirmed_by=None,
        readback_confirmed_on=None,
        signed_off_on=None,
        expires_on=None,
        unrepresented_conditions=["season"],
    )
    assert draft.status == ThresholdStatus.REQUIRES_DOMAIN_VALIDATION
    with pytest.raises(ValueError, match="VALIDATED"):
        DefinitionRule(draft)


# --- applying a definition -----------------------------------------------------------------


def test_each_band_is_reached_and_outside_every_band_is_not_a_recommendation() -> None:
    assert outcome(defn(forecast_margin=0.0), [3.5]) == R.NO_GO
    assert outcome(defn(forecast_margin=0.0), [2.5]) == R.CAUTION
    assert outcome(defn(forecast_margin=0.0), [1.5]) == R.GO
    gap = defn(
        forecast_margin=0.0,
        caution_when={"comparison": ">", "value": 2.5},
        go_when={"comparison": "<=", "value": 1.0},
    )
    assert outcome(gap, [1.5]) == R.INSUFFICIENT_EVIDENCE  # between the signed-off ranges


def test_a_go_needs_an_explicit_signed_off_go_range() -> None:
    no_go_only = defn(
        caution_when=None, go_when=None, no_second_reviewer_reason="synthetic", reviews=[]
    )
    assert outcome(no_go_only, [0.1]) == R.INSUFFICIENT_EVIDENCE
    assert outcome(no_go_only, [9.0]) == R.NO_GO


def test_the_worst_value_in_the_window_decides() -> None:
    assert outcome(defn(forecast_margin=0.0), [0.5, 3.5]) == R.NO_GO
    assert outcome(defn(forecast_margin=0.0), [3.5, 0.5]) == R.NO_GO
    lower = defn(
        worse_when="lower",
        forecast_margin=0.0,
        no_go_when={"comparison": "<", "value": 1.0},
        caution_when={"comparison": "<", "value": 2.0},
        go_when={"comparison": ">=", "value": 2.0},
    )
    assert outcome(lower, [5.0, 0.5]) == R.NO_GO  # the minimum is the worst when lower is worse


def test_the_forecast_margin_makes_the_rule_more_cautious_and_never_less() -> None:
    assert outcome(defn(forecast_margin=0.0), [1.9]) == R.GO
    assert outcome(defn(forecast_margin=0.5), [1.9]) == R.CAUTION  # 1.9 + 0.5 > 2.0
    assert outcome(defn(forecast_margin=0.5), [2.6]) == R.NO_GO  # 2.6 + 0.5 > 3.0


def test_the_margin_acts_in_the_dangerous_direction_when_lower_is_worse() -> None:
    lower = {
        "worse_when": "lower",
        "no_go_when": {"comparison": "<", "value": 1.0},
        "caution_when": {"comparison": "<", "value": 2.0},
        "go_when": {"comparison": ">=", "value": 2.0},
    }
    assert outcome(defn(**lower, forecast_margin=0.0), [2.1]) == R.GO
    assert outcome(defn(**lower, forecast_margin=0.5), [2.1]) == R.CAUTION  # 2.1 - 0.5 < 2.0


@pytest.mark.parametrize(
    ("comparison", "value", "expected"),
    [
        (">", 3.0, R.GO),  # equal is NOT greater
        (">=", 3.0, R.NO_GO),  # equal IS greater or equal
    ],
)
def test_the_signed_off_comparison_is_applied_exactly_at_the_boundary(
    comparison: str, value: float, expected: R
) -> None:
    d = defn(
        forecast_margin=0.0,
        no_go_when={"comparison": comparison, "value": value},
        caution_when=None,
        go_when={"comparison": "<=", "value": 3.0},
    )
    assert outcome(d, [3.0]) == expected


def test_a_plan_outside_the_scope_is_not_evaluated_by_the_definition() -> None:
    rule = DefinitionRule(defn())
    items = evidence({"a": 9.0, "b": 9.0})
    assert rule.evaluate(PLAN.model_copy(update={"site_id": "site-b"}), items) is None
    assert rule.evaluate(PLAN.model_copy(update={"max_depth_m": 31}), items) is None
    assert rule.evaluate(PLAN.model_copy(update={"max_depth_m": 30}), items).outcome == R.NO_GO  # type: ignore[union-attr]


def test_a_site_outside_the_scope_leaves_the_factor_unevaluated_in_the_engine() -> None:
    engine = _engine(defn(forecast_margin=0.0), factors=frozenset({K.WAVE_HEIGHT}))
    other_site = PLAN.model_copy(update={"site_id": "site-b"})
    result = engine.assess(other_site, evidence({"a": 0.1, "b": 0.1}), NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert K.WAVE_HEIGHT in result.unevaluated_factors


def test_missing_evidence_is_insufficient_evidence() -> None:
    result = DefinitionRule(defn()).evaluate(PLAN, [])
    assert result is not None and result.outcome == R.INSUFFICIENT_EVIDENCE


def test_the_nearest_samples_on_each_side_of_the_window_are_included() -> None:
    """A dive between two hourly samples is exposed to both, so a high bracketing sample counts."""
    rule = DefinitionRule(defn(forecast_margin=0.0))
    window_plan = PLAN.model_copy(
        update={"planned_start": START + timedelta(minutes=10), "planned_duration_minutes": 30}
    )

    def at(offset: timedelta, value: float, ident: str) -> EvidenceItem:
        return evidence({"x": value})[0].model_copy(
            update={"id": ident, "valid_at": START + offset}
        )

    low_inside = at(timedelta(minutes=20), 0.5, "inside")
    high_before = at(timedelta(0), 3.5, "before")
    high_after = at(timedelta(minutes=50), 3.5, "after")
    low_far = at(timedelta(hours=-5), 0.5, "far-before")
    assert rule.evaluate(window_plan, [low_inside]).outcome == R.GO  # type: ignore[union-attr]
    for bracket in (high_before, high_after):
        result = rule.evaluate(window_plan, [low_inside, bracket])
        assert (
            result is not None and result.outcome == R.NO_GO and bracket.id in result.evidence_ids
        )
    # only the NEAREST earlier sample is used: an older, calmer one does not dilute a worse one
    assert rule.evaluate(window_plan, [low_inside, low_far, high_before]).outcome == R.NO_GO  # type: ignore[union-attr]
    assert (
        rule.evaluate(
            window_plan, [low_inside, high_before, at(timedelta(minutes=5), 0.4, "nearer")]
        ).outcome
        == R.GO
    )  # type: ignore[union-attr]


def test_an_interval_item_overlapping_the_window_is_used_without_extra_samples() -> None:
    span = evidence({"x": 3.5})[0].model_copy(
        update={"id": "span", "valid_at": START - H, "valid_until": END + H}
    )
    result = DefinitionRule(defn(forecast_margin=0.0)).evaluate(PLAN, [span])
    assert result is not None and result.outcome == R.NO_GO


@pytest.mark.parametrize("bad", [True, "3.5", None, math.nan, math.inf])
def test_unusable_values_are_insufficient_evidence_never_a_guess(bad: Any) -> None:
    items = evidence({"h": 1.0})
    items[0] = items[0].model_copy(
        update={"value": {"variables": {"wave_height": bad}, "units": {"wave_height": "m"}}}
    )
    result = DefinitionRule(defn()).evaluate(PLAN, items)
    assert result is not None and result.outcome == R.INSUFFICIENT_EVIDENCE


def test_a_unit_that_differs_from_the_definition_is_insufficient_evidence() -> None:
    items = evidence({"h": 0.1}, unit="ft")
    result = DefinitionRule(defn()).evaluate(PLAN, items)
    assert result is not None and result.outcome == R.INSUFFICIENT_EVIDENCE
    assert "unit" in result.rationale


def test_results_cite_the_evidence_the_factor_and_the_validated_status() -> None:
    items = evidence({"a": 1.0, "b": 1.5})
    result = DefinitionRule(defn(forecast_margin=0.0)).evaluate(PLAN, items)
    assert result is not None
    assert result.evidence_ids == ("e-0", "e-1")
    assert result.factor == K.WAVE_HEIGHT and result.threshold_status == ThresholdStatus.VALIDATED
    assert "synthetic.wave_height" in result.rationale and "wave_height" in result.rationale


def test_the_rule_carries_the_source_as_its_citation() -> None:
    rule = DefinitionRule(defn())
    assert "SYNTHETIC test document" in rule.citation and "synthetic-v0" in rule.citation


# --- in the engine ---------------------------------------------------------------------------


def _engine(*definitions: RuleDefinition, factors: frozenset[K] | None = None) -> RiskRulesEngine:
    rules = [DefinitionRule(d) for d in definitions]
    policy = EvidencePolicy(frozenset({DataCategory.WAVES_SWELL}), H, degraded_may_support_go=True)
    kwargs: dict[str, Any] = {} if factors is None else {"required_factors": factors}
    return RiskRulesEngine(rules, policy, "synthetic", **kwargs)


def test_one_validated_rule_cannot_give_go_under_the_default_coverage_guard() -> None:
    items = evidence({"a": 0.1, "b": 0.1})
    default = _engine(defn(forecast_margin=0.0))
    assert default.assess(PLAN, items, NOW).recommendation == R.INSUFFICIENT_EVIDENCE
    only_this = _engine(defn(forecast_margin=0.0), factors=frozenset({K.WAVE_HEIGHT}))
    assert only_this.assess(PLAN, items, NOW).recommendation == R.GO  # synthetic scope only


def test_a_definition_no_go_cannot_be_relaxed_by_an_llm_proposal() -> None:
    items = evidence({"a": 9.0, "b": 9.0})
    engine = _engine(defn(forecast_margin=0.0), factors=frozenset({K.WAVE_HEIGHT}))
    deterministic = engine.assess(PLAN, items, NOW).recommendation
    assert deterministic == R.NO_GO
    assert reconcile(deterministic, R.GO).final == R.NO_GO


def test_an_expired_rule_is_not_applied_and_the_result_says_why() -> None:
    engine = _engine(defn(forecast_margin=0.0), factors=frozenset({K.WAVE_HEIGHT}))

    def fresh(at: datetime) -> list[EvidenceItem]:
        return evidence({"a": 0.1, "b": 0.1}, retrieved_at=at - timedelta(minutes=5))

    just_before = EXPIRES - timedelta(seconds=1)
    before = engine.assess(PLAN, fresh(just_before), just_before)
    at = engine.assess(PLAN, fresh(EXPIRES), EXPIRES)
    assert before.recommendation == R.GO
    assert at.recommendation == R.INSUFFICIENT_EVIDENCE  # exactly at expiry is expired
    assert any(r.rule_id.startswith("rule.expired.") for r in at.rule_results)
    assert K.WAVE_HEIGHT in at.unevaluated_factors  # the factor is no longer covered


def test_an_expired_no_go_rule_does_not_silently_become_a_go() -> None:
    engine = _engine(defn(forecast_margin=0.0), factors=frozenset({K.WAVE_HEIGHT}))
    later = EXPIRES + H
    items = evidence({"a": 9.0, "b": 9.0}, retrieved_at=later - timedelta(minutes=5))
    assert engine.assess(PLAN, items, later).recommendation == R.INSUFFICIENT_EVIDENCE


def test_the_real_recorded_marine_data_flows_through_a_synthetic_definition() -> None:
    from tests.agent.scripted_llm import engine as synthetic_engine  # noqa: F401
    from tests.agent.scripted_llm import run_pipeline

    record = run_pipeline(None, with_warning_rule=False)
    live_plan = record.plan
    rule = DefinitionRule(
        defn(
            scope={"site_ids": [live_plan.site_id]},
            forecast_margin=0.0,
            no_go_when={"comparison": ">", "value": 9.0},
            caution_when={"comparison": ">", "value": 5.0},
            go_when={"comparison": "<=", "value": 5.0},
        )
    )
    result = rule.evaluate(live_plan, record.evidence)
    assert result is not None and result.outcome == R.GO
    assert result.evidence_ids and all(
        i.startswith("open-meteo-marine:") for i in result.evidence_ids
    )


def test_definitions_are_independent_copies() -> None:
    first = raw()
    snapshot = copy.deepcopy(first)
    RuleDefinition.model_validate(first)
    assert first == snapshot


# --- review follow-ups: sign-off integrity, bounds, ties, expiry contract ------------------------


def test_the_readback_must_be_confirmed_by_the_preparer_or_a_reviewer() -> None:
    assert defn(readback_confirmed_by=person("Reviewer Two"))  # a reviewer may confirm
    with pytest.raises(ValidationError, match="read-back was confirmed by someone"):
        defn(readback_confirmed_by=person("A Developer"))


def test_a_review_or_readback_dated_after_the_signoff_is_refused() -> None:
    late = _iso(SIGNED + H)
    with pytest.raises(ValidationError, match="dated after the sign-off"):
        defn(readback_confirmed_on=late)
    with pytest.raises(ValidationError, match="dated after the sign-off"):
        defn(reviews=[{"reviewer": person("Reviewer Two"), "reviewed_on": late}])


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": {"site_ids": ["a"] * 51}},
        {"scope": {"site_ids": ["bad id"]}},
        {"metric": "x" * 101},
        {"unit": "m" * 201},
        {"limitations": ["x"] * 51},
        {"unrepresented_conditions": ["x"] * 51, "status": "REQUIRES DOMAIN VALIDATION"},
        {"no_second_reviewer_reason": "x" * 201},
    ],
)
def test_unbounded_fields_are_bounded(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        defn(**changes)


def test_secondary_wave_height_is_not_an_encodable_metric_for_wave_height() -> None:
    with pytest.raises(ValidationError, match="no data source"):
        defn(metric="wind_wave_height")


@pytest.mark.parametrize(
    ("caution", "go", "at_the_shared_value"),
    [
        (
            {"comparison": ">=", "value": 2.0},
            {"comparison": "<=", "value": 2.0},
            R.CAUTION,
        ),  # overlap: severe wins
        (
            {"comparison": ">", "value": 2.0},
            {"comparison": "<", "value": 2.0},
            R.INSUFFICIENT_EVIDENCE,
        ),  # hole
        (
            {"comparison": ">", "value": 2.0},
            {"comparison": "<=", "value": 2.0},
            R.GO,
        ),  # exact partition
        (
            {"comparison": ">=", "value": 2.0},
            {"comparison": "<", "value": 2.0},
            R.CAUTION,
        ),  # exact partition
    ],
)
def test_ties_at_a_shared_value_resolve_to_the_more_severe_outcome_or_to_no_recommendation(
    caution: dict[str, Any], go: dict[str, Any], at_the_shared_value: R
) -> None:
    d = defn(forecast_margin=0.0, caution_when=caution, go_when=go)
    assert outcome(d, [2.0, 2.0]) == at_the_shared_value


def test_equal_no_go_and_caution_values_resolve_to_no_go() -> None:
    d = defn(
        forecast_margin=0.0,
        no_go_when={"comparison": ">", "value": 2.0},
        caution_when={"comparison": ">", "value": 2.0},
    )
    assert outcome(d, [2.5, 2.5]) == R.NO_GO


def test_negative_zero_is_just_zero() -> None:
    d = defn(
        worse_when="lower",
        forecast_margin=0.0,
        no_go_when={"comparison": "<", "value": -0.0},
        caution_when={"comparison": "<", "value": 0.0},
        go_when={"comparison": ">=", "value": 0.0},
    )
    assert outcome(d, [0.0, 0.0]) == R.GO


class _ValidatedNoExpiry:
    """A rule that claims a VALIDATED result but forgot to declare an expiry."""

    rule_id = "synthetic.forgot-expiry"
    citation = "test fixture (synthetic)"

    def evaluate(self, plan: DivePlan, evidence: Any) -> Any:
        from divesafe.domain import RuleResult

        return RuleResult(
            rule_id=self.rule_id,
            outcome=R.GO,
            rationale="x",
            factor=K.WAVE_HEIGHT,
            threshold_status=ThresholdStatus.VALIDATED,
        )


def test_a_validated_result_from_a_rule_without_an_expiry_is_refused() -> None:
    policy = EvidencePolicy(frozenset({DataCategory.WAVES_SWELL}), H, degraded_may_support_go=True)
    engine = RiskRulesEngine(
        [_ValidatedNoExpiry()], policy, "synthetic", required_factors=frozenset({K.WAVE_HEIGHT})
    )
    result = engine.assess(PLAN, evidence({"a": 0.1, "b": 0.1}), NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id.startswith("rule.validated_without_expiry") for r in result.rule_results)
    assert K.WAVE_HEIGHT in result.unevaluated_factors


def test_every_definition_rule_declares_an_expiry() -> None:
    rule = DefinitionRule(defn())
    assert rule.expires_at == defn().expires_on


# --- second review: citations, several definitions per factor, expiry robustness -----------------


def _hourly(values: list[float], first: timedelta = timedelta(0)) -> list[EvidenceItem]:
    items = []
    for i, value in enumerate(values):
        base = evidence({"x": value})[0]
        items.append(base.model_copy(update={"id": f"s{i}", "valid_at": START + first + i * H}))
    return items


def _go_engine(*rules: Any) -> RiskRulesEngine:
    policy = EvidencePolicy(frozenset({DataCategory.WAVES_SWELL}), H, degraded_may_support_go=True)
    return RiskRulesEngine(rules, policy, "synthetic", required_factors=frozenset({K.WAVE_HEIGHT}))


def test_a_dive_between_two_hourly_samples_can_reach_go_through_the_engine() -> None:
    """Regression: the rule cited its bracketing samples, but the citation guard only admitted
    in-window ones, so GO and CAUTION were unreachable on ordinary hourly data."""
    between = PLAN.model_copy(
        update={"planned_start": START + timedelta(minutes=30), "planned_duration_minutes": 60}
    )
    samples = _hourly([0.1, 0.1])  # at START and START + 1h; the dive is 30-90 minutes after START
    samples = [samples[0], samples[1].model_copy(update={"valid_at": START + 2 * H})]
    engine = _go_engine(DefinitionRule(defn(forecast_margin=0.0)))
    result = engine.assess(between, samples, NOW)
    assert result.recommendation == R.GO
    assert not any(r.rule_id.startswith("rule.cites_unusable") for r in result.rule_results)


def test_a_high_bracketing_sample_makes_the_engine_result_no_go() -> None:
    between = PLAN.model_copy(
        update={"planned_start": START + timedelta(minutes=30), "planned_duration_minutes": 60}
    )
    first = _hourly([5.0])[0]
    second = _hourly([0.1])[0].model_copy(update={"id": "s1", "valid_at": START + 2 * H})
    engine = _go_engine(DefinitionRule(defn(forecast_margin=0.0)))
    assert engine.assess(between, [first, second], NOW).recommendation == R.NO_GO


def test_only_the_nearest_bracketing_instant_is_a_legitimate_citation() -> None:
    far = _hourly([0.1])[0].model_copy(update={"id": "far", "valid_at": START - 6 * H})
    near_before = _hourly([0.1])[0].model_copy(update={"id": "near", "valid_at": START - H})
    after = _hourly([0.1])[0].model_copy(update={"id": "after", "valid_at": END + H})
    engine = _go_engine(_Go(cites=("far",)))  # cites an instant that is not the nearest
    result = engine.assess(PLAN, [far, near_before, after], NOW)
    assert any(r.rule_id.startswith("rule.cites_unusable") for r in result.rule_results)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE


class _Go:
    rule_id = "synthetic.cites"
    citation = "test fixture (synthetic)"

    def __init__(self, cites: tuple[str, ...]) -> None:
        self._cites = cites

    def evaluate(self, plan: DivePlan, evidence: Any) -> Any:
        from divesafe.domain import RuleResult

        return RuleResult(
            rule_id=self.rule_id, outcome=R.GO, rationale="x", evidence_ids=self._cites
        )


def _two_sites() -> tuple[DefinitionRule, DefinitionRule]:
    a = DefinitionRule(
        defn(id="synthetic.site-a", scope={"site_ids": ["site-a"]}, forecast_margin=0.0)
    )
    b = DefinitionRule(
        defn(id="synthetic.site-b", scope={"site_ids": ["site-b"]}, forecast_margin=0.0)
    )
    return a, b


def test_several_definitions_for_one_factor_do_not_block_each_other() -> None:
    from divesafe.risk import FactorScopeRule

    a, b = _two_sites()
    engine = _go_engine(a, b, FactorScopeRule(K.WAVE_HEIGHT, [a, b]))
    items = evidence({"a": 0.1, "b": 0.1})
    for site in ("site-a", "site-b"):
        plan = PLAN.model_copy(update={"site_id": site})
        assert engine.assess(plan, items, NOW).recommendation == R.GO, site


def test_when_no_definition_for_the_factor_applies_the_result_says_so_and_blocks_go() -> None:
    from divesafe.risk import FactorScopeRule

    a, b = _two_sites()
    engine = _go_engine(a, b, FactorScopeRule(K.WAVE_HEIGHT, [a, b]))
    other = PLAN.model_copy(update={"site_id": "site-c"})
    result = engine.assess(other, evidence({"a": 0.1, "b": 0.1}), NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    scope = next(r for r in result.rule_results if r.rule_id == "definition-scope.wave_height")
    assert "No signed-off rule for wave_height applies" in scope.rationale
    assert scope.threshold_status is None and K.WAVE_HEIGHT in result.unevaluated_factors


def test_a_naive_or_malformed_expiry_is_a_recorded_failure_not_a_crash() -> None:
    class _NaiveExpiry:
        rule_id = "synthetic.naive-expiry"
        citation = "test fixture (synthetic)"
        expires_at = datetime(2030, 1, 1)  # naive: comparing with an aware now raises TypeError

        def evaluate(self, plan: DivePlan, evidence: Any) -> None:
            return None

    engine = _go_engine(_NaiveExpiry())
    result = engine.assess(PLAN, evidence({"a": 0.1, "b": 0.1}), NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id == "rule.error.synthetic.naive-expiry" for r in result.rule_results)
