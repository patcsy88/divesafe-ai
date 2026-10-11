"""Typed settings read from DIVESAFE_* environment variables."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIVESAFE_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: SecretStr | None = None
    # Owner-role connection used ONLY by `python -m divesafe.services.db_init` to install the
    # schema; the running application never needs it.
    admin_database_url: SecretStr | None = None
    app_db_role: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_]{0,62}$")

    llm_provider: Literal["openai", "anthropic", "ollama", "fake"] = "fake"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: AnyHttpUrl | None = None
    # Hosted providers receive evidence and plan data. Off unless explicitly allowed.
    allow_external_llm: bool = False
    # Comma-separated exact hostnames of non-loopback Ollama servers that may be used over plain
    # http (ADR 0016). The prompt then crosses the network unencrypted. Empty: https is required.
    ollama_plain_http_hosts: str | None = None
    # Time budgets for a slow self-hosted model (ADR 0016). The defaults are the original values.
    # One call, the whole agent stage, the wait for a server's first byte, and parallel calls.
    llm_call_timeout_seconds: float = Field(default=60.0, gt=0, le=900)
    llm_stage_timeout_seconds: float = Field(default=150.0, gt=0, le=3600)
    llm_first_byte_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    llm_max_concurrent_calls: int = Field(default=4, ge=1, le=8)

    def plain_http_host_set(self) -> frozenset[str]:
        parts = [p.strip().lower() for p in (self.ollama_plain_http_hosts or "").split(",")]
        parts = [p for p in parts if p]
        if any(
            not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", p) or p.rsplit(".", 1)[-1].isdigit()
            for p in parts
        ):
            raise ValueError(
                "DIVESAFE_OLLAMA_PLAIN_HTTP_HOSTS must be comma-separated exact hostnames"
            )
        return frozenset(parts)

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
        if self.llm_first_byte_timeout_seconds > self.llm_call_timeout_seconds:
            raise ValueError("the LLM first-byte timeout cannot exceed one call's timeout")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
