"""The agent stage of the lifecycle: Diagnose/Reason and Propose.

At most 9 provider calls per assessment (5 specialists, 3 scenarios, 1 risk proposal), each with
a timeout, token cap and strict validation, and an overall stage deadline. Every failure is
recorded as an issue and the stage degrades to "no proposal": the deterministic result then
stands on its own. The risk agent and the scenarios are told which specialists are missing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from divesafe.agents.prompts import PROMPT_VERSION
from divesafe.agents.risk_agent import propose
from divesafe.agents.runner import MAX_EVIDENCE_ITEMS, STAGE_TIMEOUT_SECONDS, AgentError
from divesafe.agents.specialists import SPECIALISTS, run_specialist
from divesafe.agents.tot import evaluate_scenarios
from divesafe.domain import (
    DivePlan,
    EvidenceItem,
    Finding,
    Recommendation,
    RuleResult,
    Scenario,
)
from divesafe.models import LLMProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentStageResult:
    proposed: Recommendation | None
    proposal_evidence_ids: tuple[str, ...]
    findings: tuple[Finding, ...]
    scenarios: tuple[Scenario, ...]
    explanation: str
    model_version: str
    issues: tuple[str, ...]


async def _stage(
    provider: LLMProvider,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    rule_results: Sequence[RuleResult],
    deterministic: Recommendation,
    issues: list[str],
    findings: list[Finding],
    scenarios_out: list[Scenario],
) -> tuple[Recommendation | None, tuple[str, ...], str]:
    outcomes = await asyncio.gather(
        *(run_specialist(provider, spec, plan, evidence) for spec in SPECIALISTS),
        return_exceptions=True,
    )
    for spec, outcome in zip(SPECIALISTS, outcomes, strict=True):
        if isinstance(outcome, Finding):
            findings.append(outcome)
        elif isinstance(outcome, AgentError):
            issues.append(str(outcome))
        elif isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
        else:
            issues.append(f"specialist:{spec.name}: rejected ({type(outcome).__name__})")

    have = {f.agent for f in findings}
    missing = [spec.name for spec in SPECIALISTS if spec.name not in have]

    scenarios, scenario_issues = await evaluate_scenarios(
        provider, plan, evidence, findings, rule_results, missing
    )
    scenarios_out.extend(scenarios)
    issues.extend(scenario_issues)

    if not evidence or len(evidence) > MAX_EVIDENCE_ITEMS:
        issues.append("risk_assessment: skipped (no evidence, or too much for one call)")
        return None, (), ""
    try:
        proposal = await propose(
            provider, plan, evidence, findings, scenarios, rule_results, deterministic, missing
        )
    except AgentError as exc:
        issues.append(str(exc))
        return None, (), ""
    except Exception as exc:
        issues.append(f"risk_assessment: rejected ({type(exc).__name__})")
        return None, (), ""
    explanation = proposal.rationale
    if proposal.uncertainties:
        explanation += " Uncertainties: " + "; ".join(proposal.uncertainties)
    return proposal.recommendation, proposal.evidence_ids, explanation


async def run_agent_stage(
    provider: LLMProvider,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    rule_results: Sequence[RuleResult],
    deterministic: Recommendation,
) -> AgentStageResult:
    issues: list[str] = []
    findings: list[Finding] = []
    scenarios: list[Scenario] = []
    version = f"{provider.name}/{provider.model}; prompts={PROMPT_VERSION}"

    proposed: Recommendation | None = None
    cited: tuple[str, ...] = ()
    explanation = ""
    try:
        async with asyncio.timeout(STAGE_TIMEOUT_SECONDS):
            proposed, cited, explanation = await _stage(
                provider, plan, evidence, rule_results, deterministic, issues, findings, scenarios
            )
    except TimeoutError:
        issues.append("agent stage: timed out; no proposal was produced")
        proposed, cited, explanation = None, (), ""

    if issues:
        logger.warning("agent stage issues", extra={"count": len(issues)})
    return AgentStageResult(
        proposed=proposed,
        proposal_evidence_ids=cited,
        findings=tuple(findings),
        scenarios=tuple(scenarios),
        explanation=explanation,
        model_version=version,
        issues=tuple(issues),
    )
