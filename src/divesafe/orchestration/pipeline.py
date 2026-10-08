"""Assessment pipeline: Observe -> Assess Risk -> Propose -> (stop at the Human Gate).

No LLM is involved yet. The pipeline gathers evidence, runs the deterministic engine, reconciles
with an optional proposal and returns a `PENDING_HUMAN` `AssessmentRecord`. Failed or missing
sources never abort the assessment: they become recorded issues and force at least
INSUFFICIENT EVIDENCE, so the user sees why instead of getting nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from divesafe.data import Connector, Site
from divesafe.domain import (
    AssessmentRecord,
    DivePlan,
    Recommendation,
    RuleResult,
    Scenario,
    most_severe,
)
from divesafe.risk import RiskRulesEngine, reconcile
from divesafe.services import gather_evidence

logger = logging.getLogger(__name__)


class InvalidPlanError(ValueError):
    """The dive plan is unusable (wrong site, or a window that has already started)."""


class UnsafeConfigurationError(ValueError):
    """The pipeline was configured in a way that could yield a result on no evidence."""


async def assess_dive(
    *,
    assessment_id: str,
    plan: DivePlan,
    site: Site,
    connectors: Sequence[Connector],
    engine: RiskRulesEngine,
    now: datetime,
    proposed: Recommendation | None = None,
    scenarios: tuple[Scenario, ...] = (),
    explanation: str = "",
    model_version: str | None = None,
) -> AssessmentRecord:
    if not engine.policy.required_categories:
        raise UnsafeConfigurationError("the evidence policy must require at least one category")
    if plan.site_id != site.id:
        raise InvalidPlanError("plan.site_id does not match the site")
    if plan.planned_start < now:
        raise InvalidPlanError("planned_start is in the past; assess a future dive window")

    window_end = plan.planned_start + timedelta(minutes=plan.planned_duration_minutes)
    evidence = await gather_evidence(connectors, site, plan.planned_start, window_end, now)

    assessment = engine.assess(plan, evidence.items, now)
    rule_results = list(assessment.rule_results)
    if evidence.issues:
        rule_results.append(
            RuleResult(
                rule_id="evidence.connector_issues",
                outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                rationale="; ".join(evidence.issues),
            )
        )
    deterministic = most_severe(tuple(r.outcome for r in rule_results))
    reconciled = reconcile(deterministic, proposed)

    record = AssessmentRecord(
        id=assessment_id,
        created_at=now,
        plan=plan,
        evidence=evidence.items,
        rule_results=tuple(rule_results),
        deterministic_recommendation=reconciled.deterministic,
        proposed_recommendation=reconciled.proposed,
        final_recommendation=reconciled.final,
        llm_attempted_downgrade=reconciled.llm_attempted_downgrade,
        confidence=None,
        evidence_issues=evidence.issues,
        scenarios=scenarios,
        explanation=explanation,
        model_version=model_version,
        ruleset_version=assessment.ruleset_version,
        data_versions=evidence.data_versions,
    )
    logger.info(
        "assessment created",
        extra={
            "assessment_id": assessment_id,
            "final": record.final_recommendation.value,
            "issues": len(evidence.issues),
        },
    )
    return record
