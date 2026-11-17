"""tides.today connector (ADR 0029, Amendment 1: API V2): parse, every refusal, the request plan
and the transport.

The fixture is SYNTHETIC (the observed shape with invented values); see tests/fixtures/README.md.
"""

from __future__ import annotations

import asyncio
import copy
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from divesafe.config import Settings
from divesafe.data import (
    TIOMAN_ISLAND,
    ConnectorResponseError,
    ConnectorTransportError,
    UrllibJsonGetter,
)
from divesafe.data import tides_today as tt
from divesafe.data.http import ALLOWED_HOSTS, MET_API_HOSTS, TIDES_TODAY_HOSTS
from divesafe.domain import DataCategory, DataKind, DataQuality

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures/tides_today_teluk_tekek_synthetic.json").read_text()
)
NOW = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)  # 09:00 MYT on 17 Nov
START = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)  # 08:00 MYT on 18 Nov
END = START + timedelta(hours=4)


def _payloads() -> tuple[Any, Any, Any]:
    return (
        copy.deepcopy(FIXTURE["location"]),
        copy.deepcopy(FIXTURE["tide_extremes"]),
        copy.deepcopy(FIXTURE["ephemeris"]),
    )


class _Getter:
    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads or _payloads())
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, url: str, params: Any) -> Any:
        self.calls.append((url, dict(params)))
        return self.payloads[len(self.calls) - 1]


def _parse(loc: Any, ext: Any, eph: Any, start: datetime = START, end: datetime = END):  # type: ignore[no-untyped-def]
    return tt.TidesTodayConnector(_Getter()).parse(loc, ext, eph, TIOMAN_ISLAND, start, end, NOW)


def _fetch(*payloads: Any, start: datetime = START, end: datetime = END):  # type: ignore[no-untyped-def]
    getter = _Getter(*payloads)
    result = asyncio.run(tt.TidesTodayConnector(getter).fetch(TIOMAN_ISLAND, start, end, NOW))
    return result, getter


def test_the_synthetic_fixture_parses_into_one_degraded_prediction_for_the_local_day() -> None:
    result, getter = _fetch()
    (item,) = result.items
    assert result.issues == ()
    assert item.category is DataCategory.TIDES and item.data_kind is DataKind.PREDICTION
    assert item.quality is DataQuality.DEGRADED and item.upstream_known is False
    assert item.is_forecast is True and item.source == "tides-today-prediction"
    assert item.source_version == "api-v2"
    assert item.valid_at == datetime(2026, 11, 17, 16, tzinfo=UTC)
    assert item.valid_until == datetime(2026, 11, 18, 16, tzinfo=UTC)
    assert item.retrieved_at == NOW
    assert item.location is not None
    assert (item.location.latitude, item.location.longitude) == (2.8, 104.133003)
    assert "3.7 km" in (item.location.description or "")
    tide = item.value["tide"]
    assert tide["unit"] == "m"
    assert tide["unit_basis"] == "metres, as named by the API field heightM (no unit is documented)"
    assert tide["datum"].startswith("chart datum (CD) as stated by the API")
    assert "level for this point is not stated" in tide["datum"]
    assert [e["kind"] for e in tide["extremes"]] == ["high", "low", "high", "low"]
    assert tide["extremes"][0] == {
        "time": "2026-11-18T03:40:00+08:00",
        "kind": "high",
        "height_m": 2.5,
    }
    assert all(e["time"].startswith("2026-11-18T") for e in tide["extremes"])
    assert tide["moon_phase"] == {"name": "First Quarter", "fraction": 0.25}
    assert set(tide) == {"datum", "unit", "unit_basis", "extremes", "moon_phase"}
    assert item.value["daylight"] == {
        "sunrise": "2026-11-18T06:50:00+08:00",
        "sunset": "2026-11-18T18:45:00+08:00",
    }
    assert item.value["requested_location_id"] == 2960
    assert "SYNTHETIC placeholder" in item.value["provider_notice"]
    assert "tides.today" in item.value["attribution"]
    assert len(item.value["attribution"]) <= 300
    assert [url for url, _ in getter.calls] == [
        "https://api.tidestoday.io/v2/locations/2960",
        "https://api.tidestoday.io/v2/locations/2960/tide-extremes",
        "https://api.tidestoday.io/v2/locations/2960/ephemeris",
    ]


