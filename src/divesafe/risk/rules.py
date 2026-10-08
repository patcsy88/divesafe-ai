"""Rules that need no numeric thresholds. Numeric rules still require a reviewed source."""

from __future__ import annotations

from collections.abc import Sequence

from divesafe.domain import DataCategory, DivePlan, EvidenceItem, Recommendation, RuleResult

_NEEDS_HUMAN_READING = frozenset({"warning", "advisory_without_validity"})


class WarningNeedsHumanReadingRule:
    """A marine warning or undated advisory in the window blocks an automatic GO.

    Warning text is free-text and multi-region, so applicability to the site cannot be decided
    automatically. The honest outcome is INSUFFICIENT EVIDENCE until a human has read it.
    """

    rule_id = "policy.marine_warning_needs_human_reading"
    citation = "ADR 0005 (DiveSafe policy): warning applicability cannot be determined from text"

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult | None:
        hits = sorted(
            e.id
            for e in evidence
            if e.category == DataCategory.MARINE_WARNINGS
            and e.value.get("kind") in _NEEDS_HUMAN_READING
        )
        if not hits:
            return None
        return RuleResult(
            rule_id=self.rule_id,
            outcome=Recommendation.INSUFFICIENT_EVIDENCE,
            rationale=(
                "A marine warning or advisory is in effect or undated, and whether it applies "
                "to this site cannot be determined automatically. A human must read it."
            ),
            evidence_ids=tuple(hits),
        )
