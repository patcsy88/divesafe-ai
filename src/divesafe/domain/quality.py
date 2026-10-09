"""What quality an evidence item has EARNED, as opposed to what its connector claimed.

A connector sets `EvidenceItem.quality` itself, so the risk engine never trusts it directly. It
uses `effective_quality`, which caps the claim:

- UNASSESSED and DEGRADED stay as claimed.
- VALIDATED is honoured only for measured observations that record both a unit check and a
  range check and carry no quality notes. Model output, forecasts, predictions, notices and
  knowledge can never be VALIDATED by their own plausibility checks; they are capped at DEGRADED.

The caps themselves are a reviewed rule (docs/risk-model.md), not a connector decision.
"""

from __future__ import annotations

from divesafe.domain.models import EvidenceItem
from divesafe.domain.provenance import DataKind, DataQuality

UNIT_CHECK = "unit check"
RANGE_CHECK = "range check"
REQUIRED_CHECKS: frozenset[str] = frozenset({UNIT_CHECK, RANGE_CHECK})


def effective_quality(item: EvidenceItem) -> DataQuality:
    if item.quality != DataQuality.VALIDATED:
        return item.quality
    steps = {t.step for t in item.transformations}
    earned = (
        item.data_kind == DataKind.OBSERVATION
        and steps >= REQUIRED_CHECKS
        and not item.quality_notes
    )
    return DataQuality.VALIDATED if earned else DataQuality.DEGRADED
