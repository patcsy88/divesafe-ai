"""A scripted fake LLM and a pipeline harness for agent tests (no live provider calls)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from divesafe.data import (
    TIOMAN_ISLAND,
    DataGovMyWarningConnector,
    OpenMeteoMarineConnector,
)
from divesafe.domain import (
    AssessmentRecord,
    DataCategory,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RuleResult,
)
from divesafe.models import FakeProvider, LLMRequest
from divesafe.orchestration import assess_dive
from divesafe.risk import EvidencePolicy, RiskRulesEngine, WarningNeedsHumanReadingRule

R = Recommendation
FIXTURES = Path(__file__).parent.parent / "fixtures"
MARINE = json.loads((FIXTURES / "open_meteo_marine_tioman_recorded_2026-10-08.json").read_text())
WARNINGS = json.loads((FIXTURES / "data_gov_my_warning_recorded_2026-10-09.json").read_text())
NOW = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
START = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
PLAN = DivePlan(
    site_id=TIOMAN_ISLAND.id, planned_start=START, planned_duration_minutes=120, max_depth_m=18
)
LIVE = frozenset(
    {
        DataCategory.WAVES_SWELL,
        DataCategory.CURRENTS,
        DataCategory.SEA_TEMPERATURE,
        DataCategory.MARINE_WARNINGS,
    }
)

Reply = str | Callable[[LLMRequest], str]


def task_of(request: LLMRequest) -> str:
    match = re.search(r"^TASK: (.+)$", request.messages[1].content, re.MULTILINE)
    assert match, "request has no TASK line"
    return match.group(1)


def allowed_ids(request: LLMRequest) -> list[str]:
    match = re.search(r"^ALLOWED_EVIDENCE_IDS: (.+)$", request.messages[1].content, re.MULTILINE)
    assert match, "request has no ALLOWED_EVIDENCE_IDS line"
    ids: list[str] = json.loads(match.group(1))
    return ids


def scripted(
    recommendation: str = "INSUFFICIENT EVIDENCE", overrides: Mapping[str, Reply] | None = None
) -> Callable[[LLMRequest], str]:
    """Valid replies for every task; `overrides` replaces the reply for a given task name."""
    overrides = overrides or {}

    def respond(request: LLMRequest) -> str:
        task = task_of(request)
        if task in overrides:
            reply = overrides[task]
            return reply(request) if callable(reply) else reply
        ids = allowed_ids(request)[:2]
        if task.startswith("specialist:"):
            body: dict[str, Any] = {
                "summary": f"{task} reading of the cited evidence.",
                "risk_factors": ["example risk factor"],
                "conflicting_signals": [],
                "evidence_ids": ids,
                "confidence": 0.6,
            }
        elif task.startswith("scenario:"):
            body = {
                "summary": f"{task} outcome.",
                "risk_factors": [],
                "conflicting_signals": [],
                "evidence_ids": ids,
                "confidence": 0.5,
            }
        else:
            body = {
                "recommendation": recommendation,
                "rationale": "Rationale citing the evidence.",
                "evidence_ids": ids,
                "uncertainties": ["model resolution"],
            }
        return json.dumps(body)

    return respond


class _GoRule:
    rule_id = "synthetic.go"
    citation = "test fixture (synthetic, not a real safety source)"

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        return RuleResult(rule_id=self.rule_id, outcome=R.GO, rationale="synthetic")


def engine(*, with_warning_rule: bool) -> RiskRulesEngine:
    rules: list[Any] = [_GoRule()]
    if with_warning_rule:
        rules.append(WarningNeedsHumanReadingRule())
    return RiskRulesEngine(
        rules,
        EvidencePolicy(LIVE, timedelta(hours=1), degraded_may_support_go=True),  # synthetic GO
        "synthetic",
        required_factors=frozenset(),  # synthetic GO rules; real rulesets keep the default
    )


class _Getter:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        return self._payload


def run_pipeline(
    provider: Any,
    *,
    warnings: Any = WARNINGS,
    with_warning_rule: bool = True,
    connectors: Sequence[Any] | None = None,
    risk_engine: RiskRulesEngine | None = None,
    **extra: Any,
) -> AssessmentRecord:
    used = (
        connectors
        if connectors is not None
        else [
            OpenMeteoMarineConnector(_Getter(MARINE)),
            DataGovMyWarningConnector(_Getter(warnings)),
        ]
    )
    return asyncio.run(
        assess_dive(
            assessment_id="agent-1",
            plan=PLAN,
            site=TIOMAN_ISLAND,
            connectors=used,
            engine=risk_engine or engine(with_warning_rule=with_warning_rule),
            now=NOW,
            provider=provider,
            **extra,
        )
    )


def fake(**kwargs: Any) -> FakeProvider:
    reply = scripted(**kwargs)
    return FakeProvider(reply)
