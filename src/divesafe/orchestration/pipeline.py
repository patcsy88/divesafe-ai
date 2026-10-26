"""Assessment pipeline: the stage graph in `graph.py`, walked in order, stopping at the gate.

Observe (connectors) -> Retrieve (placeholder) -> Assess (engine) -> Reason (agents) -> Rejudge
(engine again at the later clock) -> Propose (`reconcile`) -> Supervise (code-only checks) ->
Gate (a `PENDING_HUMAN` record). Failed or missing sources never abort the assessment: they
become recorded issues and force at least INSUFFICIENT EVIDENCE, so the user sees why instead of
getting nothing. `STAGES` decides which stage follows which; this module only supplies the work.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from divesafe.agents import STAGE_TIMED_OUT_ISSUE, run_agent_stage
from divesafe.config.settings import CONNECTOR_BUDGET_SECONDS
from divesafe.data import Connector, DiveSite
from divesafe.domain import (
    AssessmentRecord,
    DivePlan,
    Finding,
    Recommendation,
    RiskFactorKind,
    RuleResult,
    Scenario,
    StageName,
    StageOutcome,
    SupervisorVerdict,
    most_severe,
)
from divesafe.models import LLMProvider
from divesafe.orchestration.graph import STAGES, next_stage
from divesafe.orchestration.supervisor import supervise
from divesafe.risk import Reconciliation, RiskRulesEngine, reconcile
from divesafe.risk.engine import WINDOW_STARTED_RULE
from divesafe.services import EvidenceSet, gather_evidence

logger = logging.getLogger(__name__)

_REJUDGED_NOTICE = (
    "the rules were re-judged after the agent stage; agent output reflects the earlier result"
)
_SUPERVISOR_NOTICE = (
    "the supervisor tightened the result after the agent stage; agent output reflects the "
    "earlier result"
)


class InvalidPlanError(ValueError):
    """The dive plan is unusable (wrong site, or a window that has already started)."""


class StartPassedError(InvalidPlanError):
    """The planned start is no longer in the future; choosing a later one fixes it."""


class SiteMismatchError(InvalidPlanError):
    """The plan names a different site from the one given: a bug in the caller, not in the plan."""


class UnsafeConfigurationError(ValueError):
    """The pipeline was configured in a way that could yield a result on no evidence."""


@dataclass
class _Run:
    """What one walk through the graph has produced so far. Mutable, local to one assessment."""

    assessment_id: str
    plan: DivePlan
    site: DiveSite
    connectors: Sequence[Connector]
    engine: RiskRulesEngine
    now: datetime
    provider: LLMProvider | None
    clock: Callable[[], datetime] | None
    connector_timeout_seconds: float
    created_by: str | None
    assigned_decider: str | None
    # Agent output, either given by the caller or produced by the Reason stage.
    proposed: Recommendation | None
    scenarios: tuple[Scenario, ...]
    explanation: str
    model_version: str | None
    # Produced by the stages.
    evidence: EvidenceSet | None = None
    rule_results: list[RuleResult] = field(default_factory=list)
    deterministic: Recommendation = Recommendation.INSUFFICIENT_EVIDENCE
    unevaluated: tuple[RiskFactorKind, ...] = ()
    ruleset_version: str = ""
    findings: tuple[Finding, ...] = ()
    agent_issues: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    completed: datetime | None = None
    reconciled: Reconciliation | None = None
    verdict: SupervisorVerdict | None = None
    record: AssessmentRecord | None = None

    @property
    def evidence_set(self) -> EvidenceSet:
        assert self.evidence is not None  # produced by Observe, required by the graph
        return self.evidence

    @property
    def reconciliation(self) -> Reconciliation:
        assert self.reconciled is not None  # produced by Propose, required by the graph
        return self.reconciled


async def _observe(run: _Run) -> StageOutcome:
    window_end = run.plan.planned_start + timedelta(minutes=run.plan.planned_duration_minutes)
    run.evidence = await gather_evidence(
        run.connectors,
        run.site,
        run.plan.planned_start,
        window_end,
        run.now,
        timeout_seconds=run.connector_timeout_seconds,
    )
    return StageOutcome.COMPLETED


async def _retrieve(run: _Run) -> StageOutcome:
    # Placeholder until RAG stage 1 (ADR 0022). Context only; never live conditions.
    return StageOutcome.NOT_CONFIGURED


async def _assess(run: _Run) -> StageOutcome:
    engine, plan, evidence, now = run.engine, run.plan, run.evidence_set, run.now
    assessment = engine.assess(plan, evidence.items, now)
    run.rule_results = list(assessment.rule_results)
    if evidence.issues:
        run.rule_results.append(
            RuleResult(
                rule_id="evidence.connector_issues",
                outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                rationale="; ".join(evidence.issues),
            )
        )
    run.deterministic = most_severe(tuple(r.outcome for r in run.rule_results))
    run.unevaluated = assessment.unevaluated_factors
    run.ruleset_version = assessment.ruleset_version
    return StageOutcome.COMPLETED


async def _reason(run: _Run) -> StageOutcome:
    provider = run.provider
    if provider is None:
        return StageOutcome.SKIPPED
    plan, evidence, rule_results, deterministic = (
        run.plan,
        run.evidence_set,
        run.rule_results,
        run.deterministic,
    )
    stage = await run_agent_stage(provider, plan, evidence.items, rule_results, deterministic)
    run.proposed, run.scenarios = stage.proposed, stage.scenarios
    run.explanation, run.model_version = stage.explanation, stage.model_version
    run.findings, run.agent_issues = stage.findings, stage.issues
    run.proposal_ids = stage.proposal_evidence_ids
    if STAGE_TIMED_OUT_ISSUE in stage.issues:
        return StageOutcome.TIMED_OUT
    return StageOutcome.COMPLETED


async def _rejudge(run: _Run) -> StageOutcome:
    # A slow agent stage can take minutes. Judge the same evidence again at the later time and
    # keep every earlier result as well: stale evidence, an expired rule or a dive window that has
    # started can only add problems, never remove one.
    run.completed = run.now
    if run.clock is None:
        return StageOutcome.COMPLETED
    run.completed = max(run.now, run.clock())
    if run.completed > run.now or run.plan.planned_start <= run.completed:
        later = run.engine.assess(run.plan, run.evidence_set.items, run.completed)
        run.rule_results.extend(r for r in later.rule_results if r not in run.rule_results)
        if run.plan.planned_start <= run.completed:
            run.rule_results.append(
                RuleResult(
                    rule_id=WINDOW_STARTED_RULE,
                    outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                    rationale="the dive start time passed while the assessment was running",
                )
            )
        run.unevaluated = tuple(
            sorted(set(run.unevaluated) | set(later.unevaluated_factors), key=lambda f: f.value)
        )
        again = most_severe(tuple(r.outcome for r in run.rule_results))
        if again is not run.deterministic:
            run.agent_issues = (*run.agent_issues, _REJUDGED_NOTICE)
        run.deterministic = again
    return StageOutcome.COMPLETED


def _reconcile(run: _Run) -> None:
    """`reconcile` is the only producer of the final recommendation; the explanation of a
    discarded downgrade is fixed text, so re-running this after a tightening is safe."""
    run.reconciled = reconcile(run.deterministic, run.proposed)
    if run.reconciled.llm_attempted_downgrade and run.proposed is not None:
        # Do not keep persuasive prose that argues against the recorded result.
        run.explanation = (
            f"The LLM proposed {run.proposed.value}, which is less severe than the rules, "
            "so its rationale was discarded."
        )


async def _propose(run: _Run) -> StageOutcome:
    _reconcile(run)
    return StageOutcome.COMPLETED


async def _supervise(run: _Run) -> StageOutcome:
    evidence = run.evidence_set
    present = {item.category for item in evidence.items}
    supervision = supervise(
        reconciled=run.reconciliation,
        proposal_evidence_ids=run.proposal_ids,
        finding_evidence_ids=(i for f in run.findings for i in f.evidence_ids),
        scenario_evidence_ids=(i for s in run.scenarios for i in s.evidence_ids),
        evidence_ids=(item.id for item in evidence.items),
        model_version=run.model_version,
        provider_ran=run.provider is not None,
        missing_required_categories=run.engine.policy.required_categories - present,
    )
    tightened_to: Recommendation | None = None
    if supervision.failed:
        before = run.reconciliation.final
        run.rule_results.extend(supervision.rule_results)
        run.deterministic = most_severe(tuple(r.outcome for r in run.rule_results))
        _reconcile(run)
        if run.reconciliation.final is not before:
            tightened_to = run.reconciliation.final
            if _SUPERVISOR_NOTICE not in run.agent_issues:
                run.agent_issues = (*run.agent_issues, _SUPERVISOR_NOTICE)
    run.verdict = SupervisorVerdict(
        passed=supervision.passed, failed=supervision.failed, tightened_to=tightened_to
    )
    return StageOutcome.COMPLETED


async def _gate(run: _Run) -> StageOutcome:
    evidence = run.evidence_set
    reconciled = run.reconciliation
    assert run.completed is not None  # set by Rejudge, required by the graph
    run.record = AssessmentRecord(
        id=run.assessment_id,
        created_at=run.completed,
        plan=run.plan,
        evidence=evidence.items,
        withheld_evidence=evidence.withheld,
        rule_results=tuple(run.rule_results),
        deterministic_recommendation=reconciled.deterministic,
        proposed_recommendation=reconciled.proposed,
        final_recommendation=reconciled.final,
        llm_attempted_downgrade=reconciled.llm_attempted_downgrade,
        confidence=None,
        evidence_issues=evidence.issues,
        unevaluated_factors=run.unevaluated,
        findings=run.findings,
        proposal_evidence_ids=run.proposal_ids,
        scenarios=run.scenarios,
        explanation=run.explanation,
        model_version=run.model_version,
        agent_issues=run.agent_issues,
        ruleset_version=run.ruleset_version,
        data_versions=evidence.data_versions,
        created_by=run.created_by,
        assigned_decider=run.assigned_decider,
        supervisor_verdict=run.verdict,
    )
    return StageOutcome.COMPLETED


# One unit of work per stage, keyed by the graph's names. The graph, not this table, decides
# the order: `assess_dive` walks `STAGES` through `next_stage`.
STAGE_WORK: dict[StageName, Callable[[_Run], Awaitable[StageOutcome]]] = {
    StageName.OBSERVE: _observe,
    StageName.RETRIEVE: _retrieve,
    StageName.ASSESS: _assess,
    StageName.REASON: _reason,
    StageName.REJUDGE: _rejudge,
    StageName.PROPOSE: _propose,
    StageName.SUPERVISE: _supervise,
    StageName.GATE: _gate,
}
if set(STAGE_WORK) != {stage.name for stage in STAGES}:
    raise RuntimeError("every stage in the graph needs exactly one unit of work")


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

    run = _Run(
        assessment_id=assessment_id,
        plan=plan,
        site=site,
        connectors=connectors,
        engine=engine,
        now=now,
        provider=provider,
        clock=clock,
        connector_timeout_seconds=connector_timeout_seconds,
        created_by=created_by,
        assigned_decider=assigned_decider,
        proposed=proposed,
        scenarios=scenarios,
        explanation=explanation,
        model_version=model_version,
    )
    current: StageName | None = STAGES[0].name
    visited: list[StageName] = []
    while current is not None:
        # The graph is linear and finite; a future edit that routes back or loops must never
        # spin, so the walk is capped at one visit per stage.
        if current in visited or len(visited) >= len(STAGES):
            raise RuntimeError("the stage graph revisited a stage; the assessment was stopped")
        visited.append(current)
        outcome = await STAGE_WORK[current](run)
        current = next_stage(current, outcome)
    if run.record is None:
        # Unreachable while every engine stage either completes or raises; kept so a graph edit
        # that lets a stage end early can never yield a record it did not build.
        raise RuntimeError("the assessment stopped before the human gate")

    record = run.record
    logger.info(
        "assessment created",
        extra={
            "assessment_id": assessment_id,
            "final": record.final_recommendation.value,
            "issues": len(run.evidence_set.issues),
            "agent_issues": len(run.agent_issues),
        },
    )
    return record
