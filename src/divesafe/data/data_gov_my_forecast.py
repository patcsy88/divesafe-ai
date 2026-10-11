"""MET Malaysia 7-day general forecast via the data.gov.my Weather API.

Docs: https://developer.data.gov.my/realtime-api/weather  (checked 2026-10-11)

Facts and limits:
- The docs state "Marine forecast data is currently unavailable". This is the general (land-style)
  forecast: rain, thunderstorm and haze wording per period of the day, and a daily minimum and
  maximum temperature. It has NO wind, wave, swell, current or tide values, so it cannot support
  any of those factors.
- Pulau Tioman is a "Rc" (recreation centre) location, id `Rc024`, name "Pulau Tioman" (live
  response 2026-10-11). Which location belongs to which site is a fixed, reviewed table here; the
  response is checked against it and anything else is rejected.
- Values are in Bahasa Melayu. The live spelling differs from the docs ("Tiada Hujan" and
  "Jerebu" live; "Tiada hujan" and "Berjerebu" in the docs), so matching ignores case and the
  table lists both. A phrase not in the table is kept as received with no translation, and a
  quality note says so; it is never guessed.
- `date` is a calendar day. It is read as a Malaysia-time (UTC+8) day, which is an inference, as
  the docs do not state a timezone.
- The text is untrusted input. `untrusted_text` is a label for readers; the protection is that the
  prompt layer puts the whole `value` of this category inside the untrusted block.
"""

from __future__ import annotations

import hashlib
import logging
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

ENDPOINT = "https://api.data.gov.my/weather/forecast/"
SOURCE = "data-gov-my-weather-forecast"
SOURCE_VERSION = "weather-api"
MET_FORECAST = "met-malaysia:general-forecast"
MALAYSIA_TIME = timezone(timedelta(hours=8), "MYT")
PAGE_LIMIT = 100
MAX_PHRASE = 200
MAX_DAYS = 8
# Data-sanity bounds for a daily air temperature in Celsius. Not a safety limit.
TEMP_RANGE = (-10, 60)

# site id -> (location_id, location_name) in the Weather API.
FORECAST_LOCATIONS: dict[str, tuple[str, str]] = {
    "my-pahang-pulau-tioman": ("Rc024", "Pulau Tioman"),
}

# Meanings from the docs' value table. "Jerebu" is the live spelling of "Berjerebu" (haze); that
# equivalence is an inference from the word, not stated by the docs.
_WORDS = {
    "berjerebu": "Hazy",
    "jerebu": "Hazy",
    "tiada hujan": "No rain",
    "hujan": "Rain",
    "hujan di beberapa tempat": "Scattered rain",
    "hujan di satu dua tempat": "Isolated rain",
    "hujan di satu dua tempat di kawasan pantai": "Isolated rain over coastal areas",
    "hujan di satu dua tempat di kawasan pedalaman": "Isolated rain over inland areas",
    "ribut petir": "Thunderstorms",
    "ribut petir di beberapa tempat": "Scattered thunderstorms",
    "ribut petir di beberapa tempat di kawasan pedalaman": (
        "Scattered thunderstorms over inland areas"
    ),
    "ribut petir di satu dua tempat": "Isolated thunderstorms",
    "ribut petir di satu dua tempat di kawasan pantai": (
        "Isolated thunderstorms over coastal areas"
    ),
    "ribut petir di satu dua tempat di kawasan pedalaman": (
        "Isolated thunderstorms over inland areas"
    ),
}
_WHEN = {
    "pagi": "Morning",
    "malam": "Night",
    "petang": "Afternoon",
    "pagi dan petang": "Morning and afternoon",
    "pagi dan malam": "Morning and night",
    "petang dan malam": "Afternoon and night",
    "sepanjang hari": "Throughout the day",
}
_PERIODS = ("morning_forecast", "afternoon_forecast", "night_forecast", "summary_forecast")
_REQUIRED = ("location", "date", *_PERIODS, "summary_when", "min_temp", "max_temp")


def _phrase(value: object, label: str) -> str:
    text = bounded_text(value, label).strip()
    if not text or len(text) > MAX_PHRASE or any(not c.isprintable() for c in text):
        raise ConnectorResponseError(f"{label} is empty, too long or not printable text")
    return text


