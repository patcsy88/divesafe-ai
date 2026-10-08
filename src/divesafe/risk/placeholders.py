"""Placeholder rules for every threshold-dependent risk factor.

THESE ARE NOT SAFETY RULES. No numeric limit exists for any of them: each limit is
`TBD - REQUIRES DOMAIN VALIDATION` and must come from a cited, reviewed source (docs/risk-model.md).
Until then a placeholder reports that its factor is NOT evaluated and returns INSUFFICIENT
EVIDENCE, so a ruleset made only of placeholders can never recommend GO.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from divesafe.domain import (
    DataCategory,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskFactorKind,
    RuleResult,
    ThresholdStatus,
)

C = DataCategory
K = RiskFactorKind

_ALL = frozenset(DataCategory)

# factor -> evidence categories it will eventually read
_FACTOR_CATEGORIES: dict[RiskFactorKind, frozenset[DataCategory]] = {
    K.WIND: frozenset({C.WIND}),
    K.WAVE_HEIGHT: frozenset({C.WAVES_SWELL}),
    K.SWELL: frozenset({C.WAVES_SWELL}),
    K.SWELL_PERIOD: frozenset({C.WAVES_SWELL}),
    K.CURRENT: frozenset({C.CURRENTS}),
    K.TIDAL_CURRENT: frozenset({C.TIDES, C.CURRENTS}),
    K.WEATHER: frozenset({C.WEATHER}),
    K.FORECAST_UNCERTAINTY: _ALL,
    K.SITE_CONSTRAINT: frozenset({C.SITE_INFORMATION, C.LOCAL_GUIDANCE}),
}


@dataclass(frozen=True)
class ThresholdTbdRule:
    factor: RiskFactorKind
    categories: frozenset[DataCategory]

    @property
    def rule_id(self) -> str:
        return f"placeholder.{self.factor.value}"

    citation = "none: threshold TBD, REQUIRES DOMAIN VALIDATION"

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        relevant = sorted(
            e.id
            for e in evidence
            if e.category in self.categories
            and (self.factor != RiskFactorKind.FORECAST_UNCERTAINTY or e.is_forecast)
        )
        return RuleResult(
            rule_id=self.rule_id,
            outcome=Recommendation.INSUFFICIENT_EVIDENCE,
            rationale=(
                f"The limit for '{self.factor.value}' is TBD (REQUIRES DOMAIN VALIDATION). "
                "This factor is NOT evaluated."
            ),
            evidence_ids=tuple(relevant),
            factor=self.factor,
            threshold_status=ThresholdStatus.TBD,
        )


def placeholder_rules() -> tuple[ThresholdTbdRule, ...]:
    return tuple(
        ThresholdTbdRule(factor, categories) for factor, categories in _FACTOR_CATEGORIES.items()
    )
