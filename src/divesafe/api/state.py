"""Application wiring. Tests inject their own `AppState`; production builds it from Settings."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from divesafe.api.auth import Authenticator, build_authenticator
from divesafe.config import Settings
from divesafe.data import (
    SITES,
    CachingRateLimitedGetter,
    Connector,
    DataGovMyWarningConnector,
    OpenMeteoMarineConnector,
    Site,
    UrllibJsonGetter,
)
from divesafe.domain import DataCategory
from divesafe.risk import EvidencePolicy, RiskRulesEngine, WarningNeedsHumanReadingRule
from divesafe.services import AssessmentRepository, InMemoryAssessmentRepository

# Interim and unreviewed: no verified wind or tide source exists yet, so assessments are always
# INSUFFICIENT EVIDENCE. This is the honest state; see docs/data-sources.md. Replace through a
# reviewed ruleset, not at runtime.
INTERIM_REQUIRED_CATEGORIES: frozenset[DataCategory] = frozenset(
    {
        DataCategory.WAVES_SWELL,
        DataCategory.CURRENTS,
        DataCategory.SEA_TEMPERATURE,
        DataCategory.MARINE_WARNINGS,
        DataCategory.WIND,
        DataCategory.TIDES,
    }
)
INTERIM_RULESET_VERSION = "interim-unreviewed-0"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class AppState:
    repository: AssessmentRepository
    connectors: Sequence[Connector]
    engine: RiskRulesEngine | None
    authenticator: Authenticator
    clock: Callable[[], datetime]
    sites: Mapping[str, Site]
    decision_max_age: timedelta | None


def build_engine(settings: Settings) -> RiskRulesEngine | None:
    """None when no evidence max age is configured: there is deliberately no default."""
    if settings.evidence_max_age_minutes is None:
        return None
    policy = EvidencePolicy(
        INTERIM_REQUIRED_CATEGORIES, timedelta(minutes=settings.evidence_max_age_minutes)
    )
    return RiskRulesEngine([WarningNeedsHumanReadingRule()], policy, INTERIM_RULESET_VERSION)


def build_default_state(settings: Settings) -> AppState:
    getter = CachingRateLimitedGetter(UrllibJsonGetter())
    return AppState(
        repository=InMemoryAssessmentRepository(),
        connectors=[OpenMeteoMarineConnector(getter), DataGovMyWarningConnector(getter)],
        engine=build_engine(settings),
        authenticator=build_authenticator(settings),
        clock=utc_now,
        sites=SITES,
        decision_max_age=(
            timedelta(minutes=settings.decision_max_age_minutes)
            if settings.decision_max_age_minutes is not None
            else None
        ),
    )
