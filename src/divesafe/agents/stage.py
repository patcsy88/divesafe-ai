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
from typing import Literal

from divesafe.agents.prompts import PROMPT_VERSION
from divesafe.agents.risk_agent import propose
from divesafe.agents.runner import MAX_EVIDENCE_ITEMS, AgentError, stage_timeout
from divesafe.agents.specialists import SPECIALISTS, run_specialist
from divesafe.agents.tot import evaluate_scenarios_with_outcomes
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

# The issue recorded when the whole stage hits its deadline. The pipeline reads it to label the
# stage outcome; fixed text, never derived from input.
STAGE_TIMED_OUT_ISSUE = "agent stage: timed out; no proposal was produced"


TaskOutcomeKind = Literal["ok", "failed", "skipped"]
RISK_TASK = "risk_assessment"


@dataclass(frozen=True)
class TaskOutcome:
    """How one agent task ended, as a fixed label: the structured source for the stage trace.
    `task` is a specialist name, a scenario kind or `risk_assessment`, all set in code."""

    task: str
    outcome: TaskOutcomeKind


@dataclass(frozen=True)
class AgentStageResult:
    proposed: Recommendation | None
    proposal_evidence_ids: tuple[str, ...]
    findings: tuple[Finding, ...]
    scenarios: tuple[Scenario, ...]
    explanation: str
    model_version: str
    issues: tuple[str, ...]
    # One per task that ran or was skipped; a task cut off by the stage deadline is absent (the
    # stage-level timeout issue covers it).
    task_outcomes: tuple[TaskOutcome, ...] = ()


def _task_label(label: str) -> TaskOutcomeKind:
    if label == "ok":
        return "ok"
    if label == "skipped":
        return "skipped"
    return "failed"


async def _stage(
    provider: LLMProvider,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    rule_results: Sequence[RuleResult],
    deterministic: Recommendation,
    issues: list[str],
    findings: list[Finding],
    scenarios_out: list[Scenario],
    tasks: list[TaskOutcome],
) -> tuple[Recommendation | None, tuple[str, ...], str]:
    outcomes = await asyncio.gather(
        *(run_specialist(provider, spec, plan, evidence) for spec in SPECIALISTS),
        return_exceptions=True,
    )
    for spec, outcome in zip(SPECIALISTS, outcomes, strict=True):
        if isinstance(outcome, Finding):
            findings.append(outcome)
            tasks.append(TaskOutcome(spec.name, "ok"))
        elif isinstance(outcome, AgentError):
            issues.append(str(outcome))
            tasks.append(TaskOutcome(spec.name, "failed"))
        elif isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
        else:
            issues.append(f"specialist:{spec.name}: rejected ({type(outcome).__name__})")
            tasks.append(TaskOutcome(spec.name, "failed"))

    have = {f.agent for f in findings}
    missing = [spec.name for spec in SPECIALISTS if spec.name not in have]

    scenarios, scenario_issues, scenario_labels = await evaluate_scenarios_with_outcomes(
        provider, plan, evidence, findings, rule_results, missing
    )
    scenarios_out.extend(scenarios)
    issues.extend(scenario_issues)
    tasks.extend(TaskOutcome(kind.value, _task_label(label)) for kind, label in scenario_labels)

    if not evidence or len(evidence) > MAX_EVIDENCE_ITEMS:
        issues.append("risk_assessment: skipped (no evidence, or too much for one call)")
        tasks.append(TaskOutcome(RISK_TASK, "skipped"))
        return None, (), ""
    try:
        proposal = await propose(
            provider, plan, evidence, findings, scenarios, rule_results, deterministic, missing
        )
    except AgentError as exc:
        issues.append(str(exc))
        tasks.append(TaskOutcome(RISK_TASK, "failed"))
        return None, (), ""
    except Exception as exc:
        issues.append(f"risk_assessment: rejected ({type(exc).__name__})")
        tasks.append(TaskOutcome(RISK_TASK, "failed"))
        return None, (), ""
    tasks.append(TaskOutcome(RISK_TASK, "ok"))
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
    tasks: list[TaskOutcome] = []
    version = f"{provider.name}/{provider.model}; prompts={PROMPT_VERSION}"

    proposed: Recommendation | None = None
    cited: tuple[str, ...] = ()
    explanation = ""
    try:
        async with asyncio.timeout(stage_timeout(provider)):
            proposed, cited, explanation = await _stage(
                provider,
                plan,
                evidence,
                rule_results,
                deterministic,
                issues,
                findings,
                scenarios,
                tasks,
            )
    except TimeoutError:
        issues.append(STAGE_TIMED_OUT_ISSUE)
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
        task_outcomes=tuple(tasks),
    )
