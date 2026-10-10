"""Application wiring. Tests inject their own `AppState`; production builds it from Settings."""

from __future__ import annotations

import hashlib
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
    OpenMeteoWindConnector,
    UrllibJsonGetter,
)
from divesafe.domain import DataCategory, RiskFactorKind
from divesafe.models import LLMProvider, create_provider
from divesafe.orchestration import (
    RulesetError,
    load_ruleset_bytes,
    read_ruleset_file,
)
from divesafe.risk import (
    EvidencePolicy,
    FactorScopeRule,
    RiskRulesEngine,
    Rule,
    WarningNeedsHumanReadingRule,
    placeholder_rules,
)
from divesafe.services import (
    AssessmentRepository,
    InMemoryAssessmentRepository,
    PostgresAssessmentRepository,
)

# Interim and unreviewed: no verified tide source exists and no limit is signed off yet, so
# assessments are always INSUFFICIENT EVIDENCE. This is the honest state; see
# docs/data-sources.md. Replace through a reviewed ruleset, not at runtime.
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


def build_engine(settings: Settings, now: datetime | None = None) -> RiskRulesEngine | None:
    """None when no evidence max age is configured: there is deliberately no default.

    With `ruleset_path` the signed-off definitions in that file are loaded. Any refusal (invalid,
    unreviewed, expired, duplicate) stops startup: silently ignoring a signed limit would be worse
    than not starting. Placeholders remain only for factors that have no definition.
    """
    if settings.evidence_max_age_minutes is None:
        return None
    policy = EvidencePolicy(
        INTERIM_REQUIRED_CATEGORIES, timedelta(minutes=settings.evidence_max_age_minutes)
    )
    rules: list[Rule] = [WarningNeedsHumanReadingRule()]
    version = INTERIM_RULESET_VERSION
    covered: set[RiskFactorKind] = set()
    if settings.ruleset_path is not None:
        try:
            data = read_ruleset_file(settings.ruleset_path)
            if (
                settings.ruleset_sha256 is not None
                and hashlib.sha256(data).hexdigest() != settings.ruleset_sha256
            ):
                raise RuntimeError("the ruleset file does not match DIVESAFE_RULESET_SHA256")
            loaded = load_ruleset_bytes(data, now or utc_now(), known_sites=SITES.keys())
        except RulesetError as exc:
            raise RuntimeError(f"the ruleset cannot be loaded: {exc}") from exc
        if not loaded.is_clean:
            raise RuntimeError(f"the ruleset has refused definitions: {loaded.describe_refusals()}")
        logger.warning(
            "ruleset loaded",
            extra={
                "ruleset_version": loaded.version,
                "ruleset_sha256": loaded.sha256,
                "definitions": len(loaded.rules),
            },
        )
        rules.extend(loaded.rules)
        covered = {rule.factor for rule in loaded.rules}
        rules.extend(
            FactorScopeRule(f, [r for r in loaded.rules if r.factor == f])
            for f in sorted(covered, key=lambda k: k.value)
        )
        version = loaded.identity
    rules.extend(r for r in placeholder_rules() if r.factor not in covered)
    return RiskRulesEngine(rules, policy, version)


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


def build_repository(settings: Settings) -> AssessmentRepository:
    if settings.database_url is None:
        return InMemoryAssessmentRepository()
    repository = PostgresAssessmentRepository(settings.database_url.get_secret_value())
    problems = repository.verify(least_privilege=settings.environment == "production")
    if problems:
        raise RuntimeError("unsafe database setup: " + "; ".join(problems))
    return repository


def build_default_state(settings: Settings) -> AppState:
    getter = CachingRateLimitedGetter(UrllibJsonGetter())
    return AppState(
        repository=build_repository(settings),
        connectors=[
            OpenMeteoMarineConnector(getter),
            OpenMeteoWindConnector(getter),
            DataGovMyWarningConnector(getter),
        ],
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
