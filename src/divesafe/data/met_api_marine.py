"""MET Malaysia API v2.1 marine forecast for a waters zone (daily ranges; degraded evidence).

Docs: the MET developer guide (behind the portal login; read 2026-10-16). Facts, from a real
response (`tests/fixtures/met_api_marine_tioman_recorded_2026-10-16.json`) and that guide:
- One value per day per datatype, no issue time, no model, no validity window. The date is a
  calendar day at +08:00. Retrieval time is the only age we know.
- `FMWS` wind speed and `FMWH` wave height are range strings ("10-20" km/h, "0.5 - 1.0" m). The
  range is kept whole and its upper end is exposed as `upper`. That is the top of MET's stated
  range, not a maximum for the site: no gusts, and conditions can exceed it. No midpoint is made.
- `FMWD` wind direction is a code ("VRB") with no stated convention, so it is kept as text.
- Wave height does not say which statistic it is (significant or maximum).
- A waters zone is an area with no coordinates; how far it extends is not stated.
- Values go under `ranges`, never `variables`, so no hourly rule or typed observation reads them.
- The lineage is unknown, so this never corroborates anything (ADR 0010).
- No licence is named, only a liability disclaimer. Limits: 10/min and 2,000/day in the guide,
  3/min and 1,000/day on the public page; the quota guard uses the stricter pair.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import UTC, date, datetime, time, timedelta, timezone

from divesafe.data.base import FetchResult
from divesafe.data.errors import ConnectorResponseError
from divesafe.data.http import JsonGetter
from divesafe.data.parsing import as_dict, bounded_text, clip
from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DiveSite,
    EvidenceItem,
    TransformationStep,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.met.gov.my/v2.1/data"
SOURCE = "met-api-marine-forecast"
SOURCE_VERSION = "v2.1"
MET_MARINE = "met-malaysia:marine-forecast"
MALAYSIA_TIME = timezone(timedelta(hours=8), "MYT")
MAX_DAYS = 8
MAX_RESULTS = 49  # the API pages at 50; a full page may be truncated

# site id -> (location id, location name) of the waters zone. Which zone belongs to which site is
# a fixed, reviewed table; the response is checked against it.
WATERS_ZONES: dict[str, tuple[str, str]] = {
    "my-pahang-pulau-tioman": ("LOCATION:499", "TIOMAN"),
}

# datatype -> (category, name in `ranges`, expected unit)
_RANGES: dict[str, tuple[DataCategory, str, str]] = {
    "FMWS": (DataCategory.WIND, "wind_speed", "km/h"),
    "FMWH": (DataCategory.WAVES_SWELL, "wave_height", "m"),
}
_DIRECTION = "FMWD"
_NUMBER = r"([0-9]{1,3}(?:\.[0-9]{1,2})?)"
_RANGE = re.compile(rf"{_NUMBER}[ ]?(?:-[ ]?{_NUMBER})?")
# Compass points and "VRB" (variable). "VRB" is from the recorded response; the compass points are
# the usual abbreviations and are an assumption until the guide states the convention.
_DIRECTION_CODES = frozenset(
    {
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
        "VRB",
    }
)
# Data-sanity ceilings for parsing only. Not safety limits.
_SANITY_MAX = {"km/h": 300.0, "m": 30.0}


def parse_range(value: object, unit: str, label: str) -> tuple[float, float]:
    """ "10-20" -> (10.0, 20.0); "1.5" -> (1.5, 1.5). Anything else is rejected, not guessed."""
    if not isinstance(value, str):
        raise ConnectorResponseError(f"{label} is not a range string")
    match = _RANGE.fullmatch(value.strip(" "))
    if match is None:
        raise ConnectorResponseError(f"{label} is not a number or a range of numbers")
    low = float(match.group(1))
    high = float(match.group(2)) if match.group(2) is not None else low
    if not (math.isfinite(low) and math.isfinite(high)) or low > high:
        raise ConnectorResponseError(f"{label} is not a valid range")
    if high > _SANITY_MAX[unit]:
        raise ConnectorResponseError(f"{label} is outside the plausible range")
    return low, high


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise ConnectorResponseError("date is missing or not text")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConnectorResponseError("date is not a valid timestamp") from exc
    if moment.utcoffset() != timedelta(hours=8) or moment.timetz().replace(tzinfo=None) != time.min:
        raise ConnectorResponseError("date is not midnight at +08:00")
    return moment.date()


class MetApiMarineConnector:
    name = SOURCE

    def __init__(self, getter: JsonGetter) -> None:
        self._getter = getter

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        zone = WATERS_ZONES.get(site.id)
        if zone is None:
            raise ConnectorResponseError("no waters zone is registered for this site")
        try:
            first, last = _days(window_start, window_end)
            today = now.astimezone(MALAYSIA_TIME).date()
        except OverflowError:
            raise ConnectorResponseError("the window is outside the supported dates") from None
        if (last - first).days >= MAX_DAYS:
            raise ConnectorResponseError("the window is longer than the forecast can cover")
        if first < today:
            raise ConnectorResponseError(
                "the forecast has no history; the window starts before today"
            )
        if (last - today).days >= MAX_DAYS:
            raise ConnectorResponseError("the window starts beyond the days the forecast covers")
        params = {
            "datasetid": "FORECAST",
            "datacategoryid": "MARINE",
            "locationid": zone[0],
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
        }
        payload = await self._getter.get_json(ENDPOINT, params)
        try:
            return self.parse(payload, site, window_start, window_end, now)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ConnectorResponseError(f"unusable response: {clip(exc)}") from exc

    def parse(
        self,
        payload: object,
        site: DiveSite,
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> FetchResult:
        zone = WATERS_ZONES.get(site.id)
        if zone is None:
            raise ConnectorResponseError("no waters zone is registered for this site")
        body = as_dict(payload, "response")
        results = body.get("results")
        if not isinstance(results, list):
            raise ConnectorResponseError("results is missing or not a list")
        if len(results) > MAX_RESULTS:
            raise ConnectorResponseError("response may be truncated; refusing to use it")

        by_day: dict[date, dict[str, dict[str, object]]] = {}
        for entry in results:
            raw = as_dict(entry, "result")
            if (raw.get("locationid"), raw.get("locationname")) != zone:
                raise ConnectorResponseError("a result is for a different waters zone")
            datatype = raw.get("datatype")
            if not isinstance(datatype, str):
                raise ConnectorResponseError("datatype is missing or not text")
            if datatype not in _RANGES and datatype != _DIRECTION:
                continue
            day = _day(raw.get("date"))
            slot = by_day.setdefault(day, {})
            if datatype in slot:
                raise ConnectorResponseError(f"two {datatype} values for the same day")
            slot[datatype] = raw

        first, last = _days(window_start, window_end)
        needed = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        if len(needed) > MAX_DAYS:
            raise ConnectorResponseError("the window is longer than the forecast can cover")

        items: list[EvidenceItem] = []
        for day in needed:
            slot = by_day.get(day, {})
            absent = [t for t in (*_RANGES, _DIRECTION) if t not in slot]
            if absent:
                raise ConnectorResponseError(
                    f"the forecast does not cover the whole window (no {absent[0]} for {day})"
                )
            direction = bounded_text(slot[_DIRECTION].get("value"), _DIRECTION)
            if direction not in _DIRECTION_CODES:
                raise ConnectorResponseError("wind direction is not a known code")
            parsed: dict[str, tuple[float, float]] = {}
            for datatype, (_, _name, unit) in _RANGES.items():
                raw = slot[datatype]
                attributes = as_dict(raw.get("attributes"), "attributes")
                if attributes.get("unit") != unit:
                    raise ConnectorResponseError(
                        f"unexpected unit for {datatype}: {clip(attributes.get('unit'), 20)} "
                        f"(expected {unit})"
                    )
                parsed[datatype] = parse_range(raw.get("value"), unit, datatype)
            start = datetime.combine(day, time.min, tzinfo=MALAYSIA_TIME)
            for datatype, (category, name, unit) in _RANGES.items():
                low, high = parsed[datatype]
                digest = hashlib.sha256(
                    f"{site.id}|{day}|{datatype}|{low}|{high}|{direction}".encode()
                ).hexdigest()[:16]
                ranges: dict[str, object] = {
                    name: {"low": low, "high": high, "unit": unit, "upper": high}
                }
                value: dict[str, object] = {
                    "ranges": ranges,
                    "date": day.isoformat(),
                    "location_id": zone[0],
                    "location_name": zone[1],
                    "data_type": "forecast",
                    "spatial_scope": "MET waters zone (extent not stated), not the dive site",
                    "temporal_scope": "one value for the whole calendar day; no issue time given",
                }
                if category is DataCategory.WIND:
                    value["wind_direction_code"] = direction
                items.append(
                    EvidenceItem(
                        id=f"{SOURCE}:{category.value}:{site.id}:{day.isoformat()}:{digest}",
                        category=category,
                        source=SOURCE,
                        source_version=SOURCE_VERSION,
                        retrieved_at=now,
                        valid_at=start.astimezone(UTC),
                        valid_until=(start + timedelta(days=1)).astimezone(UTC),
                        is_forecast=True,
                        data_kind=DataKind.FORECAST,
                        upstream=(MET_MARINE,),
                        upstream_known=False,
                        quality=DataQuality.DEGRADED,
                        quality_notes=(
                            "daily range for a waters zone; no issue time, so its age is unknown",
                            "the producing model is not reported, so the lineage is unknown",
                            "upper is the top of the stated range, not a site maximum",
                            "wave height does not say whether it is significant or maximum",
                            "no licence is named by the provider, only a liability disclaimer",
                        ),
                        transformations=(
                            TransformationStep(
                                step="timezone",
                                detail="calendar date read as a +08:00 day and converted to UTC",
                            ),
                            TransformationStep(
                                step="range parse",
                                detail=f"{datatype} range string split into low and high ({unit})",
                            ),
                        ),
                        value=value,
                    )
                )
        logger.info("marine forecast parsed", extra={"count": len(items), "site": site.id})
        return FetchResult(tuple(items))


def _days(window_start: datetime, window_end: datetime) -> tuple[date, date]:
    first = window_start.astimezone(MALAYSIA_TIME).date()
    last = (window_end.astimezone(MALAYSIA_TIME) - timedelta(microseconds=1)).date()
    return first, last
