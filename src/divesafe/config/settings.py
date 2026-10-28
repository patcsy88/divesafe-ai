"""Typed settings read from DIVESAFE_* environment variables."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from divesafe.config.roles import RESERVED_ROLE_NAMES


def _require_tls(dsn: str) -> None:
    """Refuse a production database connection that may fall back to plaintext. The message never
    echoes the connection string."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        mode = conninfo_to_dict(dsn).get("sslmode", "prefer")
    except Exception:  # noqa: BLE001 - the parser's message can echo the string
        raise ValueError("DIVESAFE_DATABASE_URL is not a valid connection string") from None
    if mode not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("production needs sslmode=require, verify-ca or verify-full in the DSN")


# The longest the connectors may take together (their own per-request timeout is below this).
CONNECTOR_BUDGET_SECONDS = 30.0
RUN_MARGIN_SECONDS = 30.0
# The agent stage's model-call cap without cross-examination (5 specialists, 3 scenarios, 1
# proposal) and with it (one more specialist round). Structural counts, not safety limits.
AGENT_CALL_CAP = 9
CROSS_EXAMINATION_CALL_CAP = 14
AGENT_STAGE_CEILING_SECONDS = 1800.0


def agent_stage_budget(
    stage_seconds: float, call_seconds: float, *, cross_examination: bool
) -> tuple[int, float]:
    """(maximum model calls, agent stage deadline in seconds). Off returns the inputs unchanged.
    On adds one parallel call wave (`call_seconds`) to the stage deadline, capped at the settings
    ceiling. Used wherever the call cap or the run deadline is derived (ADR 0025 item 5)."""
    if not cross_examination:
        return AGENT_CALL_CAP, stage_seconds
    stage = min(stage_seconds + call_seconds, AGENT_STAGE_CEILING_SECONDS)
    return CROSS_EXAMINATION_CALL_CAP, stage


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIVESAFE_",
        env_file=".env",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: SecretStr | None = None
    # Owner-role connection used ONLY by `python -m divesafe.services.db_init` to install the
    # schema; the running application never needs it.
    admin_database_url: SecretStr | None = None
    app_db_role: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    # Knowledge loader role (ADR 0022 decision 11). Only `db_init` reads it; unset skips its grants.
    loader_db_role: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_]{0,62}$")

    llm_provider: Literal["openai", "anthropic", "ollama", "openai_compatible", "fake"] = "fake"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: AnyHttpUrl | None = None
    # Hosted providers receive evidence and plan data. Off unless explicitly allowed.
    allow_external_llm: bool = False
    # The operator's statement that a loopback model server runs the model on this machine and
    # forwards prompts nowhere (ADR 0023). A loopback address alone proves nothing: a local proxy
    # can forward everything to a hosted model. Never inferred from the URL, and it cannot be
    # combined with DIVESAFE_ALLOW_EXTERNAL_LLM or used with a non-loopback server.
    llm_server_is_local: bool = False
    # Comma-separated exact hostnames of non-loopback Ollama servers that may be used over plain
    # http (ADR 0016). The prompt then crosses the network unencrypted. Empty: https is required.
    ollama_plain_http_hosts: str | None = None
    # Time budgets for a slow self-hosted model (ADR 0016). The defaults are the original values.
    # One call, the whole agent stage, the wait for a server's first byte, and parallel calls.
    llm_call_timeout_seconds: float = Field(default=60.0, gt=0, le=600, allow_inf_nan=False)
    llm_stage_timeout_seconds: float = Field(default=150.0, gt=0, le=1800, allow_inf_nan=False)
    llm_first_byte_timeout_seconds: float = Field(default=15.0, gt=0, le=300, allow_inf_nan=False)
    llm_max_concurrent_calls: int = Field(default=4, ge=1, le=8)
    # Output tokens per call. A reasoning model spends part of it thinking, so it may need more.
    llm_max_output_tokens: int = Field(default=1500, ge=256, le=8000)
    # ADR 0025 item 5: a second, bounded round of specialist calls. Off by default. Read by the
    # caller only; it is not a parameter of `assess_dive`, not in the API request. When on, the
    # call cap and the run deadline change (see `agent_stage_budget`).
    agent_cross_examination: bool = False

    # Comma-separated exact hostnames of the non-loopback servers the openai_compatible provider
    # may call (ADR 0018). They must use https. Empty: only a loopback server is allowed.
    llm_allowed_hosts: str | None = None

    @field_validator("loader_db_role")
    @classmethod
    def _loader_role_acceptable(cls, value: str | None) -> str | None:
        # Same reserved names as services.postgres.check_loader_role_name; never echoed.
        if value is not None and (
            value == "public" or value.startswith("pg_") or value in RESERVED_ROLE_NAMES
        ):
            raise ValueError("DIVESAFE_LOADER_DB_ROLE is not an acceptable role name")
        return value

    @model_validator(mode="after")
    def _loader_differs_from_app(self) -> Settings:
        if self.loader_db_role is not None and self.loader_db_role == self.app_db_role:
            raise ValueError("DIVESAFE_LOADER_DB_ROLE is not an acceptable role name")
        return self

    def plain_http_host_set(self) -> frozenset[str]:
        return _host_set(self.ollama_plain_http_hosts, "DIVESAFE_OLLAMA_PLAIN_HTTP_HOSTS")

    def effective_run_deadline_seconds(self) -> float:
        if self.run_deadline_seconds is not None:
            return self.run_deadline_seconds
        _, stage = agent_stage_budget(
            self.llm_stage_timeout_seconds,
            self.llm_call_timeout_seconds,
            cross_examination=self.agent_cross_examination,
        )
        return stage + CONNECTOR_BUDGET_SECONDS + RUN_MARGIN_SECONDS

    def llm_allowed_host_set(self) -> frozenset[str]:
        return _host_set(self.llm_allowed_hosts, "DIVESAFE_LLM_ALLOWED_HOSTS")

    # MET Malaysia API token (api.met.gov.my). Unset: the MET marine connector is not used.
    met_api_token: SecretStr | None = None

    @field_validator("met_api_token")
    @classmethod
    def _met_token_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9._~+/=-]{1,256}", value.get_secret_value()
        ):
            raise ValueError("DIVESAFE_MET_API_TOKEN has an unexpected shape")
        return value

    # PHN e-Tide tide predictions (ADR 0021). Off until the ADR is accepted and PHN's wording is on
    # file: every enabled deployment sends POSTs to an undocumented government endpoint.
    phn_etide_enabled: bool = False

    evidence_max_age_minutes: int | None = Field(default=None, gt=0)
    # Path to a reviewed ruleset file of signed-off limits (docs/domain-validation). Unset = none.
    ruleset_path: Path | None = None
    # Optional pin: the app refuses to start if the ruleset file's SHA-256 differs from this.
    ruleset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_max_age_minutes: int | None = Field(default=None, gt=0)

    auth_mode: Literal["api_key", "dev"] = "api_key"
    # JSON object mapping sha256 hex digest of an API key -> actor name. Never store raw keys.
    # {"<sha256 hex>": {"actor": "name", "roles": ["viewer"|"assessor"|"decider"]}}
    api_key_hashes: SecretStr | None = None

    # Per-actor operational limits (abuse and upstream-quota protection, not safety limits).
    # Assessment runs (ADR 0019). Operational limits, not safety limits. The lease is the run
    # deadline plus a fixed margin, so a run gives up before its lease can expire.
    max_runs_in_flight: int = Field(default=4, ge=1, le=64)
    max_runs_per_actor: int = Field(default=2, ge=1, le=64)
    run_deadline_seconds: float | None = Field(default=None, ge=30, le=7200, allow_inf_nan=False)
    run_retry_after_seconds: int = Field(default=30, ge=1, le=600)
    run_shutdown_grace_seconds: float = Field(default=10.0, ge=0, le=120, allow_inf_nan=False)
    failed_run_retention_days: int = Field(default=7, ge=1, le=365)
    rate_limit_create_per_minute: int = Field(default=10, gt=0, le=10_000)
    rate_limit_decide_per_minute: int = Field(default=30, gt=0, le=10_000)
    rate_limit_read_per_minute: int = Field(default=120, gt=0, le=10_000)
    # Failed authentications allowed per source per minute before it is refused (429).
    rate_limit_auth_failures_per_minute: int = Field(default=10, gt=0, le=10_000)
    # Comma-separated IP addresses of reverse proxies whose X-Forwarded-For may be believed (for
    # example the web server). Empty means the TCP peer is always the source.
    trusted_proxies: str | None = None

    def trusted_proxy_set(self) -> frozenset[str]:
        from divesafe.config.netaddr import canonical_ip

        parts = [p.strip() for p in (self.trusted_proxies or "").split(",") if p.strip()]
        found = [canonical_ip(p) for p in parts]
        if any(a is None for a in found):
            raise ValueError("DIVESAFE_TRUSTED_PROXIES must be comma-separated IP addresses")
        return frozenset(str(a) for a in found)

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        self.trusted_proxy_set()
        if self.llm_stage_timeout_seconds < self.llm_call_timeout_seconds:
            raise ValueError("the LLM stage timeout cannot be shorter than one call's timeout")
        if self.llm_call_timeout_seconds < 2 * self.llm_first_byte_timeout_seconds + 1:
            raise ValueError(
                "one LLM call's timeout must be at least twice the first-byte timeout plus 1 s"
            )
        if self.max_runs_per_actor > self.max_runs_in_flight:
            raise ValueError("DIVESAFE_MAX_RUNS_PER_ACTOR cannot exceed the in-flight cap")
        _, stage = agent_stage_budget(
            self.llm_stage_timeout_seconds,
            self.llm_call_timeout_seconds,
            cross_examination=self.agent_cross_examination,
        )
        if (
            self.run_deadline_seconds is not None
            and self.run_deadline_seconds < stage + CONNECTOR_BUDGET_SECONDS + RUN_MARGIN_SECONDS
        ):
            raise ValueError(
                "DIVESAFE_RUN_DEADLINE_SECONDS must cover the LLM stage timeout plus the "
                f"{CONNECTOR_BUDGET_SECONDS:.0f} s connector budget plus {RUN_MARGIN_SECONDS:.0f} s"
            )
        # applies in every environment, not only production
        if self.llm_server_is_local and self.allow_external_llm:
            raise ValueError(
                "DIVESAFE_LLM_SERVER_IS_LOCAL and DIVESAFE_ALLOW_EXTERNAL_LLM contradict each "
                "other; set only one"
            )
        self.llm_allowed_host_set()
        plain_hosts = self.plain_http_host_set()
        if self.environment == "production" and plain_hosts:
            raise ValueError("plain-http Ollama hosts cannot be used in production")
        if self.environment == "production" and self.llm_provider == "fake":
            raise ValueError("the fake LLM provider cannot be used in production")
        if self.environment == "production" and self.auth_mode == "dev":
            raise ValueError("dev authentication cannot be used in production")
        keys = (
            "".join(self.api_key_hashes.get_secret_value().split()) if self.api_key_hashes else ""
        )
        if self.environment == "production" and self.auth_mode == "api_key" and keys in ("", "{}"):
            raise ValueError("production needs API keys in DIVESAFE_API_KEY_HASHES")
        if self.environment == "production" and self.database_url is not None:
            _require_tls(self.database_url.get_secret_value())
        if (
            self.environment == "production"
            and self.ruleset_path is not None
            and self.ruleset_sha256 is None
        ):
            raise ValueError("a production ruleset must be pinned with DIVESAFE_RULESET_SHA256")
        return self


