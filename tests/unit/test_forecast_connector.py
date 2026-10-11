"""General-forecast connector: recorded real response plus labelled synthetic mutations."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from divesafe.data import TIOMAN_ISLAND, ConnectorResponseError, DataGovMyForecastConnector
from divesafe.domain import DataCategory, DataKind, DataQuality, DiveSite

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent
        / "fixtures/data_gov_my_forecast_tioman_recorded_2026-10-11.json"
    ).read_text()
)
NOW = datetime(2026, 10, 11, 1, 0, tzinfo=UTC)
START = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)  # 08:00 on 12 Oct, Malaysia time
END = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)


class _Getter:
    def __init__(self, payload: Any) -> None:
        self.payload, self.calls = payload, []

    async def get_json(self, url: str, params: Any) -> Any:
        self.calls.append((url, dict(params)))
        return self.payload


def _run(payload: Any = FIXTURE, start: datetime = START, end: datetime = END, site: Any = None):  # type: ignore[no-untyped-def]
    getter = _Getter(payload)
    result = asyncio.run(
        DataGovMyForecastConnector(getter).fetch(site or TIOMAN_ISLAND, start, end, NOW)
    )
    return result, getter


def test_the_recorded_response_gives_one_labelled_forecast_item_for_the_day() -> None:
    result, getter = _run()
    (item,) = result.items
    assert item.category is DataCategory.WEATHER
    assert item.data_kind is DataKind.FORECAST and item.is_forecast
    assert item.quality is DataQuality.DEGRADED and item.upstream_known is False
    assert item.value["date"] == "2026-10-12" and item.value["location_id"] == "Rc024"
    assert item.value["min_temp_c"] == 24 and item.value["max_temp_c"] == 35
    assert "no wind, wave, swell, current or tide" in " ".join(item.quality_notes)
    assert getter.calls[0][0].startswith("https://api.data.gov.my/weather/forecast/")
    assert getter.calls[0][1]["contains"] == "Rc024@location__location_id"


def test_the_item_covers_the_malaysia_time_day_not_the_utc_day() -> None:
    (item,) = _run()[0].items
    assert item.valid_at == datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
    assert item.valid_until == datetime(2026, 10, 12, 16, 0, tzinfo=UTC)


def test_a_window_over_two_malaysia_days_gives_two_items() -> None:
    start = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)  # 23:00 on 12 Oct
    end = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)  # 02:00 on 13 Oct
    assert [i.value["date"] for i in _run(start=start, end=end)[0].items] == [
        "2026-10-12",
        "2026-10-13",
    ]


def test_live_and_documented_spellings_are_both_translated_and_unknown_ones_are_not_guessed() -> (
    None
):
    day = next(r for r in FIXTURE if r["date"] == "2026-10-12")
    live = copy.deepcopy(FIXTURE)
    for row in live:
        if row["date"] == "2026-10-12":
            row["morning_forecast"] = "Jerebu"
            row["afternoon_forecast"] = "ribut petir di satu dua tempat"
            row["night_forecast"] = "Hujan lebat sekali"  # synthetic: not in the table
    (item,) = _run(live)[0].items
    assert item.value["periods"]["morning"]["en"] == "Hazy"
    assert item.value["periods"]["afternoon"]["en"] == "Isolated thunderstorms"
    assert item.value["periods"]["night"] == {"ms": "Hujan lebat sekali", "en": None}
    assert "night_forecast" in " ".join(item.quality_notes) and day


def test_the_text_is_marked_untrusted() -> None:
    (item,) = _run()[0].items
    assert "periods" in item.value["untrusted_text"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[0].pop("max_temp"),
        lambda rows: rows[0]["location"].update(location_id="Rc999"),
        lambda rows: rows[0]["location"].update(location_name="Pulau Redang"),
        lambda rows: rows[0].update(date="not-a-date"),
        lambda rows: rows[0].update(date=None),
        lambda rows: rows[0].update(min_temp="24"),
        lambda rows: rows[0].update(min_temp=True),
        lambda rows: rows[0].update(min_temp=40, max_temp=30),
        lambda rows: rows[0].update(morning_forecast=""),
        lambda rows: rows[0].update(morning_forecast="x" * 500),
        lambda rows: rows[0].update(morning_forecast="a\x00b"),
        lambda rows: rows[0].update(night_forecast=5),
        lambda rows: rows.append(copy.deepcopy(rows[0])),
        lambda rows: rows.__setitem__(0, "nope"),
    ],
)
def test_a_malformed_forecast_is_rejected_not_repaired(mutate: Any) -> None:
    rows = copy.deepcopy(FIXTURE)
    # Put the first row on the day the window needs, so the mutation is actually read.
    for row in rows:
        if row["date"] == "2026-10-12":
            rows.insert(0, rows.pop(rows.index(row)))
            break
    mutate(rows)
    with pytest.raises(ConnectorResponseError):
        _run(rows)


def test_a_response_that_is_not_a_list_is_rejected() -> None:
    for bad in ({}, "x", None, {"data": []}):
        with pytest.raises(ConnectorResponseError):
            _run(bad)


def test_a_full_page_is_refused_as_possibly_truncated() -> None:
    rows = [copy.deepcopy(FIXTURE[0]) for _ in range(100)]
    with pytest.raises(ConnectorResponseError, match="truncated"):
        _run(rows)


def test_a_window_the_forecast_does_not_cover_is_rejected_not_partly_served() -> None:
    far = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    with pytest.raises(ConnectorResponseError, match="does not cover"):
        _run(start=far, end=far.replace(hour=3))
    spans = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)  # 23:00 on the last day, into a missing one
    with pytest.raises(ConnectorResponseError, match="does not cover"):
        _run(start=spans, end=spans.replace(hour=18))


def test_an_empty_response_is_rejected() -> None:
    with pytest.raises(ConnectorResponseError, match="does not cover"):
        _run([])


def test_a_site_without_a_registered_location_is_refused_before_any_request() -> None:
    other = DiveSite(
        id="my-other-site", name="x", latitude=1.0, longitude=100.0, coordinate_source="t"
    )
    getter = _Getter(FIXTURE)
    with pytest.raises(ConnectorResponseError, match="no forecast location"):
        asyncio.run(DataGovMyForecastConnector(getter).fetch(other, START, END, NOW))
    assert getter.calls == []


def test_a_reversed_window_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="window_end"):
        _run(start=END, end=START)


@pytest.mark.parametrize("bad", [10**400, -999, 61, -11, True])
def test_an_implausible_temperature_is_rejected(bad: Any) -> None:
    rows = copy.deepcopy(FIXTURE)
    for row in rows:
        if row["date"] == "2026-10-12":
            row["max_temp"] = bad
    with pytest.raises(ConnectorResponseError):
        _run(rows)


def test_a_window_longer_than_the_forecast_can_cover_is_rejected() -> None:
    with pytest.raises(ConnectorResponseError, match="longer than"):
        _run(start=START, end=START.replace(day=25))
