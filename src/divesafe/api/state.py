"""Application wiring. Tests inject their own `AppState`; production builds it from Settings."""

from __future__ import annotations

import logging
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
    DiveSite,
    OpenMeteoMarineConnector,
    UrllibJsonGetter,
)
from divesafe.domain import DataCategory
from divesafe.models import LLMProvider, create_provider
from divesafe.risk import (
    EvidencePolicy,
    RiskRulesEngine,
    Rule,
    WarningNeedsHumanReadingRule,
    placeholder_rules,
)
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


logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class AppState:
    repository: AssessmentRepository
    connectors: Sequence[Connector]
    engine: RiskRulesEngine | None
    authenticator: Authenticator
    clock: Callable[[], datetime]
    sites: Mapping[str, DiveSite]
    decision_max_age: timedelta | None
    provider: LLMProvider | None = None


def build_engine(settings: Settings) -> RiskRulesEngine | None:
    """None when no evidence max age is configured: there is deliberately no default."""
    if settings.evidence_max_age_minutes is None:
        return None
    policy = EvidencePolicy(
        INTERIM_REQUIRED_CATEGORIES, timedelta(minutes=settings.evidence_max_age_minutes)
    )
    rules: list[Rule] = [WarningNeedsHumanReadingRule(), *placeholder_rules()]
    return RiskRulesEngine(rules, policy, INTERIM_RULESET_VERSION)


def build_provider(settings: Settings) -> LLMProvider | None:
    """None disables the agent stage (the default `fake` setting). A real provider that has no
    adapter fails loudly at startup instead of silently producing assessments without agents."""
    if settings.llm_provider == "fake":
        logger.warning("agent stage is OFF (DIVESAFE_LLM_PROVIDER=fake)")
        return None
    provider = create_provider(settings)
    if provider.external and not settings.allow_external_llm:
        raise RuntimeError(
            "this LLM provider sends evidence and plan data off-machine; "
            "set DIVESAFE_ALLOW_EXTERNAL_LLM=true to accept that"
        )
    logger.warning(
        "agent stage is ON",
        extra={"provider": provider.name, "model": provider.model, "external": provider.external},
    )
    return provider


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
        provider=build_provider(settings),
    )
