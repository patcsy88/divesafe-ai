"""The prompt tells the model the length limits, and they stay inside the schema's limits."""

from __future__ import annotations

import re

import pytest

from divesafe.agents.prompts import SYSTEM_PROMPT
from divesafe.agents.schemas import FindingOutput, ProposalOutput

pytestmark = pytest.mark.safety


def _max(model: type, field: str) -> int:
    metadata = model.model_fields[field].metadata  # type: ignore[attr-defined]
    return int(next(m.max_length for m in metadata if hasattr(m, "max_length")))


def _stated(label: str) -> int:
    found = re.search(rf"{label} at most (\d+) characters", " ".join(SYSTEM_PROMPT.split()))
    assert found, label
    return int(found.group(1))


def test_the_stated_limits_are_inside_the_schema_limits() -> None:
    assert _stated("every summary") <= 1000
    assert _stated("every rationale") <= 2000
    assert _stated("uncertainty") <= 300
    assert "at most 8 of each" in " ".join(SYSTEM_PROMPT.split())
    assert _max(FindingOutput, "risk_factors") >= 8 and _max(ProposalOutput, "uncertainties") >= 8
