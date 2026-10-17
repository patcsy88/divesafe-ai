"""Tide predictions from the Pusat Hidrografi Nasional (PHN) e-Tide endpoint (degraded evidence).

Design and the open questions: docs/adr/0021-phn-etide-tide-predictions.md. Facts, from one real
response (`tests/fixtures/phn_etide_tioman_recorded_2026-10-17.json`, trimmed to one day):
- The endpoint is undocumented. The body is JSON labelled `text/html`. Three blocks are returned,
  named `current_day`, `prev_day` and `prev_1_day`; the last two were the NEXT two days, so block
  names are never trusted and each block's own date is checked.
- Each day has 144 heights on a 10-minute grid and PHN's own list of highs and lows. The list is
  used (and cross-checked against the series), never derived.
- Heights are metres above chart datum (LAT) per a note on the e-Tide page, not endpoint docs.
- The time zone is inferred (+08:00) and checked only against our clock via `current_day`.
- A tide height or time says nothing about current speed or slack water.
- Values go under `tide`, never `variables` or `ranges`, so no hourly rule or typed observation
  reads them. The producing method is not stated, so this never corroborates anything (ADR 0010).
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
from divesafe.data.parsing import as_dict, clip
from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DiveSite,
    EvidenceItem,
    TransformationStep,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://etide.hydro.gov.my/functions/q_functions.php"
SOURCE = "phn-etide-prediction"
SOURCE_VERSION = "undocumented"
PHN_LINEAGE = "phn:etide-tide-prediction"
ATTRIBUTION = "Pusat Hidrografi Nasional (e-Tide)"
MALAYSIA_TIME = timezone(timedelta(hours=8), "MYT")
HORIZON_DAYS = 3  # today and the next two: what the endpoint returned (ADR 0021)
SLOTS = 144  # 10-minute grid over one day
SLOT_MINUTES = 10
MAX_EXTREMES = 6
CLOCK_TOLERANCE_MINUTES = 30  # plausibility bound on the inferred time zone, not a safety limit
# Data-sanity bounds for parsing only. Not safety limits.
_HEIGHT_MIN, _HEIGHT_MAX = -2.0, 15.0

# site id -> PHN location id. Which location belongs to which site is a fixed, reviewed table.
LOCATIONS: dict[str, str] = {"my-pahang-pulau-tioman": "pulau_tioman"}

_BLOCKS = ("current_day", "prev_day", "prev_1_day")  # offsets 0, 1, 2 days; dates are verified
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_ROW_DATE = re.compile(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{2})")
_BLOCK_DATE = re.compile(r"([0-9]{1,2}) ([A-Za-z]+) ([0-9]{4})")

_DATUM = (
    "Lowest Astronomical Tide / chart datum, per a note on the e-Tide page (not endpoint "
    "documentation)"
)
_NOT_A_CURRENT = (
    "a tide height and time only: it gives no current speed, direction or slack-water time"
)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConnectorResponseError(f"{label} is not a number")
    number = float(value)
    if not math.isfinite(number) or not _HEIGHT_MIN <= number <= _HEIGHT_MAX:
        raise ConnectorResponseError(f"{label} is outside the plausible range")
    return number


def _minutes(value: object, label: str) -> int:
    if not isinstance(value, str):
        raise ConnectorResponseError(f"{label} is not text")
    match = _TIME.fullmatch(value)
    if match is None:
        raise ConnectorResponseError(f"{label} is not a HH:MM time")
    return int(match.group(1)) * 60 + int(match.group(2))


def _build_date(day: str, month: int | None, year: int, label: str) -> date:
    if month is None:
        raise ConnectorResponseError(f"{label} has an unknown month")
    try:
        return date(year, month, int(day))
    except ValueError as exc:
        raise ConnectorResponseError(f"{label} is not a real date") from exc


def _row_date(value: object) -> date:
    if not isinstance(value, str):
        raise ConnectorResponseError("a row date is not text")
    match = _ROW_DATE.fullmatch(value)
    if match is None:
        raise ConnectorResponseError("a row date is not DD-Mon-YY")
    abbreviations = [m[:3] for m in _MONTHS]
    month = abbreviations.index(match.group(2)) + 1 if match.group(2) in abbreviations else None
    return _build_date(match.group(1), month, 2000 + int(match.group(3)), "a row date")


def _block_date(value: object) -> date:
    if not isinstance(value, str):
        raise ConnectorResponseError("a block date is not text")
    match = _BLOCK_DATE.fullmatch(value)
    if match is None:
        raise ConnectorResponseError("a block date is not 'D Month YYYY'")
    month = _MONTHS.index(match.group(2)) + 1 if match.group(2) in _MONTHS else None
    return _build_date(match.group(1), month, int(match.group(3)), "a block date")


def _series(block: dict[str, object], day: date) -> list[float]:
    rows = block.get("data")
    if not isinstance(rows, list) or len(rows) != SLOTS:
        raise ConnectorResponseError(f"a day does not have exactly {SLOTS} heights")
    heights: list[float] = []
    for index, raw in enumerate(rows):
        row = as_dict(raw, "height row")
        expected = index * SLOT_MINUTES
        if _minutes(row.get("time"), "a row time") != expected:
            raise ConnectorResponseError("heights are not on the 10-minute grid")
        if _row_date(row.get("date")) != day:
            raise ConnectorResponseError("a row is for a different day than its block")
        heights.append(_number(row.get("height"), "a height"))
    return heights


def _extremes(block: dict[str, object], heights: list[float], day: date) -> list[dict[str, object]]:
    low_high = as_dict(block.get("MAX_LOW_Tide"), "MAX_LOW_Tide")
    listed = low_high.get("HL")
    if not isinstance(listed, list) or not 1 <= len(listed) <= MAX_EXTREMES:
        raise ConnectorResponseError("the list of highs and lows is missing or the wrong size")
    out: list[dict[str, object]] = []
    slots: list[tuple[int, int]] = []
    previous_minute = -1
    previous_stat: int | None = None
    for raw in listed:
        entry = as_dict(raw, "high/low entry")
        stat = entry.get("stat")
        if type(stat) is not int or stat not in (0, 1):
            raise ConnectorResponseError("a high/low entry is neither high nor low")
        if _row_date(entry.get("date")) != day:
            raise ConnectorResponseError("a high/low entry is for a different day")
        minute = _minutes(entry.get("time"), "a high/low time")
        if minute <= previous_minute:
            raise ConnectorResponseError("highs and lows are not in time order")
        if stat == previous_stat:
            raise ConnectorResponseError("highs and lows do not alternate")
        if minute % SLOT_MINUTES != 0:
            raise ConnectorResponseError("a high/low time is not on the 10-minute grid")
        height = _number(entry.get("height"), "a high/low height")
        slot = minute // SLOT_MINUTES
        near = heights[max(0, slot - 3) : slot + 4]
        extreme = max(near) if stat == 1 else min(near)
        if not math.isclose(heights[slot], height, abs_tol=1e-9) or heights[slot] != extreme:
            raise ConnectorResponseError("a high/low entry does not match the height series")
        previous_minute, previous_stat = minute, stat
        slots.append((slot, stat))
        moment = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=MALAYSIA_TIME)
        out.append(
            {
                "time": moment.isoformat(),
                "kind": "high" if stat == 1 else "low",
                "height_m": height,
            }
        )
    _require_no_unlisted_turn(heights, slots)
    return out


def _require_no_unlisted_turn(heights: list[float], slots: list[tuple[int, int]]) -> None:
    """Between listed extremes the series must run one way, so a dropped high or low is refused
    instead of passing as a complete day. No tolerance is applied."""
    edges = [(0, None), *slots, (SLOTS - 1, None)]
    for (start, start_stat), (end, end_stat) in zip(edges, edges[1:], strict=False):
        rising = end_stat == 1 if end_stat is not None else start_stat == 0
        steps = [
            b - a for a, b in zip(heights[start:end], heights[start + 1 : end + 1], strict=True)
        ]
        if not all(s >= 0 for s in steps) if rising else not all(s <= 0 for s in steps):
            raise ConnectorResponseError(
                "the series turns where no high or low is listed; the list may be incomplete"
            )


def _check_clock(current: dict[str, object], now: datetime) -> None:
    """The one runtime check on the inferred +08:00: PHN's 'now' time must be near our clock."""
    low_high = as_dict(current.get("MAX_LOW_Tide"), "MAX_LOW_Tide")
    status = as_dict(low_high.get("C_status"), "C_status")
    theirs = _minutes(status.get("time"), "C_status time")
    local = now.astimezone(MALAYSIA_TIME)
    ours = local.hour * 60 + local.minute
    gap = abs(theirs - ours)
    if min(gap, 24 * 60 - gap) > CLOCK_TOLERANCE_MINUTES:
        raise ConnectorResponseError("the server's clock does not match ours; refusing the data")


