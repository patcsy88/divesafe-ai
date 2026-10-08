"""Typed observations built from evidence. Boundary plumbing only: it parses a connector's
`value` payload into validated conditions so invalid data is rejected rather than coerced."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import AwareDatetime, Field

from divesafe.domain.base import Frozen
from divesafe.domain.conditions import (
    OceanConditions,
    TidalConditions,
    WaveConditions,
    WeatherConditions,
)
from divesafe.domain.models import DataCategory, EvidenceItem

Conditions = WeatherConditions | WaveConditions | OceanConditions | TidalConditions


class MarineWarning(Frozen):
    """A warning or advisory as published. Applicability to a site is never decided here."""

    title: str
    heading: str
    text: str
    issued: AwareDatetime
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    applicability: str = "NOT DETERMINED: free text; a human must read it"


class EnvironmentalObservation(Frozen):
    """Conditions plus the evidence (and so the provenance) they came from."""

    evidence_id: str = Field(min_length=1)
    category: DataCategory
    valid_at: AwareDatetime
    conditions: Conditions


_WAVE_KEYS: Mapping[str, str] = {
    "wave_height": "wave_height_m",
    "wave_period": "wave_period_s",
    "wave_direction": "wave_direction_deg",
    "wind_wave_height": "wind_wave_height_m",
    "swell_wave_height": "swell_height_m",
    "swell_wave_period": "swell_period_s",
    "swell_wave_direction": "swell_direction_deg",
}
_OCEAN_KEYS: Mapping[str, str] = {
    "sea_surface_temperature": "sea_surface_temperature_c",
    "ocean_current_velocity": "current_speed_kmh",
    "ocean_current_direction": "current_direction_deg",
}


def observation_from_evidence(item: EvidenceItem) -> EnvironmentalObservation | None:
    """Typed conditions for marine-model evidence; None for categories without a typed form.

    Raises `pydantic.ValidationError` for implausible values (negative magnitude, bearing outside
    0-360, NaN), which callers must treat as unusable data.
    """
    variables: dict[str, Any] = dict(item.value.get("variables", {}))
    conditions: Conditions
    if item.category == DataCategory.WAVES_SWELL:
        conditions = WaveConditions(
            **{_WAVE_KEYS[k]: v for k, v in variables.items() if k in _WAVE_KEYS}
        )
    elif item.category in (DataCategory.SEA_TEMPERATURE, DataCategory.CURRENTS):
        conditions = OceanConditions(
            **{_OCEAN_KEYS[k]: v for k, v in variables.items() if k in _OCEAN_KEYS}
        )
    else:
        return None
    return EnvironmentalObservation(
        evidence_id=item.id, category=item.category, valid_at=item.valid_at, conditions=conditions
    )
