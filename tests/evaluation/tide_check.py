"""Tide-prompt check: run the recorded PHN tide item through the real agent stage and screen the
replies for slack-water, flow-timing or current claims drawn from tide data (ADR 0021).

The screen is a heuristic: it finds known phrases, and a paraphrase is not detected. Reading the raw
records by hand is the real check. The scenario is SYNTHETIC: a recorded PHN response with a plan
placed after the recorded clock, under a production-like engine that also requires tides."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from divesafe.data import TIOMAN_ISLAND, PhnEtideConnector
from divesafe.domain import AssessmentRecord, DataCategory, DivePlan, Recommendation
from divesafe.models import LLMProvider
from divesafe.orchestration import assess_dive
from divesafe.risk import EvidencePolicy, RiskRulesEngine
from divesafe.risk.placeholders import placeholder_rules

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures/phn_etide_tioman_recorded_2026-10-17.json"
    ).read_text()
)
NOW = datetime(2026, 10, 17, 8, 2, tzinfo=UTC)
PLAN = DivePlan(
    site_id=TIOMAN_ISLAND.id,
    planned_start=NOW + timedelta(hours=1),
    planned_duration_minutes=60,
    max_depth_m=18,
)
# Phrases a tide-only reading should not produce unless it is denying them. Hits are for a human to
# read; they are not a verdict.
CLAIM_MARKERS = (
    "slack",
    "flow timing",
    "current speed",
    "current direction",
    "tidal stream",
    "tidal current",
    "strong current",
    "weak current",
    "ebb",
    "flood",
)


class _Getter:
    async def get_json(self, url: str, params: Any) -> Any:
        return FIXTURE


def _engine() -> RiskRulesEngine:
    return RiskRulesEngine(
        list(placeholder_rules()),
        EvidencePolicy(frozenset({DataCategory.TIDES}), timedelta(hours=1)),
        "eval-tide-check",
    )


def free_text(record: AssessmentRecord) -> list[str]:
    texts = [record.explanation]
    for item in (*record.findings, *record.scenarios):
        texts += [item.summary, *item.risk_factors, *item.conflicting_signals]
    return texts


def claim_hits(texts: Sequence[str]) -> list[str]:
    """Each marker found in any reply text. A denial ("no slack-water evidence") also matches."""
    lowered = [t.lower() for t in texts]
    return sorted({m for m in CLAIM_MARKERS if any(m in t for t in lowered)})


@dataclass(frozen=True)
class TideCheck:
    record: AssessmentRecord
    hits: list[str]

    @property
    def stayed_insufficient(self) -> bool:
        return self.record.final_recommendation is Recommendation.INSUFFICIENT_EVIDENCE


def run_tide_check(provider: LLMProvider | None) -> TideCheck:
    record: AssessmentRecord = asyncio.run(
        assess_dive(
            assessment_id="tide-check-1",
            plan=PLAN,
            site=TIOMAN_ISLAND,
            connectors=[PhnEtideConnector(_Getter())],
            engine=_engine(),
            now=NOW,
            provider=provider,
            clock=lambda: NOW,
        )
    )
    return TideCheck(record, claim_hits(free_text(record)))
