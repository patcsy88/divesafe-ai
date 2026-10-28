"""The agent stage of the lifecycle: Diagnose/Reason and Propose.

At most 9 provider calls per assessment (5 specialists, 3 scenarios, 1 risk proposal), each with
a timeout, token cap and strict validation, and an overall stage deadline. Every failure is
recorded as an issue and the stage degrades to "no proposal": the deterministic result then
stands on its own. The risk agent and the scenarios are told which specialists are missing.

`run_agent_stage` takes private ablation switches (`specialists`, `scenarios`, `proposal`) for
the evaluation harness (ADR 0025 item 4). They are not reachable from `assess_dive`, `Settings`,
the API or the web; a skipped group is a fixed `ablation:<group>` issue and a `skipped` task.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from divesafe.agents.cross_examination import CrossOutcome, run_cross_examination
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
    ScenarioKind,
)
from divesafe.models import LLMProvider

logger = logging.getLogger(__name__)

# The issue recorded when the whole stage hits its deadline. The pipeline reads it to label the
# stage outcome; fixed text, never derived from input.
STAGE_TIMED_OUT_ISSUE = "agent stage: timed out; no proposal was produced"


TaskOutcomeKind = Literal["ok", "failed", "skipped"]
RISK_TASK = "risk_assessment"
ABLATION_ISSUE_PREFIX = "ablation:"


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
    # ADR 0025 item 5, empty unless the cross-examination round ran. Second-round findings feed the
    # proposal only; they are never passed to the scenarios.
    cross_examination_findings: tuple[Finding, ...] = ()
    cross_examination_outcomes: tuple[CrossOutcome, ...] = ()


def _task_label(label: str) -> TaskOutcomeKind:
    if label == "ok":
        return "ok"
    if label == "skipped":
        return "skipped"
    return "failed"


async def _run_specialists(
    provider: LLMProvider,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    issues: list[str],
    findings: list[Finding],
    tasks: list[TaskOutcome],
) -> None:
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
    *,
    with_specialists: bool = True,
    with_scenarios: bool = True,
    with_proposal: bool = True,
    cross_examination: bool = False,
    cross_out: list[Finding] | None = None,
    cross_outcomes: list[CrossOutcome] | None = None,
    cross_timeout: float = 0.0,
) -> tuple[Recommendation | None, tuple[str, ...], str]:
    if not with_specialists:
        issues.append(f"{ABLATION_ISSUE_PREFIX}specialists")
        tasks.extend(TaskOutcome(spec.name, "skipped") for spec in SPECIALISTS)
    else:
        await _run_specialists(provider, plan, evidence, issues, findings, tasks)

    have = {f.agent for f in findings}
    missing = [spec.name for spec in SPECIALISTS if spec.name not in have]

    # Round two (ADR 0025 item 5): one call per specialist after round one. Its findings are added
    # to the proposal's input only; the scenarios below keep round one alone.
    proposal_findings: list[Finding] = list(findings)
    if cross_examination and with_specialists and findings and cross_out is not None:
        added, outcomes, cross_issues = await run_cross_examination(
            provider, plan, evidence, findings, cross_timeout
        )
        cross_out.extend(added)
        if cross_outcomes is not None:
            cross_outcomes.extend(outcomes)
        issues.extend(cross_issues)
        proposal_findings.extend(added)

    scenarios: tuple[Scenario, ...] = ()
    if not with_scenarios:
        issues.append(f"{ABLATION_ISSUE_PREFIX}scenarios")
        tasks.extend(TaskOutcome(kind.value, "skipped") for kind in ScenarioKind)
    else:
        scenarios, scenario_issues, scenario_labels = await evaluate_scenarios_with_outcomes(
            provider, plan, evidence, findings, rule_results, missing
        )
        scenarios_out.extend(scenarios)
        issues.extend(scenario_issues)
        tasks.extend(TaskOutcome(kind.value, _task_label(label)) for kind, label in scenario_labels)

    if not with_proposal:
        issues.append(f"{ABLATION_ISSUE_PREFIX}proposal")
        tasks.append(TaskOutcome(RISK_TASK, "skipped"))
        return None, (), ""
    if not evidence or len(evidence) > MAX_EVIDENCE_ITEMS:
        issues.append("risk_assessment: skipped (no evidence, or too much for one call)")
        tasks.append(TaskOutcome(RISK_TASK, "skipped"))
        return None, (), ""
    try:
        proposal = await propose(
            provider,
            plan,
            evidence,
            proposal_findings,
            scenarios,
            rule_results,
            deterministic,
            missing,
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
    *,
    specialists: bool = True,
    scenarios: bool = True,
    proposal: bool = True,
    cross_examination: bool = False,
    cross_examination_timeout: float | None = None,
) -> AgentStageResult:
    """Run the agent stage. The keyword-only switches are private ablation hooks for the
    evaluation harness only; production callers never pass them. `cross_examination` turns on the
    second round (ADR 0025 item 5); the caller supplies its timeout from the stage budget, and the
    round never runs longer than the stage deadline."""
    issues: list[str] = []
    findings: list[Finding] = []
    scenarios_out: list[Scenario] = []
    tasks: list[TaskOutcome] = []
    cross_out: list[Finding] = []
    cross_outcomes: list[CrossOutcome] = []
    version = f"{provider.name}/{provider.model}; prompts={PROMPT_VERSION}"

    # Off: the outer deadline is today's stage timeout, exactly. On with a budget: the outer
    # deadline is that budget (ADR 0025 item 5) and is the only bound on round two. Each round-two
    # call has its own call timeout inside `call_structured`, so a slow call times out alone.
    stage_seconds = stage_timeout(provider)
    round_two = stage_seconds
    if cross_examination and cross_examination_timeout is not None:
        stage_seconds = cross_examination_timeout
        round_two = cross_examination_timeout

    proposed: Recommendation | None = None
    cited: tuple[str, ...] = ()
    explanation = ""
    try:
        async with asyncio.timeout(stage_seconds):
            proposed, cited, explanation = await _stage(
                provider,
                plan,
                evidence,
                rule_results,
                deterministic,
                issues,
                findings,
                scenarios_out,
                tasks,
                with_specialists=specialists,
                with_scenarios=scenarios,
                with_proposal=proposal,
                cross_examination=cross_examination,
                cross_out=cross_out,
                cross_outcomes=cross_outcomes,
                cross_timeout=round_two,
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
        scenarios=tuple(scenarios_out),
        explanation=explanation,
        model_version=version,
        issues=tuple(issues),
        task_outcomes=tuple(tasks),
        cross_examination_findings=tuple(cross_out),
        cross_examination_outcomes=tuple(cross_outcomes),
    )
