"""Domain foundation: plans, typed conditions, provenance, sites, confidence.

Required-test mapping: (1) valid DivePlan, (2) invalid environmental data rejected, (4) missing
data detection, (6) Recommendation enum validation, (7) human decision recording.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.agent.scripted_llm import run_pipeline
from tests.conftest import NOW, make_evidence

from divesafe.domain import (
    ActualConditions,
    ConfidenceAssessment,
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    DiveSite,
    Evidence,
    EvidenceItem,
    GeoPoint,
    HumanDecision,
    OceanConditions,
    PostDiveObservation,
    Recommendation,
    RiskFactor,
    RuleResult,
    SiteConstraint,
    ThresholdStatus,
    TidalConditions,
    WaveConditions,
    WeatherConditions,
    effective_quality,
    observation_from_evidence,
)
from divesafe.orchestration import decide

T = datetime(2026, 1, 1, 12, tzinfo=UTC)


# --- (1) DivePlan ---------------------------------------------------------------------------


def test_valid_dive_plan_is_constructed() -> None:
    plan = DivePlan(site_id="s", planned_start=T, planned_duration_minutes=45, max_depth_m=18)
    assert plan.max_depth_m == 18 and plan.planned_start.tzinfo is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"max_depth_m": 0},
        {"max_depth_m": -5},
        {"planned_duration_minutes": 0},
        {"site_id": ""},
        {"planned_start": datetime(2026, 1, 1, 12)},  # naive datetime
        {"unexpected": 1},
    ],
)
def test_invalid_dive_plans_are_rejected(changes: dict[str, object]) -> None:
    base: dict[str, object] = {
        "site_id": "s",
        "planned_start": T,
        "planned_duration_minutes": 45,
        "max_depth_m": 18,
    }
    with pytest.raises(ValidationError):
        DivePlan(**{**base, **changes})  # type: ignore[arg-type]


# --- (2) invalid environmental data is rejected --------------------------------------------


@pytest.mark.parametrize(
    ("model", "field", "bad"),
    [
        (WaveConditions, "wave_height_m", -0.1),
        (WaveConditions, "swell_period_s", -3),
        (WaveConditions, "wave_direction_deg", 361),
        (WaveConditions, "swell_direction_deg", -1),
        (WaveConditions, "wave_height_m", float("nan")),
        (OceanConditions, "current_speed_kmh", -1),
        (OceanConditions, "current_direction_deg", 400),
        (OceanConditions, "sea_surface_temperature_c", float("inf")),
        (WeatherConditions, "wind_speed_kmh", -10),
        (WeatherConditions, "wind_direction_deg", 720),
        (TidalConditions, "tidal_current_speed_kmh", -2),
    ],
)
def test_implausible_conditions_are_rejected_not_coerced(
    model: type, field: str, bad: float
) -> None:
    with pytest.raises(ValidationError):
        model(**{field: bad})


def test_unknown_condition_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WaveConditions(wave_height_metres=1.0)  # type: ignore[call-arg]


def test_negative_wave_height_in_evidence_is_unusable() -> None:
    bad = EvidenceItem(
        id="bad-wave",
        category=DataCategory.WAVES_SWELL,
        source="t",
        retrieved_at=NOW,
        valid_at=NOW,
        is_forecast=True,
        data_kind=DataKind.MODEL,
        value={"variables": {"wave_height": -2.0}, "units": {"wave_height": "m"}},
    )
    with pytest.raises(ValidationError):
        observation_from_evidence(bad)


def test_connector_evidence_parses_into_typed_conditions() -> None:
    record = run_pipeline(None)
    typed = [o for e in record.evidence if (o := observation_from_evidence(e)) is not None]
    assert typed and all(o.evidence_id for o in typed)
    waves = next(o for o in typed if o.category == DataCategory.WAVES_SWELL)
    assert (
        isinstance(waves.conditions, WaveConditions) and waves.conditions.wave_height_m is not None
    )


# --- (4) missing data detection ------------------------------------------------------------


def test_missing_condition_fields_are_reported_never_defaulted_to_zero() -> None:
    waves = WaveConditions(wave_height_m=0.4)
    assert waves.swell_height_m is None  # not 0
    assert "swell_height_m" in waves.missing() and "wave_height_m" not in waves.missing()
    assert not waves.is_complete
    assert WaveConditions().missing() == tuple(WaveConditions.model_fields)


def test_a_complete_reading_reports_no_missing_fields() -> None:
    ocean = OceanConditions(
        sea_surface_temperature_c=30.0, current_speed_kmh=1.0, current_direction_deg=90
    )
    assert ocean.is_complete and ocean.missing() == ()


def test_unmapped_categories_have_no_typed_observation() -> None:
    for category in (DataCategory.TIDES, DataCategory.MARINE_WARNINGS, DataCategory.LOCAL_GUIDANCE):
        assert observation_from_evidence(make_evidence(category)) is None


def _marine_like(variables: dict[str, object], units: dict[str, object]) -> EvidenceItem:
    return EvidenceItem(
        id="x",
        category=DataCategory.WAVES_SWELL,
        source="t",
        retrieved_at=NOW,
        valid_at=NOW,
        is_forecast=True,
        data_kind=DataKind.MODEL,
        value={"variables": variables, "units": units},
    )


@pytest.mark.parametrize("units", [{}, {"wave_height": "ft"}, {"wave_height": None}])
def test_a_missing_or_different_unit_is_refused_never_converted(units: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="expected unit"):
        observation_from_evidence(_marine_like({"wave_height": 1.0}, units))


def test_wind_evidence_parses_into_canonical_weather_conditions() -> None:
    from tests.safety.test_wind_evidence_safety import _evidence  # real recorded wind

    wind = next(e for e in _evidence() if e.category == DataCategory.WIND)
    observation = observation_from_evidence(wind)
    assert observation is not None and isinstance(observation.conditions, WeatherConditions)
    assert observation.conditions.wind_speed_kmh == wind.value["variables"]["wind_speed_10m"]
    assert observation.conditions.wind_gust_kmh == wind.value["variables"]["wind_gusts_10m"]
    assert (
        observation.conditions.wind_direction_deg == wind.value["variables"]["wind_direction_10m"]
    )


def test_implausible_wind_is_refused() -> None:
    from tests.safety.test_wind_evidence_safety import _evidence

    wind = next(e for e in _evidence() if e.category == DataCategory.WIND)
    bad = wind.model_copy(
        update={
            "value": {
                **wind.value,
                "variables": {**wind.value["variables"], "wind_speed_10m": -5.0},
            }
        }
    )
    with pytest.raises(ValidationError):
        observation_from_evidence(bad)


# --- (6) Recommendation enum ----------------------------------------------------------------


def test_recommendation_has_exactly_the_four_documented_values() -> None:
    assert {r.value for r in Recommendation} == {
        "GO",
        "CAUTION",
        "NO-GO",
        "INSUFFICIENT EVIDENCE",
    }


@pytest.mark.parametrize("bad", ["MAYBE", "go", "NO_GO", "", "INSUFFICIENT_EVIDENCE"])
def test_recommendation_rejects_anything_else(bad: str) -> None:
    with pytest.raises(ValueError):
        Recommendation(bad)
    with pytest.raises(ValidationError):
        RuleResult(rule_id="r", outcome=bad, rationale="x")  # type: ignore[arg-type]


# --- (7) human decision recording ----------------------------------------------------------


def test_human_decision_is_recorded_with_identity_and_rationale() -> None:
    pending = run_pipeline(None)
    when = pending.created_at + timedelta(minutes=1)
    decided = decide(
        pending,
        decided_by="leader-1",
        decision=Recommendation.NO_GO,
        decided_at=when,
        rationale="Visibility briefing.",
    )
    human = decided.human_decision
    assert human is not None
    assert (human.decided_by, human.decided_at, human.is_override) == ("leader-1", when, True)
    assert human.override_rationale == "Visibility briefing."
    assert pending.human_decision is None  # the original is untouched


def test_human_decision_rejects_blank_identity_and_unexplained_overrides() -> None:
    with pytest.raises(ValidationError):
        HumanDecision(decided_by=" ", decision=Recommendation.GO, decided_at=T, is_override=False)
    with pytest.raises(ValidationError):
        HumanDecision(decided_by="a", decision=Recommendation.GO, decided_at=T, is_override=True)


# --- provenance ----------------------------------------------------------------------------


def _item(**changes: object) -> EvidenceItem:
    base: dict[str, object] = {
        "id": "e1",
        "category": DataCategory.WAVES_SWELL,
        "source": "test",
        "retrieved_at": T,
        "valid_at": T,
        "is_forecast": True,
        "data_kind": DataKind.MODEL,
        "value": {},
    }
    return EvidenceItem(**{**base, **changes})  # type: ignore[arg-type]


def test_evidence_must_declare_what_kind_of_data_it_is() -> None:
    data = {k: v for k, v in _item().model_dump().items() if k != "data_kind"}
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate(data)


@pytest.mark.parametrize(
    ("kind", "is_forecast"),
    [
        (DataKind.OBSERVATION, True),  # a measurement cannot be a forecast
        (DataKind.NOTICE, True),
        (DataKind.MODEL, False),  # model output cannot pass as measured
        (DataKind.FORECAST, False),
        (DataKind.PREDICTION, False),
    ],
)
def test_data_kind_cannot_contradict_the_forecast_flag(kind: DataKind, is_forecast: bool) -> None:
    with pytest.raises(ValidationError):
        _item(data_kind=kind, is_forecast=is_forecast)


def test_validity_window_must_not_end_before_it_starts() -> None:
    with pytest.raises(ValidationError):
        _item(valid_until=T - timedelta(hours=1))
    assert _item(valid_until=T + timedelta(hours=1)).valid_until is not None


def test_rejected_data_cannot_be_stored_as_evidence() -> None:
    with pytest.raises(ValidationError):
        _item(quality=DataQuality.REJECTED)


def test_quality_defaults_to_unassessed_not_good() -> None:
    assert _item().quality == DataQuality.UNASSESSED


def test_geopoint_is_validated() -> None:
    assert GeoPoint(latitude=5.8, longitude=103.0).latitude == 5.8
    for lat, lon in ((91, 0), (0, 181), (float("nan"), 0)):
        with pytest.raises(ValidationError):
            GeoPoint(latitude=lat, longitude=lon)


def test_connector_evidence_is_traceable_end_to_end() -> None:
    record = run_pipeline(None)
    marine = next(e for e in record.evidence if e.category == DataCategory.WAVES_SWELL)
    assert marine.source and marine.source_version
    assert marine.retrieved_at and marine.valid_at
    assert marine.data_kind == DataKind.MODEL and marine.is_forecast is True
    assert marine.location is not None  # the model grid cell
    assert marine.quality == DataQuality.DEGRADED and marine.quality_notes
    assert {t.step for t in marine.transformations} >= {"unit check", "range check", "grid snap"}

    warning = next(e for e in record.evidence if e.category == DataCategory.MARINE_WARNINGS)
    assert warning.data_kind == DataKind.NOTICE and warning.is_forecast is False
    assert warning.valid_until is not None and warning.valid_until >= warning.valid_at
    assert any(t.step == "timezone" for t in warning.transformations)
    assert warning.quality == DataQuality.DEGRADED  # usable with stated limitations
    assert effective_quality(warning) == DataQuality.DEGRADED


# --- site, confidence, vocabulary ----------------------------------------------------------


def test_dive_site_needs_a_coordinate_source_and_constraints_start_as_tbd() -> None:
    with pytest.raises(ValidationError):
        DiveSite(id="s", name="n", latitude=1, longitude=1, coordinate_source="")
    site = DiveSite(
        id="s",
        name="n",
        latitude=5.8,
        longitude=103.0,
        coordinate_source="test",
        constraints=(SiteConstraint(id="c", description="exposure to NE swell"),),
    )
    assert site.location.latitude == 5.8
    assert site.constraints[0].threshold_status == ThresholdStatus.TBD


def test_confidence_is_not_computed_by_default_and_a_number_needs_a_method() -> None:
    empty = ConfidenceAssessment()
    assert empty.value is None and "not a measure of dive safety" in empty.note
    with pytest.raises(ValidationError):
        ConfidenceAssessment(value=0.8)
    assert ConfidenceAssessment(value=0.8, method="documented method v1").value == 0.8


def test_project_vocabulary_aliases_point_at_the_real_types() -> None:
    assert Evidence is EvidenceItem
    assert RiskFactor is RuleResult
    assert PostDiveObservation is ActualConditions


def test_the_record_refuses_a_confidence_number_without_a_documented_method() -> None:
    from divesafe.domain import AssessmentRecord

    record = run_pipeline(None)
    assert record.confidence is None and record.confidence_method is None
    data = record.model_dump()
    data["confidence"] = 0.9
    with pytest.raises(ValidationError):
        AssessmentRecord.model_validate(data)
    data["confidence_method"] = "documented method v1"
    assert AssessmentRecord.model_validate(data).confidence == 0.9
