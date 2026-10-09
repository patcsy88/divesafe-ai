from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from divesafe.domain import (
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    EvidenceItem,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def plan() -> DivePlan:
    return DivePlan(
        site_id="site-1",
        planned_start=NOW + timedelta(hours=2),
        planned_duration_minutes=45,
        max_depth_m=18,
    )


def make_evidence(
    category: DataCategory, *, age: timedelta = timedelta(minutes=5), **value: object
) -> EvidenceItem:
    return EvidenceItem(
        id=f"ev-{category.value}",
        category=category,
        source="test",
        retrieved_at=NOW - age,
        valid_at=NOW,
        valid_until=NOW + timedelta(days=1),  # synthetic: covers any test dive window
        is_forecast=True,
        data_kind=DataKind.FORECAST,  # synthetic test fixture
        quality=DataQuality.DEGRADED,  # a forecast can never earn VALIDATED
        value=dict(value),
    )
