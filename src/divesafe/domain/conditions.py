"""Typed environmental conditions. Units are in the field names. A `None` field means "not
available", never zero. Validation here is physical plausibility only (finite, non-negative
magnitudes, bearings within 0-360); it is NOT a safety threshold."""

from __future__ import annotations

from pydantic import Field

from divesafe.domain.base import Frozen


class _Conditions(Frozen):
    def missing(self) -> tuple[str, ...]:
        """Names of fields with no value."""
        return tuple(name for name in type(self).model_fields if getattr(self, name) is None)

    @property
    def is_complete(self) -> bool:
        return not self.missing()


class WeatherConditions(_Conditions):
    wind_speed_kmh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wind_gust_kmh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wind_direction_deg: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)
    summary: str | None = None


class WaveConditions(_Conditions):
    wave_height_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wave_period_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wave_direction_deg: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)
    wind_wave_height_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    swell_height_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    swell_period_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    swell_direction_deg: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)


class OceanConditions(_Conditions):
    sea_surface_temperature_c: float | None = Field(default=None, allow_inf_nan=False)
    current_speed_kmh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    current_direction_deg: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)


class TidalConditions(_Conditions):
    """Tide predictions and tidal streams. No verified source exists yet (docs/data-sources.md);
    a model's sea level relative to global mean sea level is NOT a tide prediction."""

    tide_height_m: float | None = Field(default=None, allow_inf_nan=False)
    vertical_datum: str | None = None
    tidal_current_speed_kmh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tidal_current_direction_deg: float | None = Field(
        default=None, ge=0, le=360, allow_inf_nan=False
    )
