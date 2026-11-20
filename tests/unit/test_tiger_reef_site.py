"""Tiger Reef: the registered site, its proxy tide mapping and the connectors that refuse it.

Owner decisions of 2026-11-20. The tide fixture is SYNTHETIC (tests/fixtures/README.md); no real
tides.today response is used. Rule definitions use invented synthetic person names.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from datetime import UTC, datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any

import pytest

from divesafe.data import (
    SITES,
    TIGER_REEF,
    TIOMAN_ISLAND,
    ConnectorResponseError,
    DataGovMyForecastConnector,
    MetApiMarineConnector,
    PhnEtideConnector,
)
from divesafe.data import tides_today as tt
from divesafe.orchestration import load_ruleset

SITE_ID = "my-pahang-tiger-reef"
TIOMAN_ID = "my-pahang-pulau-tioman"
RULES_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SIGNED = RULES_NOW - timedelta(hours=30)
EXPIRES = RULES_NOW + timedelta(hours=100)
FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures/tides_today_teluk_tekek_synthetic.json").read_text()
)
NOW = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
START = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=4)


DIFFERS_NOTE = (
    "the tide at the dive site may differ in time and height from this point's by an unknown amount"
)
TIGER_NOTE = "about 13.4 km from the Tiger Reef dive site (position as published by PADI)"


class _Getter:
    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, url: str, params: Any) -> Any:
        self.calls.append((url, dict(params)))
        return self.payloads[len(self.calls) - 1]


def _tides(site: Any) -> Any:
    getter = _Getter(
        copy.deepcopy(FIXTURE["location"]),
        copy.deepcopy(FIXTURE["tide_extremes"]),
        copy.deepcopy(FIXTURE["ephemeris"]),
    )
    result = asyncio.run(tt.TidesTodayConnector(getter).fetch(site, START, END, NOW))
    (item,) = result.items
    return item, getter


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(radians, (*a, *b))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 12742 * asin(sqrt(h))


def test_tiger_reef_is_registered_with_the_owners_exact_values() -> None:
    assert SITES[SITE_ID] is TIGER_REEF
    assert TIGER_REEF.id == "my-pahang-tiger-reef"
    assert TIGER_REEF.name == "Tiger Reef (dive site)"
    assert (TIGER_REEF.latitude, TIGER_REEF.longitude) == (2.89302, 104.05593)
    assert TIGER_REEF.area_names == ("Pahang", "Tioman")
    assert TIGER_REEF.coordinate_source == (
        "PADI dive-site page 'Tiger Reef' (padi.com/dive-site/malaysia/tiger-reef-2/, read "
        "2026-11-20; contributed by a PADI dive centre; PADI does not warrant its accuracy). "
        "It matches the owner's boat GPS reading (undated; supplied 2026-11-20) exactly, "
        "probably a common origin, not an independent check; not surveyed."
    )
    assert SITES[TIOMAN_ID] is TIOMAN_ISLAND
    assert TIOMAN_ISLAND.coordinate_source == (
        "Open-Meteo geocoding API (GeoNames-derived), 'Tioman Island', feature type island, "
        "Pahang, MY, GeoNames id 1734910, queried 2026-10-10. Island-level point only, not a "
        "dive site."
    )
    assert set(SITES) == {"my-pahang-pulau-tioman", SITE_ID}


def test_tiger_reef_is_tide_mapped_to_teluk_tekek_and_says_it_is_a_13_4_km_proxy() -> None:
    item, getter = _tides(TIGER_REEF)
    assert tt.LOCATIONS[SITE_ID].id == 2960
    assert [url for url, _ in getter.calls][0] == "https://api.tidestoday.io/v2/locations/2960"
    assert item.value["requested_location_id"] == 2960
    assert item.id.startswith(f"tides-today-prediction:tides:{SITE_ID}:")
    assert item.location is not None
    description = item.location.description or ""
    assert TIGER_NOTE in description
    assert "3.7" not in description and "not a dive site" in description
    scope = item.value["spatial_scope"]
    assert TIGER_NOTE in scope and "not the dive site" in scope
    assert "3.7" not in scope
    notes = " ".join(item.quality_notes)
    assert f"{TIGER_NOTE}, not for the dive site" in notes
    assert DIFFERS_NOTE in notes
    assert "3.7" not in notes


def test_tioman_still_says_3_7_km_word_for_word() -> None:
    assert tt.LOCATIONS[TIOMAN_ID].distance_note == "about 3.7 km from the site's reference point"
    item, _ = _tides(TIOMAN_ISLAND)
    assert item.location is not None
    assert (item.location.description or "") == (
        "tides.today location 2960 'Teluk Tekek', "
        "about 3.7 km from the site's reference point; not a dive site"
    )
    assert item.value["spatial_scope"] == (
        "tides.today point 2960 'Teluk Tekek', "
        "about 3.7 km from the site's reference point; not the dive site"
    )
    assert "13.4" not in " ".join(item.quality_notes)
    assert DIFFERS_NOTE in " ".join(item.quality_notes)  # on every site's items


@pytest.mark.safety
@pytest.mark.parametrize(("site_id", "location"), list(tt.LOCATIONS.items()))
def test_every_tides_location_names_its_true_distance_and_is_not_for_the_dive_site(
    site_id: str, location: tt.Location
) -> None:
    assert site_id in SITES
    site = SITES[site_id]
    note = location.distance_note
    assert note.strip()
    (km,) = re.findall(r"about ([0-9]+\.[0-9]) km", note)
    true = _haversine_km((site.latitude, site.longitude), (location.latitude, location.longitude))
    assert float(km) == round(true, 1)
    item, _ = _tides(site)
    assert note in " ".join(item.quality_notes)
    assert f"{note}, not for the dive site" in " ".join(item.quality_notes)
    assert DIFFERS_NOTE in " ".join(item.quality_notes)
    assert note in (item.location.description if item.location else "")
    assert note in item.value["spatial_scope"]


def test_tides_today_still_has_no_mapping_for_an_unregistered_site() -> None:
    other = TIGER_REEF.model_copy(update={"id": "my-pahang-elsewhere"})
    getter = _Getter()
    with pytest.raises(ConnectorResponseError, match="no tides.today location"):
        asyncio.run(tt.TidesTodayConnector(getter).fetch(other, START, END, NOW))
    assert getter.calls == []


def test_unmapped_connectors_refuse_tiger_reef_before_any_request() -> None:
    cases: list[tuple[Any, str]] = [
        (DataGovMyForecastConnector, "no forecast location is registered"),
        (MetApiMarineConnector, "no waters zone is registered"),
        (PhnEtideConnector, "no tide location is registered"),
    ]
    for connector, message in cases:
        getter = _Getter()
        with pytest.raises(ConnectorResponseError, match=message):
            asyncio.run(connector(getter).fetch(TIGER_REEF, START, END, NOW))
        assert getter.calls == []


def _person(name: str) -> dict[str, str]:
    return {"name": name, "role": "synthetic role", "qualification": "synthetic qualification"}


def _file(scope_site: str) -> str:
    """One complete, VALIDATED, SYNTHETIC definition (invented numbers and names)."""
    signed = SIGNED.isoformat()
    definition = {
        "id": "synthetic.tiger_wave_height",
        "factor": "wave_height",
        "metric": "wave_height",
        "worse_when": "higher",
        "unit": "m",
        "no_go_when": {"comparison": ">", "value": 3.0},
        "caution_when": {"comparison": ">", "value": 2.0},
        "go_when": {"comparison": "<=", "value": 2.0},
        "forecast_margin": 0.5,
        "scope": {"site_ids": [scope_site], "max_depth_m": 30},
        "source": {
            "document": "SYNTHETIC test document",
            "version_or_date": "v0",
            "location_in_document": "s0",
            "publicly_available": False,
        },
        "document_version": "synthetic-v0",
        "status": "VALIDATED",
        "prepared_by": _person("Synthetic Preparer"),
        "reviews": [{"reviewer": _person("Synthetic Reviewer"), "reviewed_on": signed}],
        "readback_confirmed_by": _person("Synthetic Preparer"),
        "readback_confirmed_on": signed,
        "signed_off_on": signed,
        "expires_on": EXPIRES.isoformat(),
    }
    return json.dumps({"ruleset_version": "synthetic-tiger-1", "definitions": [definition]})


def test_a_synthetic_rule_scoped_to_tiger_reef_loads_and_an_unregistered_id_is_refused() -> None:
    ok = load_ruleset(_file(SITE_ID), RULES_NOW, known_sites=SITES.keys())
    assert ok.is_clean and len(ok.rules) == 1
    bad = load_ruleset(_file("my-pahang-tiger-reeff"), RULES_NOW, known_sites=SITES.keys())
    assert not bad.is_clean and bad.rules == ()
    assert "not registered" in bad.refused[0].reason
