"""MET's daily ranges may only tighten (ADR 0031). Pure and deterministic.

One check per signed-off wind or wave-height definition. It tests the worst end of MET's daily
range over the +08:00 days the dive window touches, plus the definition's forecast margin,
against that definition's own NO-GO and CAUTION limits. It never gives GO, carries no VALIDATED
status and never covers a factor, so it can only add severity. It applies only to sites in the
reviewed list below; for those, missing or unusable MET data is INSUFFICIENT EVIDENCE.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from divesafe.domain import (
    SUPPORTED_METRICS,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RuleResult,
    WorseWhen,
)
from divesafe.risk.definition_rules import NOT_EVALUATED_MARKER, DefinitionRule, limit_matches

MET_RANGE_SOURCE: Final = "met-api-marine-forecast"
MET_RANGE_RULE_PREFIX: Final = "met-range."
# Site id -> MET waters zone. Owner-approved links only, one site per decision (ADR 0031 item 5).
MET_TIGHTEN_ZONES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "my-pahang-pulau-tioman": "LOCATION:499",
        "my-pahang-tiger-reef": "LOCATION:499",
    }
)
# Definition metric -> name of MET's range in `value["ranges"]` (ADR 0031 item 1).
MET_PAIRED_METRICS: Final[Mapping[str, str]] = MappingProxyType(
    {"wind_speed_10m": "wind_speed", "wave_height": "wave_height"}
)


def _number(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    try:
        value = float(raw)
    except OverflowError:  # an int too large for a float
        return None
    return value if math.isfinite(value) else None


class MetRangeTightenRule:
    def __init__(self, rule: DefinitionRule) -> None:
        d = rule.definition
        if d.metric not in MET_PAIRED_METRICS:
            raise ValueError("this definition's metric has no MET range")
        self._rule = rule
        self._d = d
        self._name = MET_PAIRED_METRICS[d.metric]
        self.rule_id = f"{MET_RANGE_RULE_PREFIX}{d.id}"
        # Stops with its definition: an expired limit is never applied, not even to tighten.
        self.expires_at: datetime = rule.expires_at
        self.citation = f"MET daily range against {rule.citation} (ADR 0031)"

    def _result(
        self, outcome: Recommendation, rationale: str, ids: Sequence[str] = ()
    ) -> RuleResult:
        aspects = SUPPORTED_METRICS[self._d.metric].not_evaluated
        if aspects:
            rationale += f" {NOT_EVALUATED_MARKER} {', '.join(aspects)}."
        # No factor and no threshold status: never covers a factor (ADR 0031 item 4).
        return RuleResult(
            rule_id=self.rule_id,
            outcome=outcome,
            rationale=rationale,
            evidence_ids=tuple(sorted(ids)),
        )

    def _insufficient(self, problem: str, ids: Sequence[str] = ()) -> RuleResult:
        return self._result(
            Recommendation.INSUFFICIENT_EVIDENCE,
            f"MET check for rule '{self._d.id}' ({self._d.metric}): {problem}",
            ids,
        )

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult | None:
        d = self._d
        zone = MET_TIGHTEN_ZONES.get(plan.site_id)
        if zone is None or not self._rule.applies(plan):
            return None
        metric = SUPPORTED_METRICS[d.metric]
        start = plan.planned_start
        end = start + timedelta(minutes=plan.planned_duration_minutes)
        # Daily items, half-open: a dive ending at midnight does not touch the next day. Every MET
        # item of this category in the window must be usable; a malformed one is never skipped.
        items = [
            e
            for e in evidence
            if e.source == MET_RANGE_SOURCE
            and e.category == metric.category
            and _touches(e, start, end)
        ]
        ids = [e.id for e in items]
        if any(e.valid_until is None for e in items):
            return self._insufficient("a MET item is not a daily range.", ids)
        if not _chains(items, start, end):
            return self._insufficient("no MET daily range for part of the dive window.", ids)
        if any(e.value.get("location_id") != zone for e in items):
            return self._insufficient("a MET item is for another waters zone.", ids)
        higher = d.worse_when == WorseWhen.HIGHER
        values: list[float] = []
        for item in items:
            ranges = item.value.get("ranges")
            rng = ranges.get(self._name) if isinstance(ranges, dict) else None
            if not isinstance(rng, dict) or rng.get("unit") != metric.unit:
                return self._insufficient("a MET range has an unexpected unit or shape.", ids)
            low, high = _number(rng.get("low")), _number(rng.get("high"))
            # Wind speed and wave height cannot be negative; the connector never makes one.
            if low is None or high is None or low < 0 or low > high:
                return self._insufficient(
                    "a MET range has a non-numeric, non-finite, negative or reversed value.", ids
                )
            values.append(high if higher else low)
        worst = max(values) if higher else min(values)
        adjusted = worst + d.forecast_margin if higher else worst - d.forecast_margin
        detail = (
            f"MET check for rule '{d.id}' ({d.metric}): worst end of MET's daily range over the "
            f"dive window {worst:g} {metric.unit}, {adjusted:g} {metric.unit} after the "
            f"signed-off forecast margin of {d.forecast_margin:g} {metric.unit}"
        )
        if limit_matches(d.no_go_when, adjusted):
            return self._result(Recommendation.NO_GO, f"{detail}: NO-GO range.", ids)
        if limit_matches(d.caution_when, adjusted):
            return self._result(Recommendation.CAUTION, f"{detail}: CAUTION range.", ids)
        return None


def _touches(item: EvidenceItem, start: datetime, end: datetime) -> bool:
    if item.valid_until is None:
        return start <= item.valid_at <= end
    return item.valid_at < end and item.valid_until > start


def _chains(items: Sequence[EvidenceItem], start: datetime, end: datetime) -> bool:
    """Do these intervals cover [start, end] without a gap?"""
    reach: datetime | None = None
    for item in sorted(items, key=lambda e: e.valid_at):
        assert item.valid_until is not None
        if reach is None:
            if item.valid_at > start:
                return False
            reach = item.valid_until
        elif item.valid_at <= reach:
            reach = max(reach, item.valid_until)
        else:
            return False
    return reach is not None and reach >= end
