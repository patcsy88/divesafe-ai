"""The Supervise stage: code-only checks on the reconciled result (ADR 0025 item 2).

Every check duplicates something an upstream layer already enforces (the runner's id check,
`reconcile`, the engine's evidence policy, the agent stage's version string). The duplication is
deliberate: the verdict is a recorded, inspectable second line of defence. Nothing here produces
a recommendation; a failed tightening check adds a rule result and the pipeline calls
`reconcile` again, so `final_recommendation` still comes only from `reconcile`.

Two classes of failure. `reconcile_tighten_only` and `severity_monotonic` are invariants: a
breach raises `SupervisorInvariantError` and nothing is stored. The other three tighten.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from divesafe.agents import PROMPT_VERSION
from divesafe.domain import (
    INVARIANT_CHECKS,
    DataCategory,
    Recommendation,
    RuleResult,
    SupervisorCheck,
    severity,
    supervisor_rule_id,
)
from divesafe.risk import Reconciliation

_INVARIANT_MESSAGE = "supervisor invariant breached; the assessment was not recorded"

# Fixed rationale text per tightening check. Never derived from input.
_RATIONALE: dict[SupervisorCheck, str] = {
    SupervisorCheck.CITATIONS_VALID: (
        "Agent output cites evidence ids that are not in the evidence list; the agent output "
        "cannot be relied on."
    ),
    SupervisorCheck.PROMPT_VERSION_PINNED: (
        "A model ran but the record does not pin the prompt version; the agent output cannot be "
        "reproduced."
    ),
    SupervisorCheck.REQUIRED_CATEGORIES_PRESENT: (
        "A required evidence category has no evidence item, yet the rules did not report "
        "insufficient evidence."
    ),
}


class SupervisorInvariantError(RuntimeError):
    """`reconcile` or the severity order was violated. Fixed message; nothing is stored."""

    def __init__(self) -> None:
        super().__init__(_INVARIANT_MESSAGE)


@dataclass(frozen=True)
class Supervision:
    """The checks' result. `rule_results` are the `supervisor.<check>` results to append, one per
    failed check; the pipeline reconciles again and records `tightened_to` on the verdict."""

    passed: tuple[SupervisorCheck, ...]
    failed: tuple[SupervisorCheck, ...]
    rule_results: tuple[RuleResult, ...]


def _tighten_only(reconciled: Reconciliation) -> bool:
    final, deterministic, proposed = (
        reconciled.final,
        reconciled.deterministic,
        reconciled.proposed,
    )
    if severity(final) < severity(deterministic):
        return False
    if proposed is not None and severity(final) < severity(proposed):
        return False
    expected_downgrade = proposed is not None and severity(proposed) < severity(deterministic)
    return reconciled.llm_attempted_downgrade == expected_downgrade


def supervise(
    *,
    reconciled: Reconciliation,
    proposal_evidence_ids: Iterable[str],
    finding_evidence_ids: Iterable[str],
    scenario_evidence_ids: Iterable[str],
    evidence_ids: Iterable[str],
    model_version: str | None,
    provider_ran: bool,
    missing_required_categories: Iterable[DataCategory],
) -> Supervision:
    """Run the five checks. Pure: no I/O, no clock, no model.

    `evidence_ids` are the ids of the evaluated evidence only (withheld items are not citable).
    `missing_required_categories` are the policy's required categories with no evidence item.
    """
    results: dict[SupervisorCheck, bool] = {
        SupervisorCheck.RECONCILE_TIGHTEN_ONLY: _tighten_only(reconciled),
        SupervisorCheck.SEVERITY_MONOTONIC: (
            severity(reconciled.final) >= severity(reconciled.deterministic)
        ),
    }
    if any(not results[check] for check in INVARIANT_CHECKS):
        raise SupervisorInvariantError()

    known = set(evidence_ids)
    cited = set(proposal_evidence_ids) | set(finding_evidence_ids) | set(scenario_evidence_ids)
    results[SupervisorCheck.CITATIONS_VALID] = cited <= known

    pinned = model_version is not None and f"prompts={PROMPT_VERSION}" in model_version
    results[SupervisorCheck.PROMPT_VERSION_PINNED] = pinned or not provider_ran

    results[SupervisorCheck.REQUIRED_CATEGORIES_PRESENT] = not any(
        True for _ in missing_required_categories
    ) or severity(reconciled.deterministic) >= severity(Recommendation.INSUFFICIENT_EVIDENCE)

    passed = tuple(check for check in SupervisorCheck if results[check])
    failed = tuple(check for check in SupervisorCheck if not results[check])
    rule_results = tuple(
        RuleResult(
            rule_id=supervisor_rule_id(check),
            outcome=Recommendation.INSUFFICIENT_EVIDENCE,
            rationale=_RATIONALE[check],
        )
        for check in failed
    )
    return Supervision(passed=passed, failed=failed, rule_results=rule_results)
