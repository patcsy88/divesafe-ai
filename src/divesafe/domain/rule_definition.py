"""A signed-off safety limit as data (docs/domain-validation/, ADR 0009).

A `RuleDefinition` carries a limit, its scope, units, forecast margin, source, reviewers,
read-back confirmation and expiry. The model refuses to be built if it is inconsistent (a swapped
or mis-transcribed band, a unit that differs from the data, a metric with no data source) and a
definition can only be VALIDATED if the whole sign-off record is present. Nothing here contains a
limit: values come only from a reviewed definition file.

What it cannot represent (diver qualification, dive type, season, exposure direction, per-factor
data age) must be listed in `unrepresented_conditions`, and a definition that lists any is never
VALIDATED, so a limit is refused instead of being silently widened.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints, model_validator

from divesafe.domain.base import Frozen
from divesafe.domain.models import DataCategory
from divesafe.domain.risk_types import THRESHOLD_FACTORS, RiskFactorKind, ThresholdStatus

_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
_Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
MAX_ITEMS = 50


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


Finite = Annotated[float, AfterValidator(_finite)]


class Comparison(StrEnum):
    GREATER_THAN = ">"
    GREATER_OR_EQUAL = ">="
    LESS_THAN = "<"
    LESS_OR_EQUAL = "<="


class WorseWhen(StrEnum):
    """Which direction is more dangerous. Stated by the professional, never assumed."""

    HIGHER = "higher"
    LOWER = "lower"


class Metric(Frozen):
    """A value the connectors actually provide: the only things a limit can be written against."""

    variable: str
    category: DataCategory
    unit: str


_METRICS = (
    Metric(variable="wave_height", category=DataCategory.WAVES_SWELL, unit="m"),
    Metric(variable="swell_wave_height", category=DataCategory.WAVES_SWELL, unit="m"),
    Metric(variable="swell_wave_period", category=DataCategory.WAVES_SWELL, unit="s"),
    Metric(variable="ocean_current_velocity", category=DataCategory.CURRENTS, unit="km/h"),
)
SUPPORTED_METRICS: dict[str, Metric] = {m.variable: m for m in _METRICS}

# Which metrics may express which factor. One primary metric per factor, so a definition on a
# secondary quantity (for example wind-wave height) can never mark the whole factor as covered.
# (The comment below still applies:) Wind, tides, weather, forecast uncertainty and site
# constraints have no data source yet, so no definition for them can be built.
FACTOR_METRICS: dict[RiskFactorKind, frozenset[str]] = {
    RiskFactorKind.WAVE_HEIGHT: frozenset({"wave_height"}),
    RiskFactorKind.SWELL: frozenset({"swell_wave_height"}),
    RiskFactorKind.SWELL_PERIOD: frozenset({"swell_wave_period"}),
    RiskFactorKind.CURRENT: frozenset({"ocean_current_velocity"}),
}


class Limit(Frozen):
    comparison: Comparison
    value: Finite


class RuleScope(Frozen):
    """Where a limit applies. Explicit sites only: there is no wildcard."""

    site_ids: tuple[_Token, ...] = Field(min_length=1, max_length=MAX_ITEMS)
    max_depth_m: Finite | None = Field(default=None, gt=0)


class SourceCitation(Frozen):
    document: _Name
    version_or_date: _Name
    location_in_document: _Name
    publicly_available: bool


class Person(Frozen):
    name: _Name
    role: _Name
    qualification: _Name


class Review(Frozen):
    reviewer: Person
    reviewed_on: AwareDatetime


class RuleDefinition(Frozen):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    factor: RiskFactorKind
    metric: _Token
    worse_when: WorseWhen
    unit: _Name
    no_go_when: Limit | None = None
    caution_when: Limit | None = None
    go_when: Limit | None = None
    forecast_margin: Finite = Field(ge=0, description="Added to the worst value, in the unit.")
    scope: RuleScope
    source: SourceCitation
    document_version: _Name
    status: ThresholdStatus = ThresholdStatus.REQUIRES_DOMAIN_VALIDATION
    prepared_by: Person
    reviews: tuple[Review, ...] = Field(default=(), max_length=MAX_ITEMS)
    no_second_reviewer_reason: _Name | None = None
    readback_confirmed_by: Person | None = None
    readback_confirmed_on: AwareDatetime | None = None
    signed_off_on: AwareDatetime | None = None
    expires_on: AwareDatetime | None = None
    unrepresented_conditions: tuple[_Name, ...] = Field(default=(), max_length=MAX_ITEMS)
    limitations: tuple[_Name, ...] = Field(default=(), max_length=MAX_ITEMS)

    @property
    def allows_go_or_caution(self) -> bool:
        return self.caution_when is not None or self.go_when is not None

    @model_validator(mode="after")
    def _consistent(self) -> RuleDefinition:
        if self.factor not in THRESHOLD_FACTORS:
            raise ValueError("this factor is not a threshold factor")
        allowed = FACTOR_METRICS.get(self.factor, frozenset())
        if self.metric not in allowed:
            raise ValueError(
                "no data source supports this metric for this factor; supported: "
                f"{sorted(allowed) or 'none (no data source exists for this factor)'}"
            )
        metric = SUPPORTED_METRICS[self.metric]
        if self.unit != metric.unit:
            raise ValueError(
                f"the unit differs from the data's unit '{metric.unit}' (no conversion is done)"
            )
        if not (self.no_go_when or self.caution_when or self.go_when):
            raise ValueError("a definition needs at least one limit")
        self._check_directions_and_order()
        return self

    def _check_directions_and_order(self) -> None:
        higher = self.worse_when == WorseWhen.HIGHER
        bad = (
            {Comparison.GREATER_THAN, Comparison.GREATER_OR_EQUAL}
            if higher
            else {Comparison.LESS_THAN, Comparison.LESS_OR_EQUAL}
        )
        good = (
            {Comparison.LESS_THAN, Comparison.LESS_OR_EQUAL}
            if higher
            else {Comparison.GREATER_THAN, Comparison.GREATER_OR_EQUAL}
        )
        for name, limit, allowed in (
            ("no_go_when", self.no_go_when, bad),
            ("caution_when", self.caution_when, bad),
            ("go_when", self.go_when, good),
        ):
            if limit is not None and limit.comparison not in allowed:
                raise ValueError(f"{name} uses a comparison that contradicts worse_when")
        ordered = [lim.value for lim in (self.no_go_when, self.caution_when, self.go_when) if lim]
        severe_first = ordered if higher else [-v for v in ordered]
        if any(a < b for a, b in zip(severe_first, severe_first[1:], strict=False)):
            raise ValueError("limits are out of order: NO-GO must be the most severe, GO the least")

    @model_validator(mode="after")
    def _validated_needs_a_complete_signoff(self) -> RuleDefinition:
        if self.status != ThresholdStatus.VALIDATED:
            return self
        problems: list[str] = []
        if self.unrepresented_conditions:
            problems.append("conditions the software cannot represent are listed")
        names = {r.reviewer.name.casefold() for r in self.reviews}
        independent = [
            r
            for r in self.reviews
            if r.reviewer.name.casefold() != self.prepared_by.name.casefold()
        ]
        if self.allows_go_or_caution and not independent:
            problems.append("a limit that can allow GO or CAUTION needs an independent reviewer")
        if (
            not self.allows_go_or_caution
            and not independent
            and not (self.no_second_reviewer_reason or "").strip()
        ):
            problems.append("no independent reviewer and no reason recorded")
        if len(names) != len(self.reviews):
            problems.append("the same reviewer is listed twice")
        if self.readback_confirmed_by is None or self.readback_confirmed_on is None:
            problems.append("the read-back was not confirmed")
        else:
            # The developer reads the encoded values back; the professional who prepared the
            # limit, or one of its reviewers, confirms them. Nobody else can.
            allowed = {self.prepared_by.name.casefold()} | names
            if self.readback_confirmed_by.name.casefold() not in allowed:
                problems.append(
                    "the read-back was confirmed by someone who is not the preparer or a reviewer"
                )
        if self.signed_off_on is None or self.expires_on is None:
            problems.append("sign-off date and expiry are required")
        else:
            if self.expires_on <= self.signed_off_on:
                problems.append("the expiry is not after the sign-off")
            dated = [r.reviewed_on for r in self.reviews]
            if self.readback_confirmed_on is not None:
                dated.append(self.readback_confirmed_on)
            if any(moment > self.signed_off_on for moment in dated):
                problems.append("a review or the read-back is dated after the sign-off")
        if problems:
            raise ValueError("cannot be VALIDATED: " + "; ".join(problems))
        return self
