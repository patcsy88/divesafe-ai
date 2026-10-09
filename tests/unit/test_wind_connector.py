"""Wind connector: recorded real response plus labelled synthetic mutations."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from divesafe.data import REDANG_ISLAND, ConnectorResponseError, OpenMeteoWindConnector
from divesafe.data.http import ALLOWED_HOSTS
from divesafe.data.ratelimit import DEFAULT_MIN_INTERVAL_SECONDS
from divesafe.domain import (
    RANGE_CHECK,
    UNIT_CHECK,
    DataCategory,
    DataKind,
    DataQuality,
    effective_quality,
)

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "open_meteo_wind_redang_recorded_2026-10-08.json"
)
WIND = json.loads(FIXTURE.read_text())
NOW = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
START = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
END = datetime(2026, 10, 8, 21, 0, tzinfo=UTC)


class _Getter:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        self.calls.append((url, params))
        return self.payload


def _fetch(payload: Any = WIND, end: datetime = END):  # type: ignore[no-untyped-def]
    getter = _Getter(payload)
    result = asyncio.run(OpenMeteoWindConnector(getter).fetch(REDANG_ISLAND, START, end, NOW))
    return result, getter


def test_one_wind_item_per_hour_and_nothing_else() -> None:
    result, _ = _fetch()
    assert result.issues == ()
    assert len(result.items) == 4  # 18:00 to 21:00 inclusive
    assert {i.category for i in result.items} == {DataCategory.WIND}
    assert DataCategory.TIDES not in {i.category for i in result.items}


def test_values_are_the_recorded_values_in_the_documented_units() -> None:
    result, _ = _fetch()
    item = result.items[0]  # 2026-10-08T18:00Z
    assert item.value["variables"] == {
        "wind_speed_10m": WIND["hourly"]["wind_speed_10m"][1],
        "wind_gusts_10m": WIND["hourly"]["wind_gusts_10m"][1],
        "wind_direction_10m": float(WIND["hourly"]["wind_direction_10m"][1]),
    }
    assert item.value["units"] == {
        "wind_speed_10m": "km/h",
        "wind_gusts_10m": "km/h",
        "wind_direction_10m": "°",
    }


def test_the_request_pins_the_unit_uses_https_and_a_known_host() -> None:
    _, getter = _fetch()
    url, params = getter.calls[0]
    assert url.startswith("https://api.open-meteo.com/")
    assert params["wind_speed_unit"] == "kmh" and params["timezone"] == "UTC"
    assert params["cell_selection"] == "sea"  # a dive site over water must prefer sea cells
    assert set(params["hourly"].split(",")) == {
        "wind_speed_10m",
        "wind_gusts_10m",
        "wind_direction_10m",
    }
    assert "api.open-meteo.com" in ALLOWED_HOSTS
    assert "api.open-meteo.com" in DEFAULT_MIN_INTERVAL_SECONDS


def test_provenance_is_complete_and_states_every_known_limitation() -> None:
    result, _ = _fetch()
    item = result.items[0]
    assert item.source == "open-meteo-wind" and item.source_version == "v1"
    assert item.retrieved_at == NOW and item.valid_at.tzinfo is not None
    assert item.data_kind == DataKind.MODEL and item.is_forecast is True
    assert item.quality == DataQuality.DEGRADED and effective_quality(item) == DataQuality.DEGRADED
    notes = " | ".join(item.quality_notes)
    assert "producing model is not reported" in notes
    assert "maximum of the preceding hour" in notes
    assert "convention not verified" in notes
    assert item.location is not None and item.value["grid_distance_km"] > 0
    assert item.value["variable_semantics"]["wind_gusts_10m"] == "maximum of the preceding hour"
    assert "Open-Meteo" in item.value["attribution"]
    assert {t.step for t in item.transformations} >= {UNIT_CHECK, RANGE_CHECK, "grid snap"}


def test_hours_already_past_are_still_model_values_not_observations() -> None:
    getter = _Getter(WIND)
    late = datetime(2026, 10, 8, 23, 59, tzinfo=UTC)
    result = asyncio.run(OpenMeteoWindConnector(getter).fetch(REDANG_ISLAND, START, END, late))
    assert result.items and all(
        i.is_forecast and i.data_kind == DataKind.MODEL for i in result.items
    )


def test_window_end_is_rounded_up_to_cover_a_partial_hour() -> None:
    result, _ = _fetch(end=datetime(2026, 10, 8, 20, 30, tzinfo=UTC))
    assert {i.valid_at.hour for i in result.items} == {18, 19, 20, 21}


def test_ids_are_unique_and_deterministic() -> None:
    first, _ = _fetch()
    second, _ = _fetch()
    ids = [i.id for i in first.items]
    assert len(ids) == len(set(ids)) and ids == [i.id for i in second.items]


# --- synthetic mutations: every problem drops the whole category and reports it ----------------


@pytest.mark.parametrize(
    ("variable", "index", "value", "why"),
    [
        ("wind_speed_10m", 2, None, "null"),
        ("wind_gusts_10m", 1, None, "null gust"),
        ("wind_speed_10m", 1, -3.0, "negative speed"),
        ("wind_gusts_10m", 1, -0.1, "negative gust"),
        ("wind_direction_10m", 1, 361, "direction above 360"),
        ("wind_direction_10m", 1, -1, "direction below 0"),
        ("wind_speed_10m", 1, "9.4", "string value"),
        ("wind_speed_10m", 1, True, "boolean value"),
    ],
)
def test_bad_values_drop_the_whole_category_and_are_reported(
    variable: str, index: int, value: Any, why: str
) -> None:  # synthetic mutation
    payload = copy.deepcopy(WIND)
    payload["hourly"][variable][index] = value
    result, _ = _fetch(payload)
    assert result.items == (), why
    assert result.issues and result.issues[0].startswith("wind:")


@pytest.mark.parametrize("unit", ["m/s", "mph", "kn", ""])
def test_a_different_wind_speed_unit_is_rejected_never_converted(unit: str) -> None:  # synthetic
    payload = copy.deepcopy(WIND)
    payload["hourly_units"]["wind_speed_10m"] = unit
    result, _ = _fetch(payload)
    assert result.items == () and "unit" in result.issues[0]


def test_missing_hours_are_reported_not_filled() -> None:  # synthetic mutation
    payload = copy.deepcopy(WIND)
    for series in payload["hourly"].values():
        del series[-3:]
    result, _ = _fetch(payload)
    assert result.items == () and "not returned" in result.issues[0]


def test_a_short_series_for_one_variable_is_rejected() -> None:  # synthetic mutation
    payload = copy.deepcopy(WIND)
    del payload["hourly"]["wind_gusts_10m"][-2:]
    result, _ = _fetch(payload)
    assert result.items == () and "short" in result.issues[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(error=True, reason="bad"),
        lambda p: p.update(utc_offset_seconds=3600),
        lambda p: p.pop("hourly"),
        lambda p: p.pop("hourly_units"),
        lambda p: p.pop("latitude"),
        lambda p: p["hourly"]["time"].__setitem__(0, p["hourly"]["time"][1]),
        lambda p: p["hourly"]["time"].__setitem__(0, "2026-10-08T17:00+05:00"),
    ],
)
def test_malformed_responses_raise(mutate: Any) -> None:  # synthetic mutation
    payload = copy.deepcopy(WIND)
    mutate(payload)
    with pytest.raises(ConnectorResponseError):
        _fetch(payload)


def test_non_object_and_hostile_provider_text_are_handled() -> None:
    with pytest.raises(ConnectorResponseError):
        _fetch([])
    with pytest.raises(ConnectorResponseError) as exc:
        _fetch({"error": True, "reason": "x" * 5000 + "\n\x1b[31mINJECT"})
    assert len(str(exc.value)) < 200 and "\x1b" not in str(exc.value)


# --- review follow-ups: grid cell validation and attribution -----------------------------------


@pytest.mark.parametrize("bad", [None, True, "5.8", float("nan"), float("inf"), 91.0, -91.0])
def test_an_invalid_grid_latitude_is_rejected(bad: Any) -> None:  # synthetic mutation
    payload = copy.deepcopy(WIND)
    payload["latitude"] = bad
    with pytest.raises(ConnectorResponseError, match="grid cell"):
        _fetch(payload)


@pytest.mark.parametrize("bad", [None, True, float("nan"), 181.0, -181.0])
def test_an_invalid_grid_longitude_is_rejected(bad: Any) -> None:  # synthetic mutation
    payload = copy.deepcopy(WIND)
    payload["longitude"] = bad
    with pytest.raises(ConnectorResponseError, match="grid cell"):
        _fetch(payload)


def test_every_open_meteo_item_carries_the_required_attribution_wording_and_link() -> None:
    from divesafe.data import OpenMeteoMarineConnector

    marine = json.loads(
        (FIXTURE.parent / "open_meteo_marine_redang_recorded_2026-10-08.json").read_text()
    )
    wind_result, _ = _fetch()
    marine_items = asyncio.run(
        OpenMeteoMarineConnector(_Getter(marine)).fetch(REDANG_ISLAND, START, END, NOW)
    ).items
    for item in (*wind_result.items, *marine_items):
        text = item.value["attribution"]
        assert "Open-Meteo.com" in text and "https://open-meteo.com/" in text
        assert "CC BY 4.0" in text
