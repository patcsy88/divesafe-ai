"""Tide predictions from the tides.today API V2 (degraded evidence; off by default).

Design, terms and open questions: docs/adr/0029-tides-today-tide-predictions.md (Amendment 1).
Facts, from one request per endpoint made by the owner on 2026-11-17 (kept outside the
repository; the committed fixture is SYNTHETIC with the same shape):
- Times are UTC with a "Z". `tide-extremes` lists `{id, dateTime, type, heightM, heightF,
  estimated}`; `ephemeris` gives one row per local day (`date` "YYYY-MM-DD"). Both `$btw` date
  filters are inclusive, and `meta.params` echoes the request.
- Heights are metres as named by the API field `heightM` (the V2 spec documents no unit). The
  datum comes from the location's `source.datumType`; only chart datum (`CD`) is accepted, and
  its level for this point is not stated. The upstream must be `tpe` (tides.today's own engine
  on the FES models).
- `estimated` is not documented, so an estimated extreme is refused.
- A tide height or time says nothing about current speed or slack water.
- Values go under `tide` and `daylight`, never `variables` or `ranges`, so no hourly rule or
  typed observation reads them. The lineage is not independently verified, so this never
  corroborates anything (ADR 0010).
- Sun times and the moon phase sit inside the tide item: if any of the three calls fails there is
  no item at all, so the category is never present on partial data.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

from divesafe.data.base import FetchResult
from divesafe.data.errors import ConnectorResponseError
from divesafe.data.http import JsonGetter
from divesafe.data.parsing import as_dict
from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DiveSite,
    EvidenceItem,
    GeoPoint,
    TransformationStep,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tidestoday.io/v2"
SOURCE = "tides-today-prediction"
SOURCE_VERSION = "api-v2"
LINEAGE = "tides-today:tide-prediction"
MALAYSIA_TIME = timezone(timedelta(hours=8), "MYT")
TIMEZONE_NAME = "Asia/Kuala_Lumpur"
HORIZON_DAYS = 7  # today and the next six local days: our bound, not the provider's (ADR 0029)
# Parsing bounds, not safety limits. A gap longer than this between two extremes means a high or
# low is missing; a real long interval is refused (fails closed).
MAX_GAP = timedelta(hours=18)
MAX_EXTREMES_PER_DAY = 6
MAX_NOTICE_CHARS = 2000
EXTREMES_PER_PAGE = "150"
EPHEMERIS_PER_PAGE = "50"
# Data-sanity bounds for parsing only. Not safety limits.
_HEIGHT_MIN, _HEIGHT_MAX = -2.0, 15.0

ATTRIBUTION = (
    "Tidal data from tides.today (Tides Today). Predictions use the FES2014/FES2022 tide models "
    "(CNES, CLS, LEGOS; AVISO+), ETOPO 2022 bathymetry (NOAA NCEI) and PSMSL sea level data; "
    "these organisations do not endorse it. Full notice in the record (provider_notice)."
)


@dataclass(frozen=True)
class Location:
    id: int
    name: str
    latitude: float
    longitude: float


# site id -> tides.today location. A fixed, reviewed table (ADR 0029 decision 1, option A).
LOCATIONS: dict[str, Location] = {
    "my-pahang-pulau-tioman": Location(2960, "Teluk Tekek", 2.8, 104.133003),
}
DISTANCE_NOTE = "about 3.7 km from the site's reference point"

ACCEPTED_DATUM = "CD"
ACCEPTED_UPSTREAM = "tpe"
_DATUM = "chart datum (CD) as stated by the API; its level for this point is not stated"
_UNIT_BASIS = "metres, as named by the API field heightM (no unit is documented)"
_NOT_A_CURRENT = (
    "a tide height and time only: it gives no current speed, direction or slack-water time"
)
_KINDS = {"high-tide": "high", "low-tide": "low"}
_MOON_PHASES = frozenset(
    {
        "New",
        "Waxing Crescent",
        "First Quarter",
        "Waxing Gibbous",
        "Full",
        "Waning Gibbous",
        "Last Quarter",
        "Waning Crescent",
    }
)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _between(first: date, last: date) -> str:
    return f"$btw:{first.isoformat()},{last.isoformat()}"


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ConnectorResponseError(f"{label} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConnectorResponseError(f"{label} is not a valid timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ConnectorResponseError(f"{label} is not a UTC timestamp")
    return parsed.astimezone(UTC)


def _height(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConnectorResponseError(f"{label} is not a number")
    number = float(value)
    if not math.isfinite(number) or not _HEIGHT_MIN <= number <= _HEIGHT_MAX:
        raise ConnectorResponseError(f"{label} is outside the plausible range")
    return number


def _flag(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConnectorResponseError(f"{label} is not true or false")
    return value


def _check_location(raw: object, expected: Location) -> str:
    """The location response must be for the tabled location, on chart datum, from the
    tides.today engine. Returns the provider's notice text."""
    body = as_dict(raw, "location response")
    location = as_dict(body.get("data"), "location")
    location_id = location.get("id")
    if type(location_id) is not int or location_id != expected.id:
        raise ConnectorResponseError("the response is for a different location")
    if location.get("name") != expected.name:
        raise ConnectorResponseError("the location name differs from the table")
    for key, want in (("lat", expected.latitude), ("lng", expected.longitude)):
        given = location.get(key)
        if not isinstance(given, str) or len(given) > 20:
            raise ConnectorResponseError("the location coordinates are not as expected")
        try:
            number = float(given)
        except ValueError:
            raise ConnectorResponseError("the location coordinates are not as expected") from None
        if number != want:
            raise ConnectorResponseError("the location coordinates differ from the table")
    region = as_dict(location.get("region"), "location region")
    if region.get("timezone") != TIMEZONE_NAME:
        raise ConnectorResponseError("the location's time zone is not Asia/Kuala_Lumpur")
    source = as_dict(location.get("source"), "location source")
    if source.get("datumType") != ACCEPTED_DATUM:
        raise ConnectorResponseError("the datum is missing or is not chart datum (CD)")
    if source.get("source") != ACCEPTED_UPSTREAM:
        raise ConnectorResponseError("the prediction source is not the tides.today engine")
    notice = source.get("copyright")
    if not isinstance(notice, str) or not notice.strip() or len(notice) > MAX_NOTICE_CHARS:
        raise ConnectorResponseError("the location's notice text is missing or too long")
    if not notice.isprintable():
        raise ConnectorResponseError("the location's notice text is not printable")
    return notice.strip()


