"""MET Malaysia marine/weather warnings via the data.gov.my Weather API.

Docs: https://developer.data.gov.my/realtime-api/weather  (rate limit: 4 requests/minute)

Facts and limits (verified 2026-10-09 against the docs and live responses):
- Warnings are free text. One warning can cover several regions in separate paragraphs, and the
  API has no structured location field. This connector therefore does NOT decide whether a
  warning applies to a site. Every warning overlapping the dive window is returned in full for a
  human to read. `matched_area_names` is an informational substring match only; an empty match
  does not mean the warning is irrelevant. The text is untrusted input (see `untrusted_text`).
- Timestamps carry no timezone in the docs. They are inferred to be Malaysia time (UTC+8): a
  warning issued "2026-10-09T01:00" was served at 01:11 Malaysia time. Because that is only an
  inference, a warning is included if it overlaps the window under EITHER UTC+8 or UTC.
  Timestamps that already carry an offset are rejected.
- Some entries are advisories with no validity window (both `valid_from` and `valid_to` null),
  e.g. a "No Advisory" tropical-cyclone monitoring notice. They are returned in full as
  `advisory_without_validity` and are never evidence that no warning is active. An entry with
  only one of the two times null is malformed and rejected.
- `no_active_warnings` means only "this feed returned no warning issued within the lookback that
  overlaps the window". It is not a marine forecast and not proof of calm.
- The API has no marine forecast; only warnings.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta, timezone

from divesafe.data.base import FetchResult
from divesafe.data.errors import ConnectorResponseError
from divesafe.data.http import JsonGetter
from divesafe.data.parsing import as_dict, bounded_text, clip, parse_naive
from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DiveSite,
    EvidenceItem,
    TransformationStep,
)

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.data.gov.my/weather/warning/"
SOURCE = "data-gov-my-weather-warning"
SOURCE_VERSION = "weather-api"
MALAYSIA_TIME = timezone(timedelta(hours=8), "MYT")
PAGE_LIMIT = 100
LOOKBACK = timedelta(days=30)
_REQUIRED = ("warning_issue", "valid_from", "valid_to", "heading_en", "text_en")


class DataGovMyWarningConnector:
    name = SOURCE

    def __init__(self, getter: JsonGetter) -> None:
        self._getter = getter

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        if window_end <= window_start:
            raise ValueError("window_end must be after window_start")
        since = (window_start - LOOKBACK).astimezone(MALAYSIA_TIME)
        params = {
            "limit": str(PAGE_LIMIT),
            "timestamp_start": f"{since:%Y-%m-%d %H:%M:%S}@warning_issue__issued",
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
        if not isinstance(payload, list):
            raise ConnectorResponseError("response is not a list of warnings")
        if len(payload) >= PAGE_LIMIT:
            raise ConnectorResponseError("response may be truncated; refusing to use it")

        items: list[EvidenceItem] = []
        dated = 0
        for entry in payload:
            raw = as_dict(entry, "warning")
            if any(k not in raw for k in _REQUIRED):
                raise ConnectorResponseError("a warning is missing required fields")
            issue = as_dict(raw["warning_issue"], "warning_issue")
            issued = parse_naive(issue.get("issued"), "issued").replace(tzinfo=MALAYSIA_TIME)
            text = bounded_text(raw["text_en"], "text_en")
            undated = raw["valid_from"] is None and raw["valid_to"] is None

            valid_from = valid_to = None
            if not undated:
                naive_from = parse_naive(raw["valid_from"], "valid_from")
                naive_to = parse_naive(raw["valid_to"], "valid_to")
                if naive_to < naive_from:
                    raise ConnectorResponseError("warning valid_to is before valid_from")
                valid_from = naive_from.replace(tzinfo=MALAYSIA_TIME)
                valid_to = naive_to.replace(tzinfo=MALAYSIA_TIME)
                overlaps = any(
                    naive_from.replace(tzinfo=tz) <= window_end
                    and naive_to.replace(tzinfo=tz) >= window_start
                    for tz in (MALAYSIA_TIME, UTC)
                )
                if not overlaps:
                    continue
                dated += 1

            heading = bounded_text(raw["heading_en"], "heading_en")
            instruction = bounded_text(raw.get("instruction_en"), "instruction_en")
            digest = hashlib.sha256(
                "|".join(
                    [
                        site.id,
                        issued.isoformat(),
                        valid_from.isoformat() if valid_from else "",
                        valid_to.isoformat() if valid_to else "",
                        heading,
                        text,
                        instruction,
                    ]
                ).encode()
            ).hexdigest()[:16]
            items.append(
                EvidenceItem(
                    id=f"{SOURCE}:{digest}",
                    category=DataCategory.MARINE_WARNINGS,
                    source=SOURCE,
                    source_version=SOURCE_VERSION,
                    retrieved_at=now,
                    valid_at=(valid_from or issued).astimezone(UTC),
                    valid_until=valid_to.astimezone(UTC) if valid_to else None,
                    is_forecast=False,
                    data_kind=DataKind.NOTICE,
                    quality=DataQuality.UNASSESSED,
                    quality_notes=(
                        "free-text, multi-region; applicability to the site is not determined",
                        "timestamps carry no timezone in the source; UTC+08:00 is inferred",
                    ),
                    transformations=(
                        TransformationStep(
                            step="timezone",
                            detail="naive source times read as UTC+08:00 and converted to UTC",
                        ),
                        TransformationStep(step="text bounded", detail="fields length-checked"),
                    ),
                    value={
                        "kind": "advisory_without_validity" if undated else "warning",
                        "title_en": bounded_text(issue.get("title_en"), "title_en"),
                        "heading_en": heading,
                        "text_en": text,
                        "instruction_en": instruction,
                        "untrusted_text": ["title_en", "heading_en", "text_en", "instruction_en"],
                        "issued": issued.astimezone(UTC).isoformat(),
                        "valid_from": valid_from.astimezone(UTC).isoformat()
                        if valid_from
                        else None,
                        "valid_to": valid_to.astimezone(UTC).isoformat() if valid_to else None,
                        "timezone_assumption": (
                            "source times inferred as UTC+08:00; overlap tested under UTC+08:00 "
                            "and UTC, included if either overlaps"
                        ),
                        "applicability": "NOT DETERMINED: free-text, multi-region; human must read",
                        "matched_area_names": [
                            a for a in site.area_names if a.lower() in text.lower()
                        ],
                    },
                )
            )

        if not items:
            items.append(
                EvidenceItem(
                    id=f"{SOURCE}:none:{site.id}:{window_start.astimezone(UTC):%Y%m%dT%H%MZ}",
                    category=DataCategory.MARINE_WARNINGS,
                    source=SOURCE,
                    source_version=SOURCE_VERSION,
                    retrieved_at=now,
                    valid_at=now,
                    valid_until=window_end.astimezone(UTC),
                    is_forecast=False,
                    data_kind=DataKind.NOTICE,
                    quality_notes=("absence of a warning in this feed; not proof of calm",),
                    transformations=(
                        TransformationStep(step="absence", detail="no overlapping dated warning"),
                    ),
                    value={
                        "kind": "no_active_warnings",
                        "meaning": (
                            "this feed returned no warning, issued within the lookback, that "
                            "overlaps the window; not a marine forecast and not proof of calm"
                        ),
                        "lookback_days": LOOKBACK.days,
                        "window_start": window_start.astimezone(UTC).isoformat(),
                        "window_end": window_end.astimezone(UTC).isoformat(),
                    },
                )
            )
        logger.info("warnings parsed", extra={"count": len(items), "dated": dated, "site": site.id})
        return FetchResult(tuple(items))
