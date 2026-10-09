"""Typed settings read from DIVESAFE_* environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIVESAFE_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: SecretStr | None = None

    llm_provider: Literal["openai", "anthropic", "ollama", "fake"] = "fake"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: AnyHttpUrl | None = None
    # Hosted providers receive evidence and plan data. Off unless explicitly allowed.
    allow_external_llm: bool = False

    evidence_max_age_minutes: int | None = Field(default=None, gt=0)
    # Path to a reviewed ruleset file of signed-off limits (docs/domain-validation). Unset = none.
    ruleset_path: Path | None = None
    # Optional pin: the app refuses to start if the ruleset file's SHA-256 differs from this.
    ruleset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_max_age_minutes: int | None = Field(default=None, gt=0)

    auth_mode: Literal["api_key", "dev"] = "api_key"
    # JSON object mapping sha256 hex digest of an API key -> actor name. Never store raw keys.
    api_key_hashes: SecretStr | None = None

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment == "production" and self.llm_provider == "fake":
            raise ValueError("the fake LLM provider cannot be used in production")
        if self.environment == "production" and self.auth_mode == "dev":
            raise ValueError("dev authentication cannot be used in production")
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
