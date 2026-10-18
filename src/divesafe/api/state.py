"""Application wiring. Tests inject their own `AppState`; production builds it from Settings."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from divesafe.api.auth import Authenticator, build_authenticator
from divesafe.api.ratelimit import Limit, RateLimiter
from divesafe.config import Settings
from divesafe.data import (
    MET_API_HOSTS,
    MET_API_QUOTA,
    PHN_ETIDE_QUOTA,
    SITES,
    CachingRateLimitedGetter,
    Connector,
    DataGovMyForecastConnector,
    DataGovMyWarningConnector,
    DiveSite,
    MetApiMarineConnector,
    OpenMeteoMarineConnector,
    OpenMeteoWindConnector,
    PhnEtideConnector,
    UrllibFormPostJsonGetter,
    UrllibJsonGetter,
)
from divesafe.domain import DataCategory, RiskFactorKind
from divesafe.models import LLMProvider, create_provider
from divesafe.models.egress import check_egress
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
    InMemoryAssessmentRepository,
    PostgresAssessmentRepository,
    RunAwareRepository,
)

# Interim and unreviewed: tide predictions are DEGRADED (PHN e-Tide, ADR 0021, off by default) and
# no limit is signed off yet, so assessments are always INSUFFICIENT EVIDENCE. This is the honest
# state; see docs/data-sources.md. Replace through a reviewed ruleset, not at runtime.
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


# Past its deadline a run still holds its lease for this long, so a run always gives up first.
RUN_LEASE_MARGIN = timedelta(seconds=60)


@dataclass(frozen=True)
class RunLimits:
    """Operational limits for assessment runs (ADR 0019); not safety limits."""

    max_in_flight: int = 4
    max_per_actor: int = 2
    deadline: timedelta = timedelta(seconds=210)
    retry_after_seconds: int = 30
    shutdown_grace: timedelta = timedelta(seconds=10)
    failed_retention: timedelta = timedelta(days=7)

    @property
    def lease(self) -> timedelta:
        return self.deadline + RUN_LEASE_MARGIN


@dataclass
class AppState:
    repository: RunAwareRepository
    connectors: Sequence[Connector]
    engine: RiskRulesEngine | None
    authenticator: Authenticator
    clock: Callable[[], datetime]
    sites: Mapping[str, DiveSite]
    decision_max_age: timedelta | None
    provider: LLMProvider | None = None
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(default_limits()))
    trusted_proxies: frozenset[str] = frozenset()
    runs: RunLimits = field(default_factory=RunLimits)


def default_limits(
    create_per_minute: int = 10,
    decide_per_minute: int = 30,
    read_per_minute: int = 120,
    auth_failures_per_minute: int = 10,
) -> dict[str, Limit]:
    """Operational limits per actor (abuse and upstream-quota protection), not safety limits."""
    return {
        "create": Limit(create_per_minute, 60),
        "decide": Limit(decide_per_minute, 60),
        "read": Limit(read_per_minute, 60),
        "auth_failure": Limit(auth_failures_per_minute, 60),
    }


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
    basis = check_egress(provider, settings)
    logger.warning(
        "agent stage is ON",
        extra={
            "provider": provider.name,
            "model": provider.model,
            "model_host": settings.llm_base_url.host if settings.llm_base_url else None,
            "address_is_remote": provider.external,
            "egress_basis": basis,
        },
    )
    if settings.phn_etide_enabled and basis != "declared_local":
        logger.warning(
            "PHN e-Tide data is ON and the model server is not declared local: the PHN tide item "
            "is sent to it with every assessment, and PHN's terms are not on file (ADR 0021)",
            extra={"egress_basis": basis},
        )
    return provider


def build_repository(settings: Settings) -> RunAwareRepository:
    if settings.database_url is None:
        return InMemoryAssessmentRepository()
    repository = PostgresAssessmentRepository(settings.database_url.get_secret_value())
    production = settings.environment == "production"
    problems = repository.verify(least_privilege=production, owner_not_superuser=production)
    if problems:
        raise RuntimeError("unsafe database setup: " + "; ".join(problems))
    return repository


def build_default_state(settings: Settings) -> AppState:
    if settings.environment == "production" and settings.admin_database_url is not None:
        raise RuntimeError(
            "refusing to start in production with the owner-role database URL "
            "(DIVESAFE_ADMIN_DATABASE_URL); it belongs to db_init only"
        )
    getter = CachingRateLimitedGetter(UrllibJsonGetter())
    connectors: list[Connector] = [
        OpenMeteoMarineConnector(getter),
        OpenMeteoWindConnector(getter),
        DataGovMyForecastConnector(getter),
        DataGovMyWarningConnector(getter),
    ]
    if settings.met_api_token is not None:
        met_getter = CachingRateLimitedGetter(
            UrllibJsonGetter(
                allowed_hosts=MET_API_HOSTS,
                auth_header=(
                    "Authorization",
                    f"METToken {settings.met_api_token.get_secret_value()}",
                ),
            ),
            min_interval_seconds={"api.met.gov.my": 0.0},
            quotas=(MET_API_QUOTA,),
        )
        connectors.append(MetApiMarineConnector(met_getter))
    if settings.phn_etide_enabled:
        logger.warning(
            "PHN e-Tide tide predictions are ON (DIVESAFE_PHN_ETIDE_ENABLED); ADR 0021 is proposed"
        )
        connectors.append(
            PhnEtideConnector(
                CachingRateLimitedGetter(
                    UrllibFormPostJsonGetter(),
                    min_interval_seconds={"etide.hydro.gov.my": 0.0},
                    quotas=(PHN_ETIDE_QUOTA,),
                )
            )
        )
    if settings.environment == "production" and not settings.trusted_proxy_set():
        logger.warning(
            "DIVESAFE_TRUSTED_PROXIES is empty: failed-authentication limits count the TCP peer. "
            "Behind a proxy or the web server, every person shares one bucket until it is set"
        )
    return AppState(
        repository=build_repository(settings),
        connectors=connectors,
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
        limiter=RateLimiter(
            default_limits(
                settings.rate_limit_create_per_minute,
                settings.rate_limit_decide_per_minute,
                settings.rate_limit_read_per_minute,
                settings.rate_limit_auth_failures_per_minute,
            )
        ),
        trusted_proxies=settings.trusted_proxy_set(),
        runs=RunLimits(
            max_in_flight=settings.max_runs_in_flight,
            max_per_actor=settings.max_runs_per_actor,
            deadline=timedelta(seconds=settings.effective_run_deadline_seconds()),
            retry_after_seconds=settings.run_retry_after_seconds,
            shutdown_grace=timedelta(seconds=settings.run_shutdown_grace_seconds),
            failed_retention=timedelta(days=settings.failed_run_retention_days),
        ),
    )
