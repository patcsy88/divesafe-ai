"""Authentication. The actor on every human decision comes from here, never from a request body.

- `api_key` (default): `Authorization: Bearer <key>`. Only sha256 hashes of keys are configured.
  With no keys configured every request is rejected (fail closed).
- `dev`: actor taken from the `X-Dev-Actor` header. Unauthenticated and spoofable; Settings
  refuses it in production.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException, Request

from divesafe.config import Settings

logger = logging.getLogger(__name__)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ACTOR = 100


@dataclass(frozen=True)
class Principal:
    actor: str


class Authenticator(Protocol):
    def authenticate(self, request: Request) -> Principal | None: ...


def _actor(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned if cleaned and len(cleaned) <= _MAX_ACTOR and cleaned.isprintable() else None


class ApiKeyAuthenticator:
    def __init__(self, hashes: Mapping[str, str]) -> None:
        self._hashes = dict(hashes)

    def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        match: str | None = None
        for known, actor in self._hashes.items():  # no early exit: constant-ish time
            if hmac.compare_digest(known, digest):
                match = actor
        return Principal(match) if match else None


class DevAuthenticator:
    def authenticate(self, request: Request) -> Principal | None:
        actor = _actor(request.headers.get("x-dev-actor", ""))
        return Principal(actor) if actor else None


def parse_api_key_hashes(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("DIVESAFE_API_KEY_HASHES is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("DIVESAFE_API_KEY_HASHES must be a JSON object")
    hashes: dict[str, str] = {}
    for digest, actor in data.items():
        cleaned = _actor(actor) if isinstance(actor, str) else None
        if not isinstance(digest, str) or not _HEX64.match(digest) or cleaned is None:
            raise ValueError("DIVESAFE_API_KEY_HASHES entries must be sha256-hex -> actor name")
        hashes[digest] = cleaned
    return hashes


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.auth_mode == "dev":
        logger.warning("dev authentication is enabled; never use it outside development")
        return DevAuthenticator()
    if settings.api_key_hashes is None:
        logger.warning("no API keys configured; every authenticated request will be rejected")
        return ApiKeyAuthenticator({})
    return ApiKeyAuthenticator(parse_api_key_hashes(settings.api_key_hashes.get_secret_value()))


def require_principal(request: Request) -> Principal:
    authenticator: Authenticator = request.app.state.divesafe.authenticator
    principal = authenticator.authenticate(request)
    if principal is None:
        logger.warning(
            "authentication failed",
            extra={
                "path": request.url.path,
                "client": request.client.host if request.client else None,
            },
        )
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal
