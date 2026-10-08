"""Bounded Tree-of-Thought scenario evaluation (docs/tot-design.md).

Exactly three scenarios (favourable, marginal, deteriorating), one level deep, one validated
call each. The scenario kind is assigned here, never taken from model output. A scenario that
fails validation is dropped and reported; nothing is repaired. Only concise summaries with
evidence ids are kept. Scenarios feed the risk agent as context and can never decide an outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from divesafe.agents.prompts import (
    agent_view,
    build_user_message,
    evidence_payload,
    missing_note,
)
from divesafe.agents.runner import MAX_EVIDENCE_ITEMS, AgentError, call_structured
from divesafe.agents.schemas import ScenarioOutput
from divesafe.domain import DivePlan, EvidenceItem, Finding, RuleResult, Scenario, ScenarioKind
from divesafe.models import LLMProvider

_KIND_BRIEF: dict[ScenarioKind, str] = {
    ScenarioKind.FAVOURABLE: (
        "Favourable: the forecasts verify at the benign end of their uncertainty."
    ),
    ScenarioKind.MARGINAL: (
        "Marginal: the central expectation, with factors that may sit near limits."
    ),
    ScenarioKind.DETERIORATING: (
        "Deteriorating: the forecasts verify at the adverse end, or timing slips."
    ),
}
KINDS: tuple[ScenarioKind, ...] = tuple(ScenarioKind)


async def _one(
    provider: LLMProvider,
    kind: ScenarioKind,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    findings: Sequence[Finding],
    rule_results: Sequence[RuleResult],
    missing: Sequence[str],
) -> Scenario:
    task = f"scenario:{kind.value}"
    message = build_user_message(
        task=task,
        instructions=(
            f"Describe ONE scenario. {_KIND_BRIEF[kind]} Compare the evidence, risk factors, "
            "conflicting signals, forecast uncertainty, data freshness and expected trend. "
            "Summarise concisely with evidence ids. Do not decide whether the dive is safe."
            + missing_note(missing)
        ),
        plan=plan,
        allowed_ids=[e.id for e in evidence],
        untrusted={
            "evidence": evidence_payload(evidence),
            "specialist_findings": agent_view(findings),
            "deterministic_rule_results": [
                {"rule_id": r.rule_id, "outcome": r.outcome.value, "rationale": r.rationale}
                for r in rule_results
            ],
        },
        schema=ScenarioOutput.model_json_schema(),
    )
    out = await call_structured(
        provider,
        task=task,
        user_message=message,
        output=ScenarioOutput,
        allowed_ids=frozenset(e.id for e in evidence),
    )
    return Scenario(
        kind=kind,
        summary=out.summary,
        evidence_ids=out.evidence_ids,
        risk_factors=out.risk_factors,
        conflicting_signals=out.conflicting_signals,
        confidence=out.confidence,
    )


async def evaluate_scenarios(
    provider: LLMProvider,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    findings: Sequence[Finding],
    rule_results: Sequence[RuleResult],
    missing: Sequence[str] = (),
) -> tuple[tuple[Scenario, ...], tuple[str, ...]]:
    if not evidence:
        return (), ("scenarios: skipped because there is no evidence",)
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        return (), ("scenarios: skipped because there is too much evidence for one call",)

    results = await asyncio.gather(
        *(_one(provider, k, plan, evidence, findings, rule_results, missing) for k in KINDS),
        return_exceptions=True,
    )
    scenarios: list[Scenario] = []
    issues: list[str] = []
    for kind, result in zip(KINDS, results, strict=True):
        if isinstance(result, Scenario):
            scenarios.append(result)
        elif isinstance(result, AgentError):
            issues.append(str(result))
        elif isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result  # cancellation and interpreter exits must propagate
        else:
            issues.append(f"scenario:{kind.value}: rejected ({type(result).__name__})")
    return tuple(scenarios), tuple(issues)
