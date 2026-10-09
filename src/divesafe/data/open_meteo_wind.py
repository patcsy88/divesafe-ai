"""Open-Meteo Forecast API connector for wind at 10 m (model forecasts; non-commercial use only).

Docs: https://open-meteo.com/en/docs  Licence: https://open-meteo.com/en/licence (CC BY 4.0;
a link "Weather data by Open-Meteo.com" must appear next to displayed data).

Facts and limits (from the provider's documentation and a live response, 2026-10-09):
- Values are numerical model output, never observations: `is_forecast=True`, `data_type="model"`.
- `wind_speed_10m` and `wind_direction_10m` are instantaneous; `wind_gusts_10m` is the MAXIMUM OF
  THE PRECEDING HOUR. The item's `valid_at` is the stamped hour, so a gust value describes the hour
  before it.
- The default "best match" selects a model per location and the response does NOT say which one
  was used, so the producing model is unknown. This is recorded in every item's quality notes.
- Resolution depends on the selected model (the docs give 1 to 55 km). The provider snaps the
  request to a grid cell; the requested point, the cell and the distance are recorded.
- The docs page read does not state the direction convention for `wind_direction_10m`. Treat
  "direction the wind blows FROM" as unverified before any exposure rule relies on it.
- Units are pinned in the request (`wind_speed_unit=kmh`) and checked in the response.
- Free tier is non-commercial only (CC BY 4.0, under 10,000 calls a day).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from divesafe.data.base import FetchResult
from divesafe.data.errors import ConnectorResponseError
from divesafe.data.http import JsonGetter
from divesafe.data.parsing import (
    as_dict,
    ceil_hour,
    check_grid_cell,
    clip,
    floor_hour,
    haversine_km,
    parse_naive,
)
from divesafe.domain import (
    RANGE_CHECK,
    UNIT_CHECK,
    DataCategory,
    DataKind,
    DataQuality,
    DiveSite,
    EvidenceItem,
    GeoPoint,
    TransformationStep,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
SOURCE = "open-meteo-wind"
SOURCE_VERSION = "v1"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0), https://open-meteo.com/"
MAX_HOURS = 24 * 16

# variable -> (expected unit, kind)
_VARIABLES: dict[str, tuple[str, str]] = {
    "wind_speed_10m": ("km/h", "non_negative"),
    "wind_gusts_10m": ("km/h", "non_negative"),
    "wind_direction_10m": ("°", "direction"),
}
_SEMANTICS = {
    "wind_speed_10m": "instantaneous value at the stamped hour",
    "wind_direction_10m": "instantaneous value at the stamped hour; convention not verified",
    "wind_gusts_10m": "maximum of the preceding hour",
}


def _valid(kind: str, value: float) -> bool:
    if not math.isfinite(value):
        return False
    return value >= 0 if kind == "non_negative" else 0 <= value <= 360


class OpenMeteoWindConnector:
    name = SOURCE

    def __init__(self, getter: JsonGetter) -> None:
        self._getter = getter

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        first = floor_hour(window_start)
        last = ceil_hour(window_end)
        params = {
            "latitude": f"{site.latitude}",
            "longitude": f"{site.longitude}",
            "hourly": ",".join(_VARIABLES),
            "wind_speed_unit": "kmh",
            "start_date": first.date().isoformat(),
            "end_date": last.date().isoformat(),
            "timezone": "UTC",
            "cell_selection": "sea",
        }
        payload = await self._getter.get_json(ENDPOINT, params)
        try:
            return self.parse(payload, site, first, last, now)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ConnectorResponseError(f"unusable response: {clip(exc)}") from exc

    def parse(
        self, payload: Any, site: DiveSite, first: datetime, last: datetime, now: datetime
    ) -> FetchResult:
        body = as_dict(payload, "response")
        if body.get("error"):
            raise ConnectorResponseError(f"provider reported an error: {clip(body.get('reason'))}")
        if body.get("utc_offset_seconds") != 0:
            raise ConnectorResponseError("expected UTC timestamps")
        units = as_dict(body.get("hourly_units"), "hourly_units")
        hourly = as_dict(body.get("hourly"), "hourly")
        times = _parse_times(hourly.get("time"))
        grid_lat, grid_lon = check_grid_cell(body.get("latitude"), body.get("longitude"))
        grid_km = round(haversine_km(site.latitude, site.longitude, grid_lat, grid_lon), 1)

        expected: list[datetime] = []
        step = first
        while step <= last:
            expected.append(step)
            step += timedelta(hours=1)
        index = {t: i for i, t in enumerate(times)}

        problem = self._problem(expected, index, units, hourly)
        if problem:
            logger.warning("wind rejected", extra={"problem": problem, "site": site.id})
            return FetchResult((), (f"wind: {problem}",))

        items = [
            EvidenceItem(
                id=f"{SOURCE}:wind:{site.id}:{moment:%Y%m%dT%H%MZ}",
                category=DataCategory.WIND,
                source=SOURCE,
                source_version=SOURCE_VERSION,
                retrieved_at=now,
                valid_at=moment,
                is_forecast=True,
                data_kind=DataKind.MODEL,
                location=GeoPoint(
                    latitude=grid_lat,
                    longitude=grid_lon,
                    description="model grid cell chosen by the provider (sea preferred)",
                ),
                upstream=(),
                upstream_known=False,  # "best match" picks a model and does not say which
                quality=DataQuality.DEGRADED,
                quality_notes=(
                    f"regional model grid cell {grid_km} km from the requested point",
                    "the producing model is not reported (automatic 'best match' selection)",
                    "wind_gusts_10m is the maximum of the preceding hour",
                    "wind_direction_10m convention not verified against the provider docs",
                ),
                transformations=(
                    TransformationStep(step="requested in UTC with wind_speed_unit=kmh"),
                    TransformationStep(
                        step=UNIT_CHECK,
                        detail=", ".join(f"{n}={units[n]}" for n in _VARIABLES),
                    ),
                    TransformationStep(step=RANGE_CHECK, detail="finite, plausible range"),
                    TransformationStep(
                        step="grid snap", detail=f"{grid_km} km from the requested point"
                    ),
                ),
                value={
                    "variables": {n: float(hourly[n][index[moment]]) for n in _VARIABLES},
                    "units": {n: units[n] for n in _VARIABLES},
                    "variable_semantics": dict(_SEMANTICS),
                    "data_type": "model",
                    "spatial_scope": "regional model grid cell, not the dive site",
                    "requested_point": [site.latitude, site.longitude],
                    "grid_cell": [grid_lat, grid_lon],
                    "grid_distance_km": grid_km,
                    "attribution": ATTRIBUTION,
                },
            )
            for moment in expected
        ]
        return FetchResult(tuple(items))

    @staticmethod
    def _problem(
        expected: list[datetime],
        index: Mapping[datetime, int],
        units: Mapping[str, Any],
        hourly: Mapping[str, Any],
    ) -> str | None:
        missing = [t for t in expected if t not in index]
        if missing:
            return f"{len(missing)} requested hour(s) not returned"
        for name, (unit, kind) in _VARIABLES.items():
            if units.get(name) != unit:
                return f"unexpected unit for {name}: {clip(units.get(name), 20)} (expected {unit})"
            series = hourly.get(name)
            if not isinstance(series, list) or len(series) <= max(index.values()):
                return f"{name} series is missing or short"
            for moment in expected:
                value = series[index[moment]]
                if isinstance(value, bool) or not isinstance(value, int | float):
                    return f"{name} has a missing or non-numeric value"
                if not _valid(kind, float(value)):
                    return f"{name} has an out-of-range value"
        return None


def _parse_times(raw: Any) -> list[datetime]:
    if not isinstance(raw, list) or not raw:
        raise ConnectorResponseError("hourly.time is missing or empty")
    if len(raw) > MAX_HOURS:
        raise ConnectorResponseError("hourly.time is unreasonably long")
    times = [parse_naive(t, "hourly.time").replace(tzinfo=UTC) for t in raw]
    if len(set(times)) != len(times):
        raise ConnectorResponseError("hourly.time contains duplicates")
    return times
