"""Risk vocabulary shared by rules, results and the audit record."""

from __future__ import annotations

from enum import StrEnum


class RiskFactorKind(StrEnum):
    """The factors the risk engine must eventually evaluate."""

    WIND = "wind"
    WAVE_HEIGHT = "wave_height"
    SWELL = "swell"
    SWELL_PERIOD = "swell_period"
    CURRENT = "current"
    TIDAL_CURRENT = "tidal_current"
    WEATHER = "weather"
    MARINE_WARNING = "marine_warning"
    FORECAST_UNCERTAINTY = "forecast_uncertainty"
    DATA_FRESHNESS = "data_freshness"
    DATA_QUALITY = "data_quality"
    SITE_CONSTRAINT = "site_constraint"


class ThresholdStatus(StrEnum):
    """Where a rule's numeric limit came from. Only VALIDATED may support a GO."""

    TBD = "TBD"
    REQUIRES_DOMAIN_VALIDATION = "REQUIRES DOMAIN VALIDATION"
    VALIDATED = "VALIDATED"


# Factors that need a numeric limit and so cannot be evaluated until one is validated.
THRESHOLD_FACTORS: frozenset[RiskFactorKind] = frozenset(
    {
        RiskFactorKind.WIND,
        RiskFactorKind.WAVE_HEIGHT,
        RiskFactorKind.SWELL,
        RiskFactorKind.SWELL_PERIOD,
        RiskFactorKind.CURRENT,
        RiskFactorKind.TIDAL_CURRENT,
        RiskFactorKind.WEATHER,
        RiskFactorKind.FORECAST_UNCERTAINTY,
        RiskFactorKind.SITE_CONSTRAINT,
    }
)
