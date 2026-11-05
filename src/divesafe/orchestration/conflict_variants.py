"""Most severe variant for conflicting evidence (ADR 0026 Part A). Deterministic: no model.

When different items share one evidence id, none of them is used in the main run. Here every
combination of one variant per conflicting id is added to the main items and the engine runs on
it; if the worst result is more severe than the main run's, one rule result carries it, so the
final recommendation can only get stricter. Variant items are never cited, never shown to an agent
and never make a required category count as present: only the engine sees them, and only to add
severity. Beyond the capacity bounds nothing is guessed: `evidence.conflict_unbounded` is added.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from datetime import datetime

from divesafe.domain import (
    DivePlan,
    EvidenceItem,
    Recommendation,
    RuleResult,
    most_severe,
    severity,
)
from divesafe.risk import RiskRulesEngine

Variants = Sequence[EvidenceItem]

# Capacity bounds, not safety limits: they protect run time only (owner, 2026-11-05).
MAX_CONFLICT_IDS = 4
MAX_VARIANTS_PER_ID = 3
MAX_COMBINATIONS = 64

VARIANT_RULE = "evidence.conflict_variant"
UNBOUNDED_RULE = "evidence.conflict_unbounded"


def within_bounds(groups: Sequence[Variants]) -> bool:
    return (
        len(groups) <= MAX_CONFLICT_IDS
        and all(len(g) <= MAX_VARIANTS_PER_ID for g in groups)
        and math.prod(len(g) for g in groups) <= MAX_COMBINATIONS
    )


def _describe(groups: Sequence[Variants], chosen: Sequence[int]) -> str:
    # Connector names and category values only (fixed names); never an id or item content.
    parts = [
        f"{group[i].category.value} (variant {i + 1} of {len(group)})"
        for group, i in zip(groups, chosen, strict=True)
    ]
    return "; ".join(parts)


def variant_results(
    engine: RiskRulesEngine,
    plan: DivePlan,
    items: Sequence[EvidenceItem],
    groups: Sequence[Variants],
    now: datetime,
    baseline: Recommendation,
) -> list[RuleResult]:
    """Rule results to add to the main run, or none when there is no conflict or no variant is
    more severe than `baseline` (the main run's most severe outcome)."""
    if not groups:
        return []
    if not within_bounds(groups):
        return [
            RuleResult(
                rule_id=UNBOUNDED_RULE,
                outcome=Recommendation.INSUFFICIENT_EVIDENCE,
                rationale=(
                    "too many conflicting evidence variants to evaluate each one; the most "
                    "severe variant was not determined"
                ),
            )
        ]
    worst = baseline
    worst_choice: tuple[int, ...] | None = None
    for choice in itertools.product(*(range(len(g)) for g in groups)):
        added = [g[i] for g, i in zip(groups, choice, strict=True)]
        outcome = most_severe(
            tuple(r.outcome for r in engine.assess(plan, [*items, *added], now).rule_results)
        )
        if severity(outcome) > severity(worst):
            worst, worst_choice = outcome, choice
            if outcome is Recommendation.NO_GO:
                break  # the maximum: no later combination can be worse
    if worst_choice is None:
        return []
    return [
        RuleResult(
            rule_id=VARIANT_RULE,
            outcome=worst,
            rationale=(
                f"read as {_describe(groups, worst_choice)}, the conflicting evidence gives "
                f"{worst.value}; the main result excludes it"
            ),
        )
    ]
