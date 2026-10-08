"""Risk Assessment agent: reads the findings, scenarios and the deterministic result and returns
a PROPOSAL. It is never the final recommendation: `reconcile` keeps the more severe of this and
the deterministic result, so a proposal can only tighten, never relax."""

from __future__ import annotations

from collections.abc import Sequence

from divesafe.agents.prompts import (
    agent_view,
    build_user_message,
    evidence_payload,
    missing_note,
)
from divesafe.agents.runner import call_structured
from divesafe.agents.schemas import ProposalOutput
from divesafe.domain import (
    DivePlan,
    EvidenceItem,
    Finding,
    Recommendation,
    RuleResult,
    Scenario,
)
from divesafe.models import LLMProvider


async def propose(
    provider: LLMProvider,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    findings: Sequence[Finding],
    scenarios: Sequence[Scenario],
    rule_results: Sequence[RuleResult],
    deterministic: Recommendation,
    missing: Sequence[str] = (),
) -> ProposalOutput:
    message = build_user_message(
        task="risk_assessment",
        instructions=(
            "Propose a recommendation (GO, CAUTION, NO-GO or INSUFFICIENT EVIDENCE) for the human "
            "decision-maker, with a short rationale citing evidence ids and a list of "
            "uncertainties. "
            f"The deterministic result is {deterministic.value} and is authoritative: you may "
            "agree or be more cautious, never less cautious. Under missing, stale or "
            "contradictory evidence prefer INSUFFICIENT EVIDENCE." + missing_note(missing)
        ),
        plan=plan,
        allowed_ids=[e.id for e in evidence],
        untrusted={
            "evidence": evidence_payload(evidence),
            "specialist_findings": agent_view(findings),
            "scenarios": agent_view(scenarios),
            "deterministic_rule_results": [
                {"rule_id": r.rule_id, "outcome": r.outcome.value, "rationale": r.rationale}
                for r in rule_results
            ],
        },
        schema=ProposalOutput.model_json_schema(),
    )
    return await call_structured(
        provider,
        task="risk_assessment",
        user_message=message,
        output=ProposalOutput,
        allowed_ids=frozenset(e.id for e in evidence),
    )
