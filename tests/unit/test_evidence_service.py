from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from tests.conftest import make_evidence

from divesafe.data import REDANG_ISLAND, ConnectorTransportError, DiveSite, FetchResult
from divesafe.domain import DataCategory
from divesafe.services import DuplicateEvidenceError, build_evidence_set, gather_evidence

NOW = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)


class _Ok:
    name = "ok"

    def __init__(self, *categories: DataCategory) -> None:
        self._categories = categories

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        return FetchResult(tuple(make_evidence(c) for c in self._categories), ("note",))


class _Down:
    name = "down"

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        raise ConnectorTransportError("HTTP 503")


def test_failed_connector_becomes_an_issue_not_silent_absence() -> None:
    evidence = asyncio.run(
        gather_evidence([_Ok(DataCategory.WIND), _Down()], REDANG_ISLAND, NOW, NOW, NOW)
    )
    assert evidence.categories == {DataCategory.WIND}
    assert any(i.startswith("down: ConnectorTransportError") for i in evidence.issues)
    assert "ok: note" in evidence.issues


def test_unexpected_exceptions_are_not_swallowed() -> None:
    class _Bug:
        name = "bug"

        async def fetch(self, *args: object) -> FetchResult:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        asyncio.run(gather_evidence([_Bug()], REDANG_ISLAND, NOW, NOW, NOW))  # type: ignore[list-item]


def test_identical_duplicates_collapse_and_conflicts_are_rejected() -> None:
    a = make_evidence(DataCategory.WIND, speed=1)
    same = make_evidence(DataCategory.WIND, speed=1)
    different = make_evidence(DataCategory.WIND, speed=2)
    merged = build_evidence_set([("x", FetchResult((a,)), None), ("y", FetchResult((same,)), None)])
    assert len(merged.items) == 1
    with pytest.raises(DuplicateEvidenceError):
        build_evidence_set([("x", FetchResult((a,)), None), ("y", FetchResult((different,)), None)])


def test_data_versions_are_recorded_per_source() -> None:
    evidence = build_evidence_set([("x", FetchResult((make_evidence(DataCategory.WIND),)), None)])
    assert evidence.data_versions == {"test": "unversioned"}