def _temperature(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConnectorResponseError(f"{label} is not a whole number")
    if not TEMP_RANGE[0] <= value <= TEMP_RANGE[1]:
        raise ConnectorResponseError(f"{label} is outside the plausible range")
    return value


class DataGovMyForecastConnector:
    name = SOURCE

    def __init__(self, getter: JsonGetter) -> None:
        self._getter = getter

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        location = FORECAST_LOCATIONS.get(site.id)
        if location is None:
            raise ConnectorResponseError("no forecast location is registered for this site")
        params = {"limit": str(PAGE_LIMIT), "contains": f"{location[0]}@location__location_id"}
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
        location = FORECAST_LOCATIONS.get(site.id)
        if location is None:
            raise ConnectorResponseError("no forecast location is registered for this site")
        if not isinstance(payload, list):
            raise ConnectorResponseError("response is not a list of forecasts")
        if len(payload) >= PAGE_LIMIT:
            raise ConnectorResponseError("response may be truncated; refusing to use it")

        by_day: dict[date, dict[str, object]] = {}
        for entry in payload:
            raw = as_dict(entry, "forecast")
            if any(k not in raw for k in _REQUIRED):
                raise ConnectorResponseError("a forecast is missing required fields")
            where = as_dict(raw["location"], "location")
            if (where.get("location_id"), where.get("location_name")) != location:
                raise ConnectorResponseError("a forecast is for a different location")
            if not isinstance(raw["date"], str):
                raise ConnectorResponseError("date is missing or not text")
            try:
                day = date.fromisoformat(raw["date"])
            except ValueError as exc:
                raise ConnectorResponseError("date is not a valid calendar date") from exc
            if day in by_day:
                raise ConnectorResponseError("two forecasts for the same day")
            by_day[day] = raw

        first = window_start.astimezone(MALAYSIA_TIME).date()
        last = (window_end.astimezone(MALAYSIA_TIME) - timedelta(microseconds=1)).date()
        if (last - first).days >= MAX_DAYS:
            raise ConnectorResponseError("the window is longer than the forecast can cover")
        needed = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        missing = [d for d in needed if d not in by_day]
        if missing:
            raise ConnectorResponseError(
                f"the forecast does not cover the whole window (no entry for {missing[0]})"
            )

        items: list[EvidenceItem] = []
        for day in needed:
            raw = by_day[day]
            phrases = {k: _phrase(raw[k], k) for k in _PERIODS}
            when = _phrase(raw["summary_when"], "summary_when")
            low = _temperature(raw["min_temp"], "min_temp")
            high = _temperature(raw["max_temp"], "max_temp")
            if low > high:
                raise ConnectorResponseError("min_temp is above max_temp")

            english = {k: _WORDS.get(v.casefold()) for k, v in phrases.items()}
            english_when = _WHEN.get(when.casefold())
            untranslated = sorted(
                {k for k, v in english.items() if v is None}
                | ({"summary_when"} if english_when is None else set())
            )
            start = datetime.combine(day, time.min, tzinfo=MALAYSIA_TIME)
            end = start + timedelta(days=1)
            digest = hashlib.sha256(
                "|".join(
                    [site.id, day.isoformat(), *phrases.values(), when, str(low), str(high)]
                ).encode()
            ).hexdigest()[:16]
            notes = [
                "general forecast for the island, not marine and not site-level",
                "no wind, wave, swell, current or tide values",
                "the date is read as a Malaysia-time (UTC+08:00) day; the source states no zone",
            ]
            if untranslated:
                notes.append(
                    "phrase not in the reviewed table, kept as received: " + ", ".join(untranslated)
                )
            items.append(
                EvidenceItem(
                    id=f"{SOURCE}:{site.id}:{day.isoformat()}:{digest}",
                    category=DataCategory.WEATHER,
                    source=SOURCE,
                    source_version=SOURCE_VERSION,
                    retrieved_at=now,
                    valid_at=start.astimezone(UTC),
                    valid_until=end.astimezone(UTC),
                    is_forecast=True,
                    data_kind=DataKind.FORECAST,
                    upstream=(MET_FORECAST,),
                    upstream_known=False,
                    quality=DataQuality.DEGRADED,
                    quality_notes=tuple(notes),
                    transformations=(
                        TransformationStep(
                            step="timezone",
                            detail="calendar date read as a UTC+08:00 day and converted to UTC",
                        ),
                        TransformationStep(
                            step="translation",
                            detail="Bahasa Melayu phrases matched to a reviewed table; "
                            "unmatched phrases kept as received",
                        ),
                    ),
                    value={
                        "location_id": location[0],
                        "location_name": location[1],
                        "date": day.isoformat(),
                        "periods": {
                            k.removesuffix("_forecast"): {"ms": phrases[k], "en": english[k]}
                            for k in _PERIODS
                        },
                        "summary_when": {"ms": when, "en": english_when},
                        "min_temp_c": low,
                        "max_temp_c": high,
                        "untrusted_text": ["periods", "summary_when"],
                        "scope": (
                            "island-level general forecast; not marine; not the dive site; "
                            "no wind, wave, swell, current or tide"
                        ),
                    },
                )
            )
        logger.info("forecast parsed", extra={"count": len(items), "site": site.id})
        return FetchResult(tuple(items))