def test_the_requests_ask_one_day_either_side_for_extremes_and_the_local_days_otherwise() -> None:
    _, getter = _fetch()
    assert getter.calls[0][1] == {"locale": "en"}
    assert getter.calls[1][1] == {
        "filter.dateTime": "$btw:2026-11-17,2026-11-19",
        "itemsPerPage": "150",
    }
    assert getter.calls[2][1] == {"filter.date": "$btw:2026-11-18,2026-11-18", "itemsPerPage": "50"}


def test_no_weather_wave_or_current_tide_value_reaches_the_item() -> None:
    loc, ext, eph = _payloads()
    # SYNTHETIC extras in the shape the location response also carries; none may be copied.
    loc["data"]["currentTide"] = {"type": "rising", "heightM": 9.99, "estimated": True}
    loc["data"]["currentWeather"] = {"significantM": 7.77, "swellM": 6.66, "waterTempC": 55.5}
    (item,) = _parse(loc, ext, eph).items
    text = json.dumps(item.value)
    for word in ("9.99", "7.77", "6.66", "55.5", "rising", "swell", "moonrise", "moonset"):
        assert word not in text


def test_the_digest_changes_with_the_values() -> None:
    first = _parse(*_payloads()).items[0].id
    loc, ext, eph = _payloads()
    ext["items"][5]["heightM"] = 1.71
    assert _parse(loc, ext, eph).items[0].id != first
    loc, ext, eph = _payloads()
    eph["items"][0]["moonPhase"] = 0.26
    assert _parse(loc, ext, eph).items[0].id != first


def test_heights_come_from_heightm_never_heightf() -> None:
    loc, ext, eph = _payloads()
    for row in ext["items"]:
        row["heightF"] = 99.0
    (item,) = _parse(loc, ext, eph).items
    assert item.value["tide"]["extremes"][0]["height_m"] == 2.5


# --- refusals --------------------------------------------------------------------------------


def _refuses(mutate, match: str | None = None) -> None:  # type: ignore[no-untyped-def]
    loc, ext, eph = _payloads()
    mutate(loc, ext, eph)
    with pytest.raises(ConnectorResponseError, match=match):
        _parse(loc, ext, eph)


def _set_loc(key: str, value: Any):  # type: ignore[no-untyped-def]
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        loc["data"][key] = value

    return mutate


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", 2961),
        ("id", "2960"),
        ("id", True),
        ("id", 2960.0),
        ("name", "Kampung Tekek"),
        ("lat", "2.900000"),
        ("lng", 104.133003),
        ("lng", "east"),
        ("source", None),
        ("source", "tpe"),
    ],
)
def test_a_different_or_malformed_location_is_refused(key: str, value: Any) -> None:
    _refuses(_set_loc(key, value))


def test_a_location_response_without_data_is_refused() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        loc["data"] = [loc["data"]]

    _refuses(mutate)


def test_a_different_time_zone_is_refused() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        loc["data"]["region"]["timezone"] = "Asia/Singapore"

    _refuses(mutate, "time zone")


@pytest.mark.parametrize("value", [None, "LAT", "cd", "CD ", "MSL", 1, ""])
def test_a_datum_other_than_chart_datum_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        if value is None:
            del loc["data"]["source"]["datumType"]
        else:
            loc["data"]["source"]["datumType"] = value

    _refuses(mutate, "chart datum")


@pytest.mark.parametrize("value", [None, "worldtides", "admiralty", "TPE", ""])
def test_an_upstream_other_than_the_tides_today_engine_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        if value is None:
            del loc["data"]["source"]["source"]
        else:
            loc["data"]["source"]["source"] = value

    _refuses(mutate, "tides.today engine")


@pytest.mark.parametrize("value", [None, "", "   ", "x" * 2001, "line\nbreak", 5])
def test_a_missing_long_or_unprintable_notice_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        loc["data"]["source"]["copyright"] = value

    _refuses(mutate, "notice")


