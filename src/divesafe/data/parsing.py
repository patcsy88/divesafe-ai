"""Shared helpers for parsing untrusted provider payloads."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from divesafe.data.errors import ConnectorResponseError

MAX_TEXT = 20_000


def clip(value: object, limit: int = 80) -> str:
    """Bounded, printable rendering of provider-controlled text for issues and logs."""
    text = repr(value) if not isinstance(value, str) else value
    text = "".join(c if c.isprintable() else "?" for c in text)
    return text if len(text) <= limit else text[:limit] + "..."


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConnectorResponseError(f"{label} is missing or not an object")
    return value


def parse_naive(value: Any, label: str) -> datetime:
    """Parse a timestamp that must carry no timezone (so no offset is silently overwritten)."""
    if not isinstance(value, str):
        raise ConnectorResponseError(f"{label} is missing or not a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConnectorResponseError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is not None:
        raise ConnectorResponseError(f"{label} unexpectedly carries a timezone")
    return parsed


def bounded_text(value: Any, label: str) -> str:
    if value is not None and not isinstance(value, str):
        raise ConnectorResponseError(f"{label} is not text")
    text = value or ""
    if len(text) > MAX_TEXT:
        raise ConnectorResponseError(f"{label} is longer than {MAX_TEXT} characters")
    return text


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def ceil_hour(moment: datetime) -> datetime:
    floored = floor_hour(moment)
    return floored if floored == moment.astimezone(UTC) else floored + timedelta(hours=1)


def check_grid_cell(latitude: object, longitude: object) -> tuple[float, float]:
    """The provider's grid cell must be real, finite coordinates."""
    ok = (
        not isinstance(latitude, bool)
        and not isinstance(longitude, bool)
        and isinstance(latitude, int | float)
        and isinstance(longitude, int | float)
        and math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )
    if not ok:
        raise ConnectorResponseError("missing or invalid grid cell coordinates")
    return float(latitude), float(longitude)  # type: ignore[arg-type]
