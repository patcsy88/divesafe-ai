"""Assessment pipeline: Observe -> Assess Risk -> Propose -> (stop at the Human Gate).

No LLM is involved yet. The pipeline gathers evidence, runs the deterministic engine, reconciles
with an optional proposal and returns a `PENDING_HUMAN` `AssessmentRecord`. Failed or missing
sources never abort the assessment: they become recorded issues and force at least
INSUFFICIENT EVIDENCE, so the user sees why instead of getting nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from divesafe.agents import run_agent_stage
from divesafe.config.settings import CONNECTOR_BUDGET_SECONDS
from divesafe.data import Connector, DiveSite
from divesafe.domain import (
    AssessmentRecord,
    DivePlan,
    Finding,
    Recommendation,
    RuleResult,
    Scenario,
    most_severe,
)
from divesafe.models import LLMProvider
from divesafe.risk import RiskRulesEngine, reconcile
from divesafe.risk.engine import WINDOW_STARTED_RULE
from divesafe.services import gather_evidence

logger = logging.getLogger(__name__)


class InvalidPlanError(ValueError):
    """The dive plan is unusable (wrong site, or a window that has already started)."""


class StartPassedError(InvalidPlanError):
    """The planned start is no longer in the future; choosing a later one fixes it."""


class SiteMismatchError(InvalidPlanError):
    """The plan names a different site from the one given: a bug in the caller, not in the plan."""


class UnsafeConfigurationError(ValueError):
    """The pipeline was configured in a way that could yield a result on no evidence."""


async def assess_dive(
    *,
    assessment_id: str,
    plan: DivePlan,
    site: DiveSite,
    connectors: Sequence[Connector],
    engine: RiskRulesEngine,
    now: datetime,
    proposed: Recommendation | None = None,
    scenarios: tuple[Scenario, ...] = (),
    explanation: str = "",
    model_version: str | None = None,
    provider: LLMProvider | None = None,
    created_by: str | None = None,
    assigned_decider: str | None = None,
    clock: Callable[[], datetime] | None = None,
    connector_timeout_seconds: float = CONNECTOR_BUDGET_SECONDS,
) -> AssessmentRecord:
    if not engine.policy.required_categories:
        raise UnsafeConfigurationError("the evidence policy must require at least one category")
    if provider is not None and (proposed is not None or scenarios or explanation):
        raise ValueError("pass either a provider or explicit agent output, not both")
    if plan.site_id != site.id:
        raise SiteMismatchError("plan.site_id does not match the site")
    if provider is not None and clock is None:
        raise ValueError(
            "a clock is required with a provider, to judge the result again afterwards"
        )
    if plan.planned_start < now:
        raise StartPassedError("planned_start is in the past; assess a future dive window")

    window_end = plan.planned_start + timedelta(minutes=plan.planned_duration_minutes)
    evidence = await gather_evidence(
        connectors,
        site,
        plan.planned_start,
        window_end,
        now,
        timeout_seconds=connector_timeout_seconds,
    )

    assessment = engine.assess(plan, evidence.items, now)
    rule_results = list(assessment.rule_results)
    issue_rules: list[RuleResult] = []
    if evidence.issues:
        issue_rules.append(
            RuleResult(
                rule_id="evidence.connector_issues",
                outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                rationale="; ".join(evidence.issues),
            )
        )
    rule_results.extend(issue_rules)
    deterministic = most_severe(tuple(r.outcome for r in rule_results))

    findings: tuple[Finding, ...] = ()
    agent_issues: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    if provider is not None:
        stage = await run_agent_stage(provider, plan, evidence.items, rule_results, deterministic)
        proposed, scenarios = stage.proposed, stage.scenarios
        explanation, model_version = stage.explanation, stage.model_version
        findings, agent_issues = stage.findings, stage.issues
        proposal_ids = stage.proposal_evidence_ids

    # A slow agent stage can take minutes. Judge the same evidence again at the later time and
    # keep every earlier result as well: stale evidence, an expired rule or a dive window that has
    # started can only add problems, never remove one.
    completed = now
    unevaluated = assessment.unevaluated_factors
    if clock is not None:
        completed = max(now, clock())
        if completed > now or plan.planned_start <= completed:
            later = engine.assess(plan, evidence.items, completed)
            rule_results.extend(r for r in later.rule_results if r not in rule_results)
            if plan.planned_start <= completed:
                rule_results.append(
                    RuleResult(
                        rule_id=WINDOW_STARTED_RULE,
                        outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                        rationale="the dive start time passed while the assessment was running",
                    )
                )
            unevaluated = tuple(
                sorted(set(unevaluated) | set(later.unevaluated_factors), key=lambda f: f.value)
            )
            again = most_severe(tuple(r.outcome for r in rule_results))
            if again is not deterministic:
                agent_issues = (
                    *agent_issues,
                    "the rules were re-judged after the agent stage; agent output reflects "
                    "the earlier result",
                )
            deterministic = again

    reconciled = reconcile(deterministic, proposed)
    if reconciled.llm_attempted_downgrade and proposed is not None:
        # Do not keep persuasive prose that argues against the recorded result.
        explanation = (
            f"The LLM proposed {proposed.value}, which is less severe than the rules, "
            "so its rationale was discarded."
        )

    record = AssessmentRecord(
        id=assessment_id,
        created_at=completed,
        plan=plan,
        evidence=evidence.items,
        rule_results=tuple(rule_results),
        deterministic_recommendation=reconciled.deterministic,
        proposed_recommendation=reconciled.proposed,
        final_recommendation=reconciled.final,
        llm_attempted_downgrade=reconciled.llm_attempted_downgrade,
        confidence=None,
        evidence_issues=evidence.issues,
        unevaluated_factors=unevaluated,
        findings=findings,
        proposal_evidence_ids=proposal_ids,
        scenarios=scenarios,
        explanation=explanation,
        model_version=model_version,
        agent_issues=agent_issues,
        ruleset_version=assessment.ruleset_version,
        data_versions=evidence.data_versions,
        created_by=created_by,
        assigned_decider=assigned_decider,
    )
    logger.info(
        "assessment created",
        extra={
            "assessment_id": assessment_id,
            "final": record.final_recommendation.value,
            "issues": len(evidence.issues),
            "agent_issues": len(agent_issues),
        },
    )
    return record
