"""Open-Meteo Marine API connector (model forecasts; non-commercial use only).

Docs: https://open-meteo.com/en/docs/marine-weather-api  Terms: https://open-meteo.com/en/terms

Limits that matter for safety (from the provider's own documentation):
- Values are numerical model output, never observations. Every item has `is_forecast=True` and
  `data_type="model"`, including hours already past, because they are still model values.
- Grid is about 8-25 km. Currents are computed at about 8 km; coastal accuracy is limited and
  the data is "not suitable for coastal navigation". The provider snaps the request to a grid
  cell; the requested point, the cell and the distance between them are recorded in each item.
  This is regional guidance, not a site-level measurement.
- `sea_level_height_msl` is deliberately NOT requested: it is relative to global mean sea level
  and is not a tide prediction, so it must never satisfy a `tides` requirement.
- Free tier is non-commercial only, CC BY 4.0, attribution to DWD and Open-Meteo required.
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
from divesafe.data.parsing import as_dict, clip, haversine_km, parse_naive
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

ENDPOINT = "https://marine-api.open-meteo.com/v1/marine"
SOURCE = "open-meteo-marine"
SOURCE_VERSION = "v1"
ATTRIBUTION = "Marine data: Open-Meteo.com (CC BY 4.0), wave models from DWD and others."
MAX_HOURS = 24 * 16

# variable -> (category, expected unit, kind)
_VARIABLES: dict[str, tuple[DataCategory, str, str]] = {
    "wave_height": (DataCategory.WAVES_SWELL, "m", "non_negative"),
    "wave_direction": (DataCategory.WAVES_SWELL, "°", "direction"),
    "wave_period": (DataCategory.WAVES_SWELL, "s", "non_negative"),
    "wind_wave_height": (DataCategory.WAVES_SWELL, "m", "non_negative"),
    "swell_wave_height": (DataCategory.WAVES_SWELL, "m", "non_negative"),
    "swell_wave_direction": (DataCategory.WAVES_SWELL, "°", "direction"),
    "swell_wave_period": (DataCategory.WAVES_SWELL, "s", "non_negative"),
    "sea_surface_temperature": (DataCategory.SEA_TEMPERATURE, "°C", "any"),
    "ocean_current_velocity": (DataCategory.CURRENTS, "km/h", "non_negative"),
    "ocean_current_direction": (DataCategory.CURRENTS, "°", "direction"),
}


def _valid(kind: str, value: float) -> bool:
    if not math.isfinite(value):
        return False
    if kind == "non_negative":
        return value >= 0
    if kind == "direction":
        return 0 <= value <= 360
    return True


def _floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _ceil_hour(moment: datetime) -> datetime:
    floored = _floor_hour(moment)
    return floored if floored == moment.astimezone(UTC) else floored + timedelta(hours=1)


class OpenMeteoMarineConnector:
    name = SOURCE

    def __init__(self, getter: JsonGetter) -> None:
        self._getter = getter

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        first = _floor_hour(window_start)
        last = _ceil_hour(window_end)
        params = {
            "latitude": f"{site.latitude}",
            "longitude": f"{site.longitude}",
            "hourly": ",".join(_VARIABLES),
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
        grid_lat, grid_lon = body.get("latitude"), body.get("longitude")
        if (
            isinstance(grid_lat, bool)
            or isinstance(grid_lon, bool)
            or not isinstance(grid_lat, int | float)
            or not isinstance(grid_lon, int | float)
        ):
            raise ConnectorResponseError("missing grid cell coordinates")
        grid_km = round(haversine_km(site.latitude, site.longitude, grid_lat, grid_lon), 1)

        expected: list[datetime] = []
        step = first
        while step <= last:
            expected.append(step)
            step += timedelta(hours=1)
        index = {t: i for i, t in enumerate(times)}

        items: list[EvidenceItem] = []
        issues: list[str] = []
        for category in dict.fromkeys(c for c, _, _ in _VARIABLES.values()):
            names = [n for n, (c, _, _) in _VARIABLES.items() if c == category]
            problem = self._category_problem(names, expected, index, units, hourly)
            if problem:
                issues.append(f"{category.value}: {problem}")
                logger.warning(
                    "marine category rejected",
                    extra={"category": category.value, "problem": problem, "site": site.id},
                )
                continue
            for moment in expected:
                i = index[moment]
                values = {n: float(hourly[n][i]) for n in names}
                items.append(
                    EvidenceItem(
                        id=f"{SOURCE}:{category.value}:{site.id}:{moment:%Y%m%dT%H%MZ}",
                        category=category,
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
                        quality=DataQuality.DEGRADED,
                        quality_notes=(
                            f"regional model grid cell {grid_km} km from the requested point",
                            "provider: coastal accuracy limited; not suitable for navigation",
                        ),
                        transformations=(
                            TransformationStep(step="requested in UTC; hour timestamps parsed"),
                            TransformationStep(
                                step=UNIT_CHECK,
                                detail=", ".join(f"{n}={units[n]}" for n in names),
                            ),
                            TransformationStep(step=RANGE_CHECK, detail="finite, plausible range"),
                            TransformationStep(
                                step="grid snap",
                                detail=f"{grid_km} km from the requested point",
                            ),
                        ),
                        value={
                            "variables": values,
                            "units": {n: units[n] for n in names},
                            "data_type": "model",
                            "spatial_scope": "regional model grid cell, not the dive site",
                            "requested_point": [site.latitude, site.longitude],
                            "grid_cell": [grid_lat, grid_lon],
                            "grid_distance_km": grid_km,
                            "attribution": ATTRIBUTION,
                        },
                    )
                )
        return FetchResult(tuple(items), tuple(issues))

    @staticmethod
    def _category_problem(
        names: list[str],
        expected: list[datetime],
        index: Mapping[datetime, int],
        units: Mapping[str, Any],
        hourly: Mapping[str, Any],
    ) -> str | None:
        missing_hours = [t for t in expected if t not in index]
        if missing_hours:
            return f"{len(missing_hours)} requested hour(s) not returned"
        for name in names:
            _, unit, kind = _VARIABLES[name]
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
