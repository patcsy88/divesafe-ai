"""tides.today connector (ADR 0029): parse, every refusal, the request plan and the transport.

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
        copy.deepcopy(FIXTURE["tides"]),
        copy.deepcopy(FIXTURE["meteorology_summary"]),
        copy.deepcopy(FIXTURE["spring_neap_tides"]),
    )


class _Getter:
    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads or _payloads())
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, url: str, params: Any) -> Any:
        self.calls.append((url, dict(params)))
        return self.payloads[len(self.calls) - 1]


def _parse(tides: Any, met: Any, sn: Any, start: datetime = START, end: datetime = END):  # type: ignore[no-untyped-def]
    return tt.TidesTodayConnector(_Getter()).parse(tides, met, sn, TIOMAN_ISLAND, start, end, NOW)


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
    assert item.valid_at == datetime(2026, 11, 17, 16, tzinfo=UTC)
    assert item.valid_until == datetime(2026, 11, 18, 16, tzinfo=UTC)
    assert item.retrieved_at == NOW
    assert item.location is not None
    assert (item.location.latitude, item.location.longitude) == (2.8, 104.133003)
    assert "3.7 km" in (item.location.description or "")
    tide = item.value["tide"]
    assert tide["unit"] == "m" and "not stated by the API" in tide["unit_basis"]
    assert "not stated by the API" in tide["datum"]
    assert [e["kind"] for e in tide["extremes"]] == ["high", "low", "high", "low"]
    assert tide["extremes"][0] == {
        "time": "2026-11-18T03:40:00+08:00",
        "kind": "high",
        "height_m": 2.5,
    }
    assert all(e["time"].startswith("2026-11-18T") for e in tide["extremes"])
    assert tide["neap"] is True and tide["spring"] is False
    assert tide["moon_phase"] == {"name": "first-quarter", "percent": 55.0}
    assert item.value["daylight"] == {
        "sunrise": "2026-11-18T06:50:00+08:00",
        "sunset": "2026-11-18T18:45:00+08:00",
    }
    assert item.value["requested_location_id"] == 2960
    assert "SYNTHETIC placeholder" in item.value["provider_notice"]
    assert "tides.today" in item.value["attribution"]
    assert len(item.value["attribution"]) <= 300
    assert [url for url, _ in getter.calls] == [
        "https://api.tidestoday.io/v1/locations/2960/tides",
        "https://api.tidestoday.io/v1/locations/2960/meteorology-summary",
        "https://api.tidestoday.io/v1/spring-neap-tides",
    ]


def test_the_requests_ask_one_day_either_side_for_tides_and_inclusive_days_otherwise() -> None:
    _, getter = _fetch()
    assert getter.calls[0][1] == {
        "dateFrom": "2026-11-17T00:00:00.000Z",
        "dateTo": "2026-11-20T00:00:00.000Z",
    }
    daily = {"dateFrom": "2026-11-18T00:00:00.000Z", "dateTo": "2026-11-19T00:00:00.000Z"}
    assert getter.calls[1][1] == daily and getter.calls[2][1] == daily


def test_no_weather_number_reaches_the_item() -> None:
    (item,) = _fetch()[0].items
    text = json.dumps(item.value)
    for word in ("windSpeed", "maxTemp", "precipitation", "humidity", "pressure", "cloudCover"):
        assert word not in text


def test_the_digest_changes_with_the_values() -> None:
    first = _parse(*_payloads()).items[0].id
    tides, met, sn = _payloads()
    tides["data"][5]["height"] = 1.71
    assert _parse(tides, met, sn).items[0].id != first


# --- refusals --------------------------------------------------------------------------------


def _refuses(mutate, match: str | None = None) -> None:  # type: ignore[no-untyped-def]
    tides, met, sn = _payloads()
    mutate(tides, met, sn)
    with pytest.raises(ConnectorResponseError, match=match):
        _parse(tides, met, sn)


def _set_loc(key: str, value: Any):  # type: ignore[no-untyped-def]
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["location"][key] = value

    return mutate


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", 2961),
        ("id", "2960"),
        ("id", True),
        ("name", "Kampung Tekek"),
        ("lat", "2.900000"),
        ("lng", 104.133003),
        ("lng", "east"),
        ("copyright", ""),
        ("copyright", "x" * 2001),
        ("copyright", "line\nbreak"),
    ],
)
def test_a_different_or_malformed_location_is_refused(key: str, value: Any) -> None:
    _refuses(_set_loc(key, value))


def test_a_different_time_zone_is_refused() -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["location"]["region"]["timezone"] = "Asia/Singapore"

    _refuses(mutate, "time zone")


def test_the_meteorology_location_is_checked_too() -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        met["location"]["id"] = 1

    _refuses(mutate, "different location")


def test_highs_and_lows_that_do_not_alternate_are_refused() -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["type"] = "low"

    _refuses(mutate, "alternate")


def test_extremes_out_of_order_are_refused() -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["datetime"] = "2026-11-18T02:00:00.000Z"

    _refuses(mutate, "time order")


def test_a_long_run_of_missing_extremes_is_refused_by_the_gap_bound() -> None:
    """A whole missing day (alternation still holds) is refused. A single missing high/low pair
    can stay under the bound and is NOT detected: ADR 0029 Known limits."""

    def mutate(tides: Any, met: Any, sn: Any) -> None:
        del tides["data"][4:8]
        tides["meta"]["totalCount"] -= 4

    _refuses(mutate, "missing")


def test_a_high_below_its_neighbouring_low_is_refused() -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["height"] = 1.0

    _refuses(mutate, "above the low")


@pytest.mark.parametrize("value", [None, "1.2", True, float("nan"), float("inf"), 99.0, -5.0])
def test_a_height_that_is_not_a_plausible_number_is_refused(value: Any) -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["height"] = value

    _refuses(mutate)


@pytest.mark.parametrize("value", [True, None, "false", 0])
def test_an_estimated_or_untyped_estimated_flag_is_refused(value: Any) -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["estimated"] = value

    _refuses(mutate)


@pytest.mark.parametrize(
    "value",
    ["2026-11-18T07:20:00.000", "2026-11-18T15:20:00.000+08:00", "yesterday", 1731900000, None],
)
def test_a_time_that_is_not_utc_with_z_is_refused(value: Any) -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["datetime"] = value

    _refuses(mutate)


def test_a_repeated_or_missing_id_is_refused() -> None:
    def repeat(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["id"] = tides["data"][4]["id"]

    def missing(tides: Any, met: Any, sn: Any) -> None:
        del tides["data"][5]["id"]

    _refuses(repeat, "id")
    _refuses(missing, "id")


def test_an_unknown_kind_is_refused() -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][5]["type"] = "slack"

    _refuses(mutate, "neither high nor low")


def test_extremes_that_do_not_cover_the_whole_local_day_are_refused() -> None:
    def tail(tides: Any, met: Any, sn: Any) -> None:
        tides["data"] = tides["data"][:7]
        tides["meta"]["totalCount"] = 7

    def head(tides: Any, met: Any, sn: Any) -> None:
        tides["data"] = tides["data"][4:]
        tides["meta"]["totalCount"] = 8

    _refuses(tail, "whole day")
    _refuses(head, "whole day")


def test_an_extreme_outside_the_requested_dates_is_refused() -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        tides["data"][-1]["datetime"] = "2026-11-20T00:00:00.000Z"

    _refuses(mutate, "outside the dates")


def test_paging_or_a_count_mismatch_is_refused() -> None:
    def pages(tides: Any, met: Any, sn: Any) -> None:
        tides["meta"]["totalPages"] = 2

    def count(tides: Any, met: Any, sn: Any) -> None:
        tides["meta"]["totalCount"] = 13

    def met_pages(tides: Any, met: Any, sn: Any) -> None:
        met["meta"]["totalPages"] = 2

    _refuses(pages, "single page")
    _refuses(count, "count")
    _refuses(met_pages, "single page")


def test_a_response_for_other_dates_is_refused() -> None:
    def tides_dates(tides: Any, met: Any, sn: Any) -> None:
        tides["meta"]["params"]["dateTo"] = "2026-11-21T00:00:00.000Z"

    def sn_dates(tides: Any, met: Any, sn: Any) -> None:
        sn["meta"]["params"]["dateFrom"] = "2026-11-17T00:00:00.000Z"

    _refuses(tides_dates, "dates requested")
    _refuses(sn_dates, "dates requested")


def test_a_missing_or_repeated_daily_row_is_refused() -> None:
    def missing(tides: Any, met: Any, sn: Any) -> None:
        met["data"][0]["date"] = "2026-11-20T00:00:00.000Z"

    def repeated(tides: Any, met: Any, sn: Any) -> None:
        sn["data"][1]["date"] = sn["data"][0]["date"]

    def not_midnight(tides: Any, met: Any, sn: Any) -> None:
        met["data"][0]["date"] = "2026-11-18T08:00:00.000Z"

    _refuses(missing, "missing a needed day")
    _refuses(repeated, "repeats")
    _refuses(not_midnight, "midnight")


def test_sun_times_that_do_not_fit_the_day_are_refused() -> None:
    def swapped(tides: Any, met: Any, sn: Any) -> None:
        row = met["data"][0]
        row["sunrise"], row["sunset"] = row["sunset"], row["sunrise"]

    def other_day(tides: Any, met: Any, sn: Any) -> None:
        met["data"][0]["sunrise"] = "2026-11-16T22:50:00.000Z"

    _refuses(swapped, "sunrise")
    _refuses(other_day, "sunrise")


@pytest.mark.parametrize(
    ("key", "value"),
    [("spring", "no"), ("neap", None), ("spring", True)],
)
def test_bad_spring_neap_flags_are_refused(key: str, value: Any) -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        sn["data"][0][key] = value  # the first row is neap, so spring=True makes both

    _refuses(mutate)


@pytest.mark.parametrize(
    "phase",
    [
        {"name": "First Quarter", "percent": 50},
        {"name": "x" * 41, "percent": 50},
        {"name": "full", "percent": 101},
        {"name": "full", "percent": "50"},
        {"name": "full", "percent": float("nan")},
        "full",
    ],
)
def test_a_bad_moon_phase_is_refused(phase: Any) -> None:
    def mutate(tides: Any, met: Any, sn: Any) -> None:
        sn["data"][0]["moonPhase"] = phase

    _refuses(mutate)


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
                raise ConnectorTransportError("HTTP 500 from api.tidestoday.io/v1/x")
            return _payloads()[self.n - 1]

    for at in (1, 2, 3):
        with pytest.raises(ConnectorTransportError):
            asyncio.run(tt.TidesTodayConnector(_Fails(at)).fetch(TIOMAN_ISLAND, START, END, NOW))


def test_a_dive_across_local_midnight_gets_both_day_items() -> None:
    start = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)  # 23:30 MYT on 18 Nov
    tides, met, sn = _payloads()
    # SYNTHETIC: one more invented day (20 Nov local needs extremes into 21 Nov UTC).
    extra = [
        ("2026-11-20T04:30:00.000Z", "low", 1.40),
        ("2026-11-20T09:50:00.000Z", "high", 1.90),
        ("2026-11-20T15:10:00.000Z", "low", 1.30),
        ("2026-11-20T22:00:00.000Z", "high", 2.20),
    ]
    tides["data"] += [
        {"id": 2000 + i, "height": h, "estimated": False, "type": k, "datetime": t}
        for i, (t, k, h) in enumerate(extra)
    ]
    tides["meta"]["totalCount"] = len(tides["data"])
    tides["meta"]["params"]["dateTo"] = "2026-11-21T00:00:00.000Z"
    third = copy.deepcopy(met["data"][1])
    third.update(
        date="2026-11-20T00:00:00.000Z",
        sunrise="2026-11-19T22:51:00.000Z",
        sunset="2026-11-20T10:45:00.000Z",
    )
    met["data"].append(third)
    sn_third = copy.deepcopy(sn["data"][1])
    sn_third["date"] = "2026-11-20T00:00:00.000Z"
    sn["data"].append(sn_third)
    for body in (met, sn):
        body["meta"]["totalCount"] = 3
        body["meta"]["params"]["dateTo"] = "2026-11-20T00:00:00.000Z"
    result = _parse(tides, met, sn, start, start + timedelta(hours=1))
    assert [i.value["date"] for i in result.items] == ["2026-11-18", "2026-11-19"]


# --- transport and settings -----------------------------------------------------------------


def test_the_host_is_kept_out_of_the_shared_list_and_has_its_own() -> None:
    assert frozenset({"api.tidestoday.io"}) == TIDES_TODAY_HOSTS
    assert not TIDES_TODAY_HOSTS & ALLOWED_HOSTS
    assert not TIDES_TODAY_HOSTS & MET_API_HOSTS


def test_the_shared_getter_refuses_the_tides_today_host() -> None:
    with pytest.raises(ConnectorTransportError, match="host not allowed"):
        asyncio.run(UrllibJsonGetter().get_json(f"{tt.BASE_URL}/locations/2960/tides", {}))


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
        UrllibJsonGetter(allowed_hosts=hosts, auth_header=("Authorization", "Basic x"))


def test_the_basic_header_is_unredirected_and_no_redirect_is_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def spy(self: Any, request: urllib.request.Request, timeout: float = 0) -> Any:
        seen["unredirected"] = dict(request.unredirected_hdrs)
        seen["headers"] = dict(request.headers)
        seen["handlers"] = [type(h).__name__ for h in self.handlers]
        raise urllib.error.URLError("stop")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy)
    getter = UrllibJsonGetter(
        allowed_hosts=TIDES_TODAY_HOSTS, auth_header=("Authorization", "Basic dGVzdA==")
    )
    with pytest.raises(ConnectorTransportError) as caught:
        asyncio.run(getter.get_json(f"{tt.BASE_URL}/spring-neap-tides", {"dateFrom": "x"}))
    assert seen["unredirected"] == {"Authorization": "Basic dGVzdA=="}
    assert "Authorization" not in seen["headers"]
    assert "_NoRedirect" in seen["handlers"] and "_SafeRedirect" not in seen["handlers"]
    assert "dGVzdA" not in str(caught.value) and "dateFrom" not in str(caught.value)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in ("ENABLED", "APP_ID", "API_KEY"):
        monkeypatch.delenv(f"DIVESAFE_TIDES_TODAY_{name}", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(f"DIVESAFE_TIDES_TODAY_{name}", value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_it_is_off_by_default_and_needs_both_credentials_to_turn_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch).tides_today_enabled is False
    for env in ({"ENABLED": "true"}, {"ENABLED": "true", "APP_ID": "app-SECRET-1"}):
        with pytest.raises(ValueError) as caught:
            _settings(monkeypatch, **env)
        assert "SECRET" not in str(caught.value)
    on = _settings(monkeypatch, ENABLED="true", APP_ID="app-1", API_KEY="key-2")
    assert on.tides_today_enabled is True


@pytest.mark.parametrize(
    "bad", ["has:colon-SECRET", "has space-SECRET", "tok-SECRET\n", "é-SECRET"]
)
def test_a_credential_with_an_unexpected_shape_is_refused_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    for field in ("APP_ID", "API_KEY"):
        with pytest.raises(ValueError) as caught:
            _settings(monkeypatch, **{field: bad})
        assert "SECRET" not in str(caught.value)