class PhnEtideConnector:
    name = SOURCE

    def __init__(self, getter: JsonGetter) -> None:
        self._getter = getter

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        location = LOCATIONS.get(site.id)
        if location is None:
            raise ConnectorResponseError("no tide location is registered for this site")
        try:
            first, last = _days(window_start, window_end)
            today = now.astimezone(MALAYSIA_TIME).date()
        except OverflowError:
            raise ConnectorResponseError("the window is outside the supported dates") from None
        _check_horizon(first, last, today)
        payload = await self._getter.get_json(ENDPOINT, {"fetch_data": "", "location": location})
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
        location = LOCATIONS.get(site.id)
        if location is None:
            raise ConnectorResponseError("no tide location is registered for this site")
        try:
            first, last = _days(window_start, window_end)
            today = now.astimezone(MALAYSIA_TIME).date()
        except OverflowError:
            raise ConnectorResponseError("the window is outside the supported dates") from None
        _check_horizon(first, last, today)
        body = as_dict(payload, "response")
        _check_clock(as_dict(body.get("current_day"), "current_day"), now)

        items: list[EvidenceItem] = []
        previous_kind: object = None
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            name = _BLOCKS[(day - today).days]
            block = as_dict(body.get(name), name)
            if _block_date(block.get("date")) != day:
                raise ConnectorResponseError(f"{name} is not for the day it should be")
            heights = _series(block, day)
            extremes = _extremes(block, heights, day)
            if previous_kind is not None and extremes[0]["kind"] == previous_kind:
                raise ConnectorResponseError("highs and lows do not alternate across midnight")
            previous_kind = extremes[-1]["kind"]
            start = datetime.combine(day, time.min, tzinfo=MALAYSIA_TIME)
            summary = [(e["time"], e["kind"], e["height_m"]) for e in extremes]
            digest = hashlib.sha256(f"{site.id}|{day}|{summary}".encode()).hexdigest()[:16]
            items.append(
                EvidenceItem(
                    id=f"{SOURCE}:{DataCategory.TIDES.value}:{site.id}:{day.isoformat()}:{digest}",
                    category=DataCategory.TIDES,
                    source=SOURCE,
                    source_version=SOURCE_VERSION,
                    retrieved_at=now,
                    valid_at=start.astimezone(UTC),
                    valid_until=(start + timedelta(days=1)).astimezone(UTC),
                    is_forecast=True,
                    data_kind=DataKind.PREDICTION,
                    upstream=(PHN_LINEAGE,),
                    upstream_known=False,
                    quality=DataQuality.DEGRADED,
                    quality_notes=(
                        "tide height and time of high and low water only: no current speed, "
                        "direction or slack-water time",
                        "from Pusat Hidrografi Nasional's e-Tide service; the producing method "
                        "is not stated, so the lineage is unknown",
                        "heights are above chart datum (LAT) per a note on the e-Tide page, "
                        "not endpoint documentation",
                        "the +08:00 time zone is inferred and checked only against our clock",
                        "a prediction for one PHN station, not for the dive site",
                        "the response does not name its station; that it is for "
                        f"{location} is assumed from the request",
                        "an extreme at the very start or end of a day, or on a flat stretch, is "
                        "checked against one side or an equal-height stretch only; its time is "
                        "PHN's choice within that stretch",
                        "retrieval time may be up to the response-cache lifetime older than shown",
                        "use is by the owner's report of PHN's permission; its terms are not "
                        "on file",
                    ),
                    transformations=(
                        TransformationStep(
                            step="timezone",
                            detail="day and times read as +08:00 and converted to UTC for validity",
                        ),
                        TransformationStep(
                            step="extremes",
                            detail="PHN's own high/low list kept after a cross-check against the "
                            "10-minute series; the series itself is dropped",
                        ),
                    ),
                    value={
                        "tide": {"datum": _DATUM, "unit": "m", "extremes": extremes},
                        "date": day.isoformat(),
                        "requested_location_id": location,
                        "data_type": "prediction",
                        "spatial_scope": (
                            f"assumed to be PHN's {location} station (from the request; the "
                            "response does not name a station); not the dive site"
                        ),
                        "temporal_scope": "one calendar day (+08:00)",
                        "not_a_current": _NOT_A_CURRENT,
                        "attribution": ATTRIBUTION,
                    },
                )
            )
        logger.info("tide prediction parsed", extra={"count": len(items), "site": site.id})
        return FetchResult(tuple(items))


def _days(window_start: datetime, window_end: datetime) -> tuple[date, date]:
    first = window_start.astimezone(MALAYSIA_TIME).date()
    last = (window_end.astimezone(MALAYSIA_TIME) - timedelta(microseconds=1)).date()
    return first, last


def _check_horizon(first: date, last: date, today: date) -> None:
    if first < today:
        raise ConnectorResponseError(
            "the prediction has no history; the window starts before today"
        )
    if (last - today).days >= HORIZON_DAYS:
        raise ConnectorResponseError("the window ends beyond the days the prediction covers")
