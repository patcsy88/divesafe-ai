"""Narrow settings for the knowledge loader (ADR 0024 A3, security finding S1).

The shared `Settings` refuses to start in production without an LLM provider and API key hashes,
and the loader gets neither on purpose, so the loader has a model of its own. It reads only the
environment name, the secrets directory, the loader connection string and an optional corpus
target. Messages name a setting and a reason, never a value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr

from divesafe.config.secret_files import (
    ENV_PREFIX,
    SecretFileError,
    read_secret_files,
    secret_file_name,
)

LOADER_FIELD = "loader_database_url"
ENVIRONMENT_KEY = "DIVESAFE_ENVIRONMENT"
SECRETS_DIR_KEY = "DIVESAFE_SECRETS_DIR"
LOADER_URL_KEY = f"{ENV_PREFIX}{LOADER_FIELD.upper()}"
# Owner decision Q3 (2026-10-23): the name stays DIVESAFE_CORPUS_TARGET. Required for load and
# approve (`require_corpus_target`), optional for withdraw.
CORPUS_TARGET_KEY = "DIVESAFE_CORPUS_TARGET"
_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
_ENVIRONMENTS = ("development", "test", "production")
_URI_SCHEMES = ("postgresql://", "postgres://")
# Shape and length BEFORE int(): an over-long digit string must be a LoaderConfigError, not a
# ValueError from int(). The ceiling is a single digit, so one digit is the whole grammar.
_TIMEOUT_VALUE = re.compile(r"[1-9][0-9]{0,2}")
# Production refuses keys that make libpq read connection parameters from another file or service.
_PRODUCTION_REFUSED_KEYS = frozenset({"service", "hostaddr", "passfile"})
_CONNECT_TIMEOUT_CEILING = 5  # ADR 0024 A3: the value make-secrets.sh writes


class LoaderConfigError(ValueError):
    """The loader's configuration is refused. The message never contains a value."""


class LoaderSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    environment: Literal["development", "test", "production"]
    loader_database_url: SecretStr
    corpus_target: Path | None = None

    @property
    def production(self) -> bool:
        return self.environment == "production"

    def require_corpus_target(self) -> Path:
        """`load` and `approve` need the corpus target; `withdraw` never calls this."""
        if self.corpus_target is None:
            raise LoaderConfigError("DIVESAFE_CORPUS_TARGET is required for load and approve")
        return self.corpus_target


def _check_uri_and_timeout(dsn: str) -> None:
    """ADR 0024 A1/A3: URI form only; no `%` in query keys (libpq decodes them, so an encoded
    duplicate of connect_timeout could win); no repeated key; connect_timeout, if present, is a
    positive integer without sign or leading zero and at most the ceiling."""
    refused = LoaderConfigError("the loader connection string is not valid")
    if not dsn.startswith(_URI_SCHEMES):
        raise refused
    try:
        query = urlsplit(dsn).query
    except ValueError:
        raise refused from None
    if not query:
        return
    seen: set[str] = set()
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if "%" in key or key in seen:
            raise refused
        seen.add(key)
        if key == "connect_timeout" and (
            _TIMEOUT_VALUE.fullmatch(value) is None or int(value) > _CONNECT_TIMEOUT_CEILING
        ):
            raise refused


def _query_keys(dsn: str) -> set[str]:
    try:
        query = urlsplit(dsn).query
    except ValueError:
        return set()
    return {part.partition("=")[0] for part in query.split("&")} if query else set()


def _require_production_shape(dsn: str) -> None:
    from psycopg.conninfo import conninfo_to_dict

    try:
        parts = conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - the parser's message can echo the string
        raise LoaderConfigError("the loader connection string is not valid") from None
    if not parts.get("user") or not parts.get("password"):
        raise LoaderConfigError(
            "production needs a user and a password in the loader connection string"
        )
    if _query_keys(dsn) & _PRODUCTION_REFUSED_KEYS:
        raise LoaderConfigError(
            "production refuses service, hostaddr and passfile in the loader connection string"
        )
    hosts = str(parts.get("host") or "").split(",")
    if any(not h for h in hosts):
        raise LoaderConfigError("production needs a non-empty host in the loader connection string")
    if any(h.startswith("/") for h in hosts):
        raise LoaderConfigError(
            "production refuses a Unix-socket host in the loader connection string"
        )
    mode = parts.get("sslmode", "prefer")
    if mode not in _TLS_MODES:
        raise LoaderConfigError(
            "production needs sslmode=require, verify-ca or verify-full in the loader connection"
            " string"
        )


def _url_from_secrets_dir(location: Path, environ: Mapping[str, str]) -> str | None:
    if Path(".env").is_file():
        # Same rule as load_settings: a settings file would be ignored or lose to a secret file.
        raise LoaderConfigError(
            ".env exists and DIVESAFE_SECRETS_DIR is set: remove it and use the secret file"
        )
    try:
        entries = set(os.listdir(location))
    except OSError:
        raise LoaderConfigError("DIVESAFE_SECRETS_DIR is not a readable directory") from None
    if entries != {secret_file_name(LOADER_FIELD)}:
        raise LoaderConfigError(
            "the secrets directory must hold the loader connection string file and nothing else"
        )
    try:
        found = read_secret_files(location, environ, fields=(LOADER_FIELD,))
    except SecretFileError as exc:
        raise LoaderConfigError(str(exc)) from None
    secret = found.get(LOADER_FIELD)
    return secret.get_secret_value() if secret is not None else None


def load_loader_settings(environ: Mapping[str, str] | None = None) -> LoaderSettings:
    env = os.environ if environ is None else environ
    name = env.get(ENVIRONMENT_KEY) or "development"
    if name not in _ENVIRONMENTS:
        raise LoaderConfigError("DIVESAFE_ENVIRONMENT is not a known environment")
    location = env.get(SECRETS_DIR_KEY)
    url: str | None
    if location:
        url = _url_from_secrets_dir(Path(location), env)
    elif name == "production":
        raise LoaderConfigError("DIVESAFE_SECRETS_DIR is required in production")
    else:
        # Development and test only: a developer database with a developer-created role.
        url = env.get(LOADER_URL_KEY) or None
    if url is None:
        raise LoaderConfigError("the loader connection string is not set")
    _check_uri_and_timeout(url)
    if name == "production":
        _require_production_shape(url)
    target = env.get(CORPUS_TARGET_KEY)
    if name == "production" and target and not Path(target).is_absolute():
        raise LoaderConfigError("DIVESAFE_CORPUS_TARGET must be an absolute path in production")
    return LoaderSettings(
        environment=name,
        loader_database_url=SecretStr(url),
        corpus_target=Path(target) if target else None,
    )