def test_the_location_echo_is_checked() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        loc["meta"]["params"] = {"locale": "ms"}

    _refuses(mutate, "dates requested")


def test_highs_and_lows_that_do_not_alternate_are_refused() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["type"] = "low-tide"

    _refuses(mutate, "alternate")


def test_extremes_out_of_order_are_refused() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["dateTime"] = "2026-11-18T02:00:00.000Z"

    _refuses(mutate, "time order")


def test_a_long_run_of_missing_extremes_is_refused_by_the_gap_bound() -> None:
    """A whole missing day (alternation still holds) is refused. A single missing high/low pair
    can stay under the bound and is NOT detected: ADR 0029 Known limits."""

    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        del ext["items"][4:8]
        ext["meta"]["totalItems"] -= 4

    _refuses(mutate, "missing")


def test_a_high_below_its_neighbouring_low_is_refused() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["heightM"] = 1.0

    _refuses(mutate, "above the low")


@pytest.mark.parametrize("value", [None, "1.2", True, float("nan"), float("inf"), 99.0, -5.0])
def test_a_height_that_is_not_a_plausible_number_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["heightM"] = value

    _refuses(mutate)


def test_a_missing_heightm_is_refused_even_with_heightf() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        del ext["items"][5]["heightM"]

    _refuses(mutate, "height")


@pytest.mark.parametrize("value", [True, None, "false", 0])
def test_an_estimated_or_untyped_estimated_flag_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["estimated"] = value

    _refuses(mutate)


@pytest.mark.parametrize(
    "value",
    ["2026-11-18T07:20:00.000", "2026-11-18T15:20:00.000+08:00", "yesterday", 1731900000, None],
)
def test_a_time_that_is_not_utc_with_z_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["dateTime"] = value

    _refuses(mutate)


