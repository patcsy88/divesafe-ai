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

# Read by tests on purpose (the opt-in Postgres and live-evaluation runs); everything else from the
# developer's shell or .env must not change what a test sees.
_KEPT_ENV_PREFIXES = ("DIVESAFE_TEST_DATABASE_URL", "DIVESAFE_EVAL_")


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from divesafe.config import Settings

    for name in list(os.environ):
        if name.startswith("DIVESAFE_") and not name.startswith(_KEPT_ENV_PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


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