def _single_page(body: dict[str, object], count: int, label: str) -> None:
    meta = as_dict(body.get("meta"), f"{label} meta")
    pages = meta.get("totalPages")
    total = meta.get("totalItems")
    if type(pages) is not int or pages != 1:
        raise ConnectorResponseError(f"{label} is not a single page")
    if type(total) is not int or total != count:
        raise ConnectorResponseError(f"{label} count does not match its rows")


def _echoed(body: dict[str, object], params: dict[str, str], label: str) -> None:
    meta = as_dict(body.get("meta"), f"{label} meta")
    if as_dict(meta.get("params"), f"{label} params") != params:
        raise ConnectorResponseError(f"{label} is not for the dates requested")


def _extremes(body: dict[str, object], limit: int) -> list[tuple[datetime, str, float]]:
    rows = body.get("items")
    if not isinstance(rows, list) or not 2 <= len(rows) <= limit:
        raise ConnectorResponseError("the list of highs and lows is missing or the wrong size")
    _single_page(body, len(rows), "tide extremes")
    out: list[tuple[datetime, str, float]] = []
    seen: set[int] = set()
    for raw in rows:
        row = as_dict(raw, "high/low entry")
        row_id = row.get("id")
        if type(row_id) is not int or row_id in seen:
            raise ConnectorResponseError("a high/low entry has a missing or repeated id")
        seen.add(row_id)
        if _flag(row.get("estimated"), "estimated") is not False:
            raise ConnectorResponseError("an extreme is marked estimated; its meaning is unknown")
        given = row.get("type")
        kind = _KINDS.get(given) if isinstance(given, str) else None
        if kind is None:
            raise ConnectorResponseError("a high/low entry is neither high nor low")
        moment = _utc(row.get("dateTime"), "a high/low time")
        height = _height(row.get("heightM"), "a high/low height")
        if out:
            previous_moment, previous_kind, previous_height = out[-1]
            if moment <= previous_moment:
                raise ConnectorResponseError("highs and lows are not in time order")
            if kind == previous_kind:
                raise ConnectorResponseError("highs and lows do not alternate")
            if moment - previous_moment > MAX_GAP:
                raise ConnectorResponseError("a high or low appears to be missing")
            higher = height > previous_height
            if (kind == "high") != higher:
                raise ConnectorResponseError("a high is not above the low beside it")
        out.append((moment, kind, height))
    return out