def test_a_repeated_or_missing_id_is_refused() -> None:
    def repeat(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["id"] = ext["items"][4]["id"]

    def missing(loc: Any, ext: Any, eph: Any) -> None:
        del ext["items"][5]["id"]

    _refuses(repeat, "id")
    _refuses(missing, "id")


@pytest.mark.parametrize("value", ["slack", "high", "low", "HIGH-TIDE", None, 1])
def test_an_unknown_kind_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["type"] = value

    _refuses(mutate, "neither high nor low")


def test_extremes_that_do_not_cover_the_whole_local_day_are_refused() -> None:
    def tail(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"] = ext["items"][:7]
        ext["meta"]["totalItems"] = 7

    def head(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"] = ext["items"][4:]
        ext["meta"]["totalItems"] = 8

    _refuses(tail, "whole day")
    _refuses(head, "whole day")


def test_an_extreme_outside_the_requested_dates_is_refused() -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][-1]["dateTime"] = "2026-11-20T00:00:00.000Z"

    _refuses(mutate, "outside the dates")


def test_paging_or_a_count_mismatch_is_refused() -> None:
    def pages(loc: Any, ext: Any, eph: Any) -> None:
        ext["meta"]["totalPages"] = 2

    def count(loc: Any, ext: Any, eph: Any) -> None:
        ext["meta"]["totalItems"] = 13

    def eph_pages(loc: Any, ext: Any, eph: Any) -> None:
        eph["meta"]["totalPages"] = 2

    def eph_count(loc: Any, ext: Any, eph: Any) -> None:
        eph["meta"]["totalItems"] = 2

    _refuses(pages, "single page")
    _refuses(count, "count")
    _refuses(eph_pages, "single page")
    _refuses(eph_count, "count")


def test_a_response_for_other_dates_is_refused() -> None:
    def ext_dates(loc: Any, ext: Any, eph: Any) -> None:
        ext["meta"]["params"]["filter.dateTime"] = "$btw:2026-11-17,2026-11-20"

    def eph_dates(loc: Any, ext: Any, eph: Any) -> None:
        eph["meta"]["params"]["filter.date"] = "$btw:2026-11-17,2026-11-18"

    def per_page(loc: Any, ext: Any, eph: Any) -> None:
        ext["meta"]["params"]["itemsPerPage"] = "50"

    _refuses(ext_dates, "dates requested")
    _refuses(eph_dates, "dates requested")
    _refuses(per_page, "dates requested")


def test_a_missing_repeated_or_malformed_ephemeris_row_is_refused() -> None:
    def missing(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0]["date"] = "2026-11-19"

    def repeated(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"].append(copy.deepcopy(eph["items"][0]))
        eph["meta"]["totalItems"] = 2

    def none(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"] = []
        eph["meta"]["totalItems"] = 0

    def stamp(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0]["date"] = "2026-11-18T00:00:00.000Z"

    def not_a_day(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0]["date"] = "2026-11-31"

    _refuses(missing, "missing a needed day")
    _refuses(repeated, "wrong number of days")
    _refuses(none, "wrong number of days")
    _refuses(stamp, "local day")
    _refuses(not_a_day, "local day")


@pytest.mark.parametrize("value", [2961, "2960", True, None])
def test_an_ephemeris_row_for_another_location_is_refused(value: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0]["locationId"] = value

    _refuses(mutate, "different location")


def test_sun_times_that_do_not_fit_the_day_are_refused() -> None:
    def swapped(loc: Any, ext: Any, eph: Any) -> None:
        row = eph["items"][0]
        row["sunrise"], row["sunset"] = row["sunset"], row["sunrise"]

    def other_day(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0]["sunrise"] = "2026-11-16T22:50:00.000Z"

    def absent(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0]["sunset"] = None

    _refuses(swapped, "sunrise")
    _refuses(other_day, "sunrise")
    _refuses(absent, "sunset")


@pytest.mark.parametrize(
    ("name", "fraction"),
    [
        ("first-quarter", 0.25),
        ("first quarter", 0.25),
        ("Blue Moon", 0.25),
        (None, 0.25),
        ("Full", 1.01),
        ("Full", -0.01),
        ("Full", "0.5"),
        ("Full", True),
        ("Full", float("nan")),
        ("Full", None),
    ],
)
def test_a_bad_moon_phase_is_refused(name: Any, fraction: Any) -> None:
    def mutate(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0].update(moonPhaseName=name, moonPhase=fraction)

    _refuses(mutate, "moon phase")


@pytest.mark.parametrize("name", sorted(tt._MOON_PHASES))  # noqa: SLF001
def test_each_documented_moon_phase_name_is_accepted(name: str) -> None:
    loc, ext, eph = _payloads()
    eph["items"][0]["moonPhaseName"] = name
    (item,) = _parse(loc, ext, eph).items
    assert item.value["tide"]["moon_phase"]["name"] == name


def test_the_documented_moon_phase_names_are_exactly_eight() -> None:
    documented = {
        "New",
        "Waxing Crescent",
        "First Quarter",
        "Waxing Gibbous",
        "Full",
        "Waning Gibbous",
        "Last Quarter",
        "Waning Crescent",
    }
    assert documented == tt._MOON_PHASES  # noqa: SLF001


def test_a_payload_that_is_not_an_object_is_refused() -> None:
    for position in range(3):
        payloads = list(_payloads())
        payloads[position] = []
        with pytest.raises(ConnectorResponseError):
            _parse(*payloads)


def test_a_window_before_today_or_beyond_the_horizon_is_refused_before_any_request() -> None:
    for start in (NOW - timedelta(days=1), NOW + timedelta(days=7)):
        getter = _Getter()
        with pytest.raises(ConnectorResponseError):
            asyncio.run(
                tt.TidesTodayConnector(getter).fetch(
                    TIOMAN_ISLAND, start, start + timedelta(hours=1), NOW
                )
            )
        assert getter.calls == []


def test_the_last_day_inside_the_horizon_is_asked_for() -> None:
    start = datetime(2026, 11, 23, 2, tzinfo=UTC)  # 23 Nov MYT, today + 6
    getter = _Getter({}, {}, {})
    with pytest.raises(ConnectorResponseError):
        asyncio.run(
            tt.TidesTodayConnector(getter).fetch(
                TIOMAN_ISLAND, start, start + timedelta(hours=1), NOW
            )
        )
    assert len(getter.calls) == 3


def test_an_unregistered_site_or_an_extreme_window_is_refused_before_any_request() -> None:
    from divesafe.domain import DiveSite

    other = DiveSite(
        id="elsewhere",
        name="elsewhere",
        latitude=1.0,
        longitude=103.0,
        coordinate_source="test (synthetic)",
        area_names=(),
    )
    getter = _Getter()
    with pytest.raises(ConnectorResponseError, match="no tides.today location"):
        asyncio.run(tt.TidesTodayConnector(getter).fetch(other, START, END, NOW))
    extreme = datetime(9999, 12, 31, 20, tzinfo=UTC)
    with pytest.raises(ConnectorResponseError):
        asyncio.run(
            tt.TidesTodayConnector(getter).fetch(
                TIOMAN_ISLAND, extreme, extreme + timedelta(hours=1), NOW
            )
        )
    assert getter.calls == []


def test_a_failing_later_call_gives_no_item() -> None:
    class _Fails:
        def __init__(self, at: int) -> None:
            self.at, self.n = at, 0

        async def get_json(self, url: str, params: Any) -> Any:
            self.n += 1
            if self.n == self.at:
                raise ConnectorTransportError("HTTP 500 from api.tidestoday.io/v2/x")
            return _payloads()[self.n - 1]

    for at in (1, 2, 3):
        with pytest.raises(ConnectorTransportError):
            asyncio.run(tt.TidesTodayConnector(_Fails(at)).fetch(TIOMAN_ISLAND, START, END, NOW))


def test_a_dive_across_local_midnight_gets_both_day_items() -> None:
    start = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)  # 23:30 MYT on 18 Nov
    loc, ext, eph = _payloads()
    ext["meta"]["params"]["filter.dateTime"] = "$btw:2026-11-17,2026-11-20"
    # SYNTHETIC: one more invented ephemeris day.
    second = copy.deepcopy(eph["items"][0])
    second.update(
        id=501,
        date="2026-11-19",
        sunrise="2026-11-18T22:51:00.000Z",
        sunset="2026-11-19T10:46:00.000Z",
        moonPhase=0.3,
        moonPhaseName="Waxing Gibbous",
    )
    eph["items"].append(second)
    eph["meta"]["totalItems"] = 2
    eph["meta"]["params"]["filter.date"] = "$btw:2026-11-18,2026-11-19"
    result = _parse(loc, ext, eph, start, start + timedelta(hours=1))
    assert [i.value["date"] for i in result.items] == ["2026-11-18", "2026-11-19"]
    assert result.items[1].value["tide"]["moon_phase"]["name"] == "Waxing Gibbous"


# --- transport and settings -----------------------------------------------------------------


def test_the_host_is_kept_out_of_the_shared_list_and_has_its_own() -> None:
    assert frozenset({"api.tidestoday.io"}) == TIDES_TODAY_HOSTS
    assert not TIDES_TODAY_HOSTS & ALLOWED_HOSTS
    assert not TIDES_TODAY_HOSTS & MET_API_HOSTS


def test_the_shared_getter_refuses_the_tides_today_host() -> None:
    with pytest.raises(ConnectorTransportError, match="host not allowed"):
        asyncio.run(UrllibJsonGetter().get_json(f"{tt.BASE_URL}/locations/2960", {}))


@pytest.mark.parametrize(
    "hosts",
    [
        ALLOWED_HOSTS,
        TIDES_TODAY_HOSTS | MET_API_HOSTS,
        TIDES_TODAY_HOSTS | {"example.com"},
        TIDES_TODAY_HOSTS | ALLOWED_HOSTS,
        frozenset(),
    ],
)
def test_a_credential_is_only_allowed_on_one_credential_host_list(hosts: frozenset[str]) -> None:
    with pytest.raises(ValueError, match="own host list"):
        UrllibJsonGetter(allowed_hosts=hosts, auth_header=("x-api-key", "x"))


@pytest.mark.parametrize(
    "name", ["Authorization", "X-Api-Key", "x-api-key ", "api-key", "Cookie", "x-api-key\r\nX"]
)
def test_the_tides_today_getter_accepts_only_the_x_api_key_header_name(name: str) -> None:
    with pytest.raises(ValueError, match="header name"):
        UrllibJsonGetter(allowed_hosts=TIDES_TODAY_HOSTS, auth_header=(name, "k"))


def test_the_met_getter_never_accepts_the_x_api_key_header() -> None:
    with pytest.raises(ValueError, match="header name"):
        UrllibJsonGetter(allowed_hosts=MET_API_HOSTS, auth_header=("x-api-key", "METToken k"))


@pytest.mark.parametrize("value", ["", "   ", "k-SECRET\r\nX: y", "k-SECRET\x00", "ké-SECRET"])
def test_a_blank_or_unsafe_key_value_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(ValueError, match="printable ASCII") as caught:
        UrllibJsonGetter(allowed_hosts=TIDES_TODAY_HOSTS, auth_header=("x-api-key", value))
    assert "SECRET" not in str(caught.value)


def test_integral_floats_are_not_accepted_as_ids_or_counts() -> None:
    def ext_id(loc: Any, ext: Any, eph: Any) -> None:
        ext["items"][5]["id"] = 1005.0

    def pages(loc: Any, ext: Any, eph: Any) -> None:
        ext["meta"]["totalPages"] = 1.0

    def total(loc: Any, ext: Any, eph: Any) -> None:
        eph["meta"]["totalItems"] = 1.0

    def eph_location(loc: Any, ext: Any, eph: Any) -> None:
        eph["items"][0]["locationId"] = 2960.0

    _refuses(ext_id, "id")
    _refuses(pages, "single page")
    _refuses(total, "count")
    _refuses(eph_location, "different location")


def test_the_key_header_is_unredirected_and_no_redirect_is_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def spy(self: Any, request: urllib.request.Request, timeout: float = 0) -> Any:
        seen["unredirected"] = dict(request.unredirected_hdrs)
        seen["headers"] = dict(request.headers)
        seen["method"] = request.get_method()
        seen["handlers"] = [type(h).__name__ for h in self.handlers]
        raise urllib.error.URLError("stop")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy)
    getter = UrllibJsonGetter(allowed_hosts=TIDES_TODAY_HOSTS, auth_header=("x-api-key", "k-123"))
    with pytest.raises(ConnectorTransportError) as caught:
        asyncio.run(
            getter.get_json(f"{tt.BASE_URL}/locations/2960/ephemeris", {"filter.date": "x"})
        )
    # urllib stores header names capitalised.
    assert seen["unredirected"] == {"X-api-key": "k-123"}
    assert "X-api-key" not in seen["headers"] and "Authorization" not in seen["headers"]
    assert seen["method"] == "GET"
    assert "_NoRedirect" in seen["handlers"] and "_SafeRedirect" not in seen["handlers"]
    assert "k-123" not in str(caught.value) and "filter.date" not in str(caught.value)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    # APP_ID: a stale V1 variable must not leak into these settings either.
    for name in ("ENABLED", "APP_ID", "API_KEY"):
        monkeypatch.delenv(f"DIVESAFE_TIDES_TODAY_{name}", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(f"DIVESAFE_TIDES_TODAY_{name}", value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_it_is_off_by_default_and_needs_the_key_to_turn_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch).tides_today_enabled is False
    with pytest.raises(ValueError) as caught:
        _settings(monkeypatch, ENABLED="true")
    assert "DIVESAFE_TIDES_TODAY_API_KEY" in str(caught.value)
    on = _settings(monkeypatch, ENABLED="true", API_KEY="key-2")
    assert on.tides_today_enabled is True


def test_the_application_id_setting_is_gone() -> None:
    assert "tides_today_app_id" not in Settings.model_fields


@pytest.mark.parametrize(
    "bad", ["has:colon-SECRET", "has space-SECRET", "tok-SECRET\n", "é-SECRET", "a\r\nb-SECRET"]
)
def test_a_key_with_an_unexpected_shape_is_refused_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    with pytest.raises(ValueError) as caught:
        _settings(monkeypatch, API_KEY=bad)
    assert "SECRET" not in str(caught.value)
