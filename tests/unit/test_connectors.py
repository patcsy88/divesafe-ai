"""Connector tests use recorded real responses (tests/fixtures) and synthetic mutations."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from divesafe.data import (
    REDANG_ISLAND,
    ConnectorResponseError,
    DataGovMyWarningConnector,
    OpenMeteoMarineConnector,
)
from divesafe.domain import DataCategory

FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
START = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
END = datetime(2026, 10, 8, 21, 0, tzinfo=UTC)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


MARINE = _load("open_meteo_marine_redang_recorded_2026-10-08.json")
WARNINGS = _load("data_gov_my_warning_recorded_2026-10-09.json")
WITH_ADVISORY = _load("data_gov_my_warning_incl_no_advisory_recorded_2026-10-09.json")


class FakeGetter:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        self.calls.append((url, params))
        return self.payload


def _marine(payload: Any = MARINE) -> tuple[OpenMeteoMarineConnector, FakeGetter]:
    getter = FakeGetter(payload)
    return OpenMeteoMarineConnector(getter), getter


def _fetch_marine(payload: Any = MARINE):  # type: ignore[no-untyped-def]
    connector, getter = _marine(payload)
    result = asyncio.run(connector.fetch(REDANG_ISLAND, START, END, NOW))
    return result, getter


# --- Open-Meteo marine ---------------------------------------------------------------------


def test_marine_yields_one_item_per_category_per_hour() -> None:
    result, _ = _fetch_marine()
    assert result.issues == ()
    hours = 4  # 18:00 to 21:00 inclusive
    for category in (DataCategory.WAVES_SWELL, DataCategory.SEA_TEMPERATURE, DataCategory.CURRENTS):
        assert len([i for i in result.items if i.category == category]) == hours


def test_marine_provenance_is_complete_and_honest() -> None:
    result, _ = _fetch_marine()
    item = next(i for i in result.items if i.category == DataCategory.WAVES_SWELL)
    assert item.source == "open-meteo-marine"
    assert item.retrieved_at == NOW
    assert item.valid_at.tzinfo is not None
    assert item.is_forecast is True
    assert item.value["data_type"] == "model"
    assert item.value["grid_cell"] == [5.791664, 103.04167]  # differs from the requested point
    assert item.value["requested_point"] == [5.77736, 103.00759]
    assert item.value["units"]["wave_height"] == "m"
    assert "Open-Meteo" in item.value["attribution"]


def test_marine_request_never_asks_for_sea_level_or_tides() -> None:
    _, getter = _fetch_marine()
    _, params = getter.calls[0]
    assert "sea_level" not in params["hourly"]
    assert params["timezone"] == "UTC"
    assert getter.calls[0][0].startswith("https://")


def test_marine_never_produces_tides_evidence() -> None:
    result, _ = _fetch_marine()
    assert DataCategory.TIDES not in {i.category for i in result.items}


def test_marine_ids_are_unique_and_deterministic() -> None:
    first, _ = _fetch_marine()
    second, _ = _fetch_marine()
    ids = [i.id for i in first.items]
    assert len(ids) == len(set(ids))
    assert ids == [i.id for i in second.items]


def test_marine_null_value_drops_whole_category_and_reports_it() -> None:  # synthetic mutation
    payload = copy.deepcopy(MARINE)
    payload["hourly"]["wave_height"][2] = None
    result, _ = _fetch_marine(payload)
    assert DataCategory.WAVES_SWELL not in {i.category for i in result.items}
    assert any(issue.startswith("waves_swell") for issue in result.issues)
    assert DataCategory.CURRENTS in {i.category for i in result.items}


def test_marine_wrong_unit_is_rejected_not_converted() -> None:  # synthetic mutation
    payload = copy.deepcopy(MARINE)
    payload["hourly_units"]["wave_height"] = "ft"
    result, _ = _fetch_marine(payload)
    assert DataCategory.WAVES_SWELL not in {i.category for i in result.items}
    assert any("unit" in issue for issue in result.issues)


def test_marine_out_of_range_value_is_rejected() -> None:  # synthetic mutation
    payload = copy.deepcopy(MARINE)
    payload["hourly"]["wave_direction"][1] = 400
    result, _ = _fetch_marine(payload)
    assert DataCategory.WAVES_SWELL not in {i.category for i in result.items}


def test_marine_missing_hours_are_reported_not_filled() -> None:  # synthetic mutation
    payload = copy.deepcopy(MARINE)
    for series in payload["hourly"].values():
        del series[-3:]  # drop the last three hours
    result, _ = _fetch_marine(payload)
    assert result.items == ()
    assert len(result.issues) == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(error=True, reason="bad"),
        lambda p: p.update(utc_offset_seconds=3600),
        lambda p: p.pop("hourly"),
        lambda p: p.pop("latitude"),
    ],
)
def test_marine_malformed_response_raises(mutate: Any) -> None:  # synthetic mutation
    payload = copy.deepcopy(MARINE)
    mutate(payload)
    with pytest.raises(ConnectorResponseError):
        _fetch_marine(payload)


def test_marine_rejects_non_object_response() -> None:
    with pytest.raises(ConnectorResponseError):
        _fetch_marine([])


# --- data.gov.my warnings ------------------------------------------------------------------


def _warnings(payload: Any = WARNINGS, start: datetime = START, end: datetime = END):  # type: ignore[no-untyped-def]
    getter = FakeGetter(payload)
    connector = DataGovMyWarningConnector(getter)
    return asyncio.run(connector.fetch(REDANG_ISLAND, start, end, NOW)), getter


def test_warnings_become_evidence_with_full_text_and_utc_times() -> None:
    result, _ = _warnings()
    assert len(result.items) == 3
    item = result.items[0]
    assert item.category == DataCategory.MARINE_WARNINGS
    assert item.is_forecast is False
    assert item.valid_at.utcoffset() == timedelta(0)
    assert item.value["text_en"].startswith("SECTION A")
    assert "UTC+08:00" in item.value["timezone_assumption"]


def test_warning_times_are_converted_from_malaysia_time() -> None:
    result, _ = _warnings()
    second = next(i for i in result.items if i.value["valid_to"].startswith("2026-10-08T22:00"))
    # source says 2026-10-09T06:00 local (UTC+8) = 22:00 UTC the previous day
    assert second.value["valid_from"].startswith("2026-10-08T17:00")


def test_warning_applicability_is_never_claimed() -> None:
    result, _ = _warnings()
    for item in result.items:
        assert item.value["applicability"].startswith("NOT DETERMINED")
    first = result.items[0]
    assert first.value["matched_area_names"] == ["Terengganu"]  # informational only


def test_warnings_outside_the_window_are_excluded() -> None:
    late_start = datetime(2026, 10, 11, 0, 30, tzinfo=UTC)  # after every entry ends in UTC or UTC+8
    result, _ = _warnings(start=late_start, end=late_start + timedelta(hours=2))
    assert [i.value["kind"] for i in result.items] == ["no_active_warnings"]


def test_no_warnings_is_positive_evidence_not_missing_data() -> None:
    result, _ = _warnings(payload=[])
    assert len(result.items) == 1
    assert result.items[0].category == DataCategory.MARINE_WARNINGS
    assert result.items[0].value["kind"] == "no_active_warnings"


def test_warnings_request_is_filtered_and_https() -> None:
    _, getter = _warnings()
    url, params = getter.calls[0]
    assert url == "https://api.data.gov.my/weather/warning/"
    assert params["timestamp_start"].endswith("@warning_issue__issued")


def test_warnings_truncated_page_is_refused() -> None:  # synthetic mutation
    with pytest.raises(ConnectorResponseError):
        _warnings(payload=[WARNINGS[0]] * 100)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w.pop("valid_to"),
        lambda w: w.update(valid_from="not-a-date"),
        lambda w: w.update(valid_to="2020-01-01T00:00:00"),
        lambda w: w.update(warning_issue="x"),
    ],
)
def test_warnings_malformed_entry_raises(mutate: Any) -> None:  # synthetic mutation
    payload = copy.deepcopy(WARNINGS)
    mutate(payload[0])
    with pytest.raises(ConnectorResponseError):
        _warnings(payload=payload)


def test_warnings_non_list_response_raises() -> None:
    with pytest.raises(ConnectorResponseError):
        _warnings(payload={"error": "nope"})


# --- undated advisories (recorded real "No Advisory" notice) --------------------------------


def test_undated_advisory_is_included_in_full_and_flagged() -> None:
    result, _ = _warnings(payload=WITH_ADVISORY)
    kinds = sorted(i.value["kind"] for i in result.items)
    assert kinds == ["advisory_without_validity", "warning", "warning", "warning"]
    advisory = next(i for i in result.items if i.value["kind"] == "advisory_without_validity")
    assert advisory.value["valid_from"] is None and advisory.value["valid_to"] is None
    assert "Tropical Cyclone" in advisory.value["text_en"]
    assert advisory.value["instruction_en"] == ""  # source null must not become the text "None"
    assert advisory.valid_at == datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # issued 22:30 UTC+8


def test_undated_advisory_is_never_turned_into_a_no_active_warnings_claim() -> None:
    only_advisory = [WITH_ADVISORY[3]]
    result, _ = _warnings(payload=only_advisory)
    assert [i.value["kind"] for i in result.items] == ["advisory_without_validity"]


def test_entry_with_only_one_validity_time_is_malformed() -> None:  # synthetic mutation
    payload = copy.deepcopy(WITH_ADVISORY)
    payload[3]["valid_to"] = "2026-10-09T00:00:00"
    with pytest.raises(ConnectorResponseError):
        _warnings(payload=payload)


# --- review fixes: model labelling, grid distance, window, parsing hardening ----------------


def test_marine_model_hours_already_past_are_still_labelled_model_not_observation() -> None:
    connector, _ = _marine()
    late_now = datetime(2026, 10, 8, 23, 59, tzinfo=UTC)  # after every fixture hour
    result = asyncio.run(connector.fetch(REDANG_ISLAND, START, END, late_now))
    assert result.items and all(
        i.is_forecast and i.value["data_type"] == "model" for i in result.items
    )


def test_marine_records_grid_snap_distance() -> None:
    result, _ = _fetch_marine()
    km = result.items[0].value["grid_distance_km"]
    assert 3.0 < km < 6.0  # recorded response snapped ~4 km from the island reference point
    assert "not the dive site" in result.items[0].value["spatial_scope"]


def test_marine_window_end_is_rounded_up_to_cover_a_partial_hour() -> None:
    connector, _ = _marine()
    end = datetime(2026, 10, 8, 20, 30, tzinfo=UTC)
    result = asyncio.run(connector.fetch(REDANG_ISLAND, START, end, NOW))
    hours = {i.valid_at.hour for i in result.items if i.category == DataCategory.CURRENTS}
    assert hours == {18, 19, 20, 21}


def test_marine_duplicate_or_offset_timestamps_are_rejected() -> None:  # synthetic mutation
    dup = copy.deepcopy(MARINE)
    dup["hourly"]["time"][1] = dup["hourly"]["time"][0]
    with pytest.raises(ConnectorResponseError):
        _fetch_marine(dup)
    offset = copy.deepcopy(MARINE)
    offset["hourly"]["time"][0] = "2026-10-08T17:00+05:00"
    with pytest.raises(ConnectorResponseError):
        _fetch_marine(offset)


def test_marine_provider_error_text_is_bounded() -> None:  # synthetic mutation
    payload = {"error": True, "reason": "x" * 5000 + "\n\x1b[31mINJECT"}
    with pytest.raises(ConnectorResponseError) as exc:
        _fetch_marine(payload)
    assert len(str(exc.value)) < 200 and "\x1b" not in str(exc.value)


def test_marine_odd_values_become_connector_errors_not_raw_exceptions() -> None:  # synthetic
    payload = copy.deepcopy(MARINE)
    payload["latitude"] = 10**400  # float conversion overflows
    with pytest.raises(ConnectorResponseError):
        _fetch_marine(payload)


def test_warning_included_if_it_overlaps_under_utc_even_if_not_under_malaysia_time() -> None:
    entry = copy.deepcopy(WARNINGS[0])
    entry["valid_from"], entry["valid_to"] = "2026-10-08T00:00:00", "2026-10-08T22:00:00"
    # under UTC+8 this ends 14:00Z (before the 18:00Z window); under UTC it ends 22:00Z (overlaps)
    result, _ = _warnings(payload=[entry])
    assert [i.value["kind"] for i in result.items] == ["warning"]


def test_warnings_lookback_is_30_days() -> None:
    _, getter = _warnings()
    assert getter.calls[0][1]["timestamp_start"].startswith("2026-09-0")  # window start minus 30 d


def test_warnings_reject_timestamps_that_carry_an_offset() -> None:  # synthetic mutation
    payload = copy.deepcopy(WARNINGS)
    payload[0]["valid_from"] = "2026-10-08T00:00:00+00:00"
    with pytest.raises(ConnectorResponseError):
        _warnings(payload=payload)


def test_warnings_overlong_text_is_rejected() -> None:  # synthetic mutation
    payload = copy.deepcopy(WARNINGS)
    payload[0]["text_en"] = "x" * 50_000
    with pytest.raises(ConnectorResponseError):
        _warnings(payload=payload)


def test_warning_text_is_marked_untrusted_and_ids_depend_on_site() -> None:
    result, _ = _warnings()
    item = result.items[0]
    assert "text_en" in item.value["untrusted_text"]
    other = REDANG_ISLAND.model_copy(update={"id": "other-site"})
    other_result = asyncio.run(
        DataGovMyWarningConnector(FakeGetter(WARNINGS)).fetch(other, START, END, NOW)
    )
    assert {i.id for i in result.items}.isdisjoint({i.id for i in other_result.items})


def test_no_warnings_item_states_what_it_does_and_does_not_mean() -> None:
    result, _ = _warnings(payload=[])
    value = result.items[0].value
    assert value["lookback_days"] == 30
    assert "not proof of calm" in value["meaning"]
    assert value["window_start"] and value["window_end"]