def _host_set(value: str | None, name: str) -> frozenset[str]:
    parts = [p.strip().lower() for p in (value or "").split(",")]
    parts = [p for p in parts if p]
    if any(
        not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*", p)
        or p.rsplit(".", 1)[-1].isdigit()
        or any(label.startswith("0x") for label in p.split("."))
        for p in parts
    ):
        raise ValueError(f"{name} must be comma-separated exact hostnames")
    return frozenset(parts)


class _SecretsLocation(BaseSettings):
    """Where secret files live. Read from the process environment only, never from .env."""

    model_config = SettingsConfigDict(
        env_prefix="DIVESAFE_", extra="ignore", env_ignore_empty=True, env_file=None
    )

    secrets_dir: Path | None = None


def load_settings() -> Settings:
    """Settings from the environment and, when DIVESAFE_SECRETS_DIR is set, from the secret files
    in that directory (ADR 0024). Unset: exactly `Settings()`."""
    import os

    from divesafe.config.secret_files import SecretFileError, read_secret_files

    location = _SecretsLocation().secrets_dir
    if location is None:
        return Settings()
    configured = Settings.model_config.get("env_file")
    names = [configured] if isinstance(configured, str | Path) else list(configured or ())
    present = [str(n) for n in names if Path(n).expanduser().is_file()]
    if present:
        raise SecretFileError(
            f"{', '.join(present)} exists and DIVESAFE_SECRETS_DIR is set: a settings file would "
            "be ignored or lose to a secret file. Remove it and use environment variables"
        )
    # _env_file=None as well: a file created after the check above must not be read either.
    return Settings(_env_file=None, **read_secret_files(location, os.environ))  # type: ignore[arg-type]


@lru_cache
def get_settings() -> Settings:
    return load_settings()