def _ephemeris_rows(
    body: dict[str, object], expected: Location, first: date, last: date
) -> dict[date, dict[str, object]]:
    rows = body.get("items")
    span = (last - first).days + 1
    if not isinstance(rows, list) or len(rows) != span:
        raise ConnectorResponseError("ephemeris has the wrong number of days")
    _single_page(body, len(rows), "ephemeris")
    found: dict[date, dict[str, object]] = {}
    for raw in rows:
        row = as_dict(raw, "ephemeris row")
        location_id = row.get("locationId")
        if type(location_id) is not int or location_id != expected.id:
            raise ConnectorResponseError("an ephemeris row is for a different location")
        given = row.get("date")
        if not isinstance(given, str) or _DAY.fullmatch(given) is None:
            raise ConnectorResponseError("an ephemeris date is not a local day")
        try:
            day = date.fromisoformat(given)
        except ValueError:
            raise ConnectorResponseError("an ephemeris date is not a local day") from None
        if day in found:
            raise ConnectorResponseError("ephemeris repeats a day")
        found[day] = row
    for offset in range(span):
        if first + timedelta(days=offset) not in found:
            raise ConnectorResponseError("ephemeris is missing a needed day")
    return found


def _daylight(row: dict[str, object], day: date) -> dict[str, str]:
    sunrise = _utc(row.get("sunrise"), "sunrise").astimezone(MALAYSIA_TIME)
    sunset = _utc(row.get("sunset"), "sunset").astimezone(MALAYSIA_TIME)
    if sunrise.date() != day or sunset.date() != day or not sunrise < sunset:
        raise ConnectorResponseError("sunrise and sunset do not fit their day")
    return {"sunrise": sunrise.isoformat(), "sunset": sunset.isoformat()}


def _moon_phase(row: dict[str, object]) -> dict[str, object]:
    name = row.get("moonPhaseName")
    if not isinstance(name, str) or name not in _MOON_PHASES:
        raise ConnectorResponseError("the moon phase name is not one of the documented eight")
    fraction = row.get("moonPhase")
    if isinstance(fraction, bool) or not isinstance(fraction, int | float):
        raise ConnectorResponseError("the moon phase is not a number")
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ConnectorResponseError("the moon phase is outside 0 to 1")
    return {"name": name, "fraction": float(fraction)}


def _days(window_start: datetime, window_end: datetime) -> tuple[date, date]:
    first = window_start.astimezone(MALAYSIA_TIME).date()
    last = (window_end.astimezone(MALAYSIA_TIME) - timedelta(microseconds=1)).date()
    return first, last


def _check_horizon(first: date, last: date, today: date) -> None:
    if first < today:
        raise ConnectorResponseError("the window starts before today")
    if (last - today).days >= HORIZON_DAYS:
        raise ConnectorResponseError("the window ends beyond the days this source is asked for")


