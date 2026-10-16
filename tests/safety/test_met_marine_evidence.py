"""MET daily marine ranges must never act as an hourly reading, a limit input or a point value."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from divesafe.agents.prompts import evidence_payload
from divesafe.data import TIOMAN_ISLAND, MetApiMarineConnector
from divesafe.domain import observation_from_evidence

pytestmark = pytest.mark.safety

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures/met_api_marine_tioman_recorded_2026-10-16.json"
    ).read_text()
)


class _Getter:
    async def get_json(self, url: str, params: object) -> object:
        return FIXTURE


def _items():  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
    start = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
    end = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    return asyncio.run(MetApiMarineConnector(_Getter()).fetch(TIOMAN_ISLAND, start, end, now)).items


def test_met_items_have_no_typed_reading() -> None:
    for item in _items():
        assert observation_from_evidence(item) is None


def test_met_items_reach_the_agents_with_their_range_and_never_as_empty_conditions() -> None:
    for payload in evidence_payload(_items()):
        assert payload.get("conditions") != {}
        ranges = payload["value"]["ranges"]
        (entry,) = ranges.values()
        assert entry["low"] < entry["high"] and entry["unit"] in {"m", "km/h"}
        assert payload["value"]["temporal_scope"].startswith("one value for the whole calendar day")
        assert payload["upstream_known"] is False and payload["quality"] == "degraded"


def test_met_values_are_never_stored_under_the_hourly_keys() -> None:
    for item in _items():
        assert "variables" not in item.value and "units" not in item.value
        assert "worst_case" not in item.value["ranges"].get("wave_height", {})