class TidesTodayConnector:
    name = SOURCE

    def __init__(self, getter: JsonGetter) -> None:
        self._getter = getter

    @staticmethod
    def requests(location: Location, first: date, last: date) -> list[tuple[str, dict[str, str]]]:
        """The three calls, in order: the location (datum, source, notice), the tide extremes
        (inclusive UTC days, one day either side so every local day is bracketed) and the
        ephemeris (inclusive local days)."""
        return [
            (f"{BASE_URL}/locations/{location.id}", {"locale": "en"}),
            (
                f"{BASE_URL}/locations/{location.id}/tide-extremes",
                {
                    "filter.dateTime": _between(
                        first - timedelta(days=1), last + timedelta(days=1)
                    ),
                    "itemsPerPage": EXTREMES_PER_PAGE,
                },
            ),
            (
                f"{BASE_URL}/locations/{location.id}/ephemeris",
                {"filter.date": _between(first, last), "itemsPerPage": EPHEMERIS_PER_PAGE},
            ),
        ]

    def _plan(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> tuple[Location, date, date]:
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        location = LOCATIONS.get(site.id)
        if location is None:
            raise ConnectorResponseError("no tides.today location is registered for this site")
        try:
            first, last = _days(window_start, window_end)
            today = now.astimezone(MALAYSIA_TIME).date()
            # Probe for OverflowError before any request; parse uses both dates.
            _ = (first - timedelta(days=1), last + timedelta(days=2))
        except OverflowError:
            raise ConnectorResponseError("the window is outside the supported dates") from None
        _check_horizon(first, last, today)
        return location, first, last

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        location, first, last = self._plan(site, window_start, window_end, now)
        (place_url, place_params), (tide_url, tide_params), (sky_url, sky_params) = self.requests(
            location, first, last
        )
        place = await self._getter.get_json(place_url, place_params)
        tides = await self._getter.get_json(tide_url, tide_params)
        sky = await self._getter.get_json(sky_url, sky_params)
        try:
            return self.parse(place, tides, sky, site, window_start, window_end, now)
        except (ValueError, TypeError, OverflowError) as exc:
            # Fixed text: a validation error could quote provider text (security review).
            raise ConnectorResponseError("unusable response: a value did not validate") from exc

    def parse(
        self,
        location_payload: object,
        extremes_payload: object,
        ephemeris_payload: object,
        site: DiveSite,
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> FetchResult:
        location, first, last = self._plan(site, window_start, window_end, now)
        (_, location_params), (_, extremes_params), (_, ephemeris_params) = self.requests(
            location, first, last
        )

        location_body = as_dict(location_payload, "location response")
        notice = _check_location(location_body, location)
        _echoed(location_body, location_params, "location")

        tides = as_dict(extremes_payload, "tide extremes response")
        _echoed(tides, extremes_params, "tide extremes")
        span = (last - first).days + 3
        extremes = _extremes(tides, MAX_EXTREMES_PER_DAY * span)
        asked_from = datetime.combine(first - timedelta(days=1), time.min, tzinfo=UTC)
        asked_to = datetime.combine(last + timedelta(days=2), time.min, tzinfo=UTC)
        if not (asked_from <= extremes[0][0] and extremes[-1][0] < asked_to):
            raise ConnectorResponseError("a high or low is outside the dates requested")

        ephemeris = as_dict(ephemeris_payload, "ephemeris response")
        _echoed(ephemeris, ephemeris_params, "ephemeris")
        sky_rows = _ephemeris_rows(ephemeris, location, first, last)

        items: list[EvidenceItem] = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            start = datetime.combine(day, time.min, tzinfo=MALAYSIA_TIME)
            end = start + timedelta(days=1)
            if not (extremes[0][0] <= start and extremes[-1][0] >= end):
                raise ConnectorResponseError("the highs and lows do not cover the whole day")
            inside = [e for e in extremes if start <= e[0] < end]
            if not inside:
                raise ConnectorResponseError("a day has no high or low")
            listed = [
                {
                    "time": moment.astimezone(MALAYSIA_TIME).isoformat(),
                    "kind": kind,
                    "height_m": height,
                }
                for moment, kind, height in inside
            ]
            daylight = _daylight(sky_rows[day], day)
            moon_phase = _moon_phase(sky_rows[day])
            summary = [(e["time"], e["kind"], e["height_m"]) for e in listed]
            digest = hashlib.sha256(
                f"{site.id}|{day}|{summary}|{daylight}|{moon_phase}".encode()
            ).hexdigest()[:16]
            items.append(
                EvidenceItem(
                    id=f"{SOURCE}:{DataCategory.TIDES.value}:{site.id}:{day.isoformat()}:{digest}",
                    category=DataCategory.TIDES,
                    source=SOURCE,
                    source_version=SOURCE_VERSION,
                    retrieved_at=now,
                    valid_at=start.astimezone(UTC),
                    valid_until=end.astimezone(UTC),
                    is_forecast=True,
                    data_kind=DataKind.PREDICTION,
                    location=GeoPoint(
                        latitude=location.latitude,
                        longitude=location.longitude,
                        description=(
                            f"tides.today location {location.id} '{location.name}', "
                            f"{DISTANCE_NOTE}; not a dive site"
                        ),
                    ),
                    upstream=(LINEAGE,),
                    upstream_known=False,
                    quality=DataQuality.DEGRADED,
                    quality_notes=(
                        "tide height and time of high and low water only: no current speed, "
                        "direction or slack-water time",
                        f"a prediction for tides.today's {location.name} point, "
                        f"{DISTANCE_NOTE}, not for the dive site",
                        "the provider names its own prediction engine on the FES global models "
                        "(source tpe); this is not independently verified, so the lineage is "
                        "treated as unknown",
                        f"heights in {_UNIT_BASIS}; datum: {_DATUM}",
                        "astronomical prediction only: no weather or pressure effects",
                        "the moon phase is astronomical context as given; the API does not "
                        "document what its fraction measures; it is not a spring/neap "
                        "classification and says nothing about current strength",
                        "the provider's list of highs and lows; order, alternation and an 18 h "
                        "gap bound are checked, but a single missing high/low pair can pass",
                        "retrieval time may be up to the response-cache lifetime older than shown",
                        "the provider's terms forbid use as the sole source for diving and for "
                        "safety-critical purposes without written permission (ADR 0029)",
                    ),
                    transformations=(
                        TransformationStep(
                            step="timezone",
                            detail="UTC times converted to +08:00; the day is the local day",
                        ),
                        TransformationStep(
                            step="extremes",
                            detail="the provider's highs and lows within the local day, after "
                            "order, alternation, gap and coverage checks",
                        ),
                        TransformationStep(
                            step="context",
                            detail="sunrise, sunset and moon phase for the day added from the "
                            "ephemeris; moonrise and moonset dropped",
                        ),
                    ),
                    value={
                        "tide": {
                            "datum": _DATUM,
                            "unit": "m",
                            "unit_basis": _UNIT_BASIS,
                            "extremes": listed,
                            "moon_phase": moon_phase,
                        },
                        "daylight": daylight,
                        "date": day.isoformat(),
                        "requested_location_id": location.id,
                        "location_name": location.name,
                        "data_type": "prediction",
                        "spatial_scope": (
                            f"tides.today point {location.id} '{location.name}', "
                            f"{DISTANCE_NOTE}; not the dive site"
                        ),
                        "temporal_scope": "one local day (+08:00)",
                        "not_a_current": _NOT_A_CURRENT,
                        "attribution": ATTRIBUTION,
                        "provider_notice": notice,
                    },
                )
            )
        logger.info(
            "tide prediction parsed",
            extra={"count": len(items), "site": site.id},
        )
        return FetchResult(tuple(items))
