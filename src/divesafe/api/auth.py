"""Authentication. The actor on every human decision comes from here, never from a request body.

- `api_key` (default): `Authorization: Bearer <key>`. Only sha256 hashes of keys are configured,
  each with an actor name and explicit roles. With no keys configured every request is rejected
  (fail closed).
- `dev`: actor taken from the `X-Dev-Actor` header. Unauthenticated and spoofable; Settings
  refuses it in production.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fastapi import HTTPException, Request

from divesafe.api.ratelimit import LIMIT_KEYS
from divesafe.config import Settings
from divesafe.config.netaddr import bucket, canonical_ip

logger = logging.getLogger(__name__)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ACTOR = 100
_REFUSED = "nothing was recorded; an assessment without a recorded decision is still pending. "


class Role(StrEnum):
    """Roles are explicit and do not imply each other."""

    VIEWER = "viewer"  # read assessments, evidence and sites
    ASSESSOR = "assessor"  # create assessments
    DECIDER = "decider"  # record the human decision and actual conditions


@dataclass(frozen=True)
class Principal:
    actor: str
    roles: frozenset[Role] = frozenset()


@dataclass(frozen=True)
class KeyEntry:
    actor: str
    roles: frozenset[Role]


class Authenticator(Protocol):
    def authenticate(self, request: Request) -> Principal | None: ...

    def canonical_decider(self, actor: str) -> str | None:
        """The configured name of `actor` if they are a known key holder who may decide."""
        ...


def _actor(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned if cleaned and len(cleaned) <= _MAX_ACTOR and cleaned.isprintable() else None


class ApiKeyAuthenticator:
    def __init__(self, hashes: Mapping[str, KeyEntry]) -> None:
        self._hashes = dict(hashes)

    def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        match: KeyEntry | None = None
        for known, entry in self._hashes.items():  # no early exit: constant-ish time
            if hmac.compare_digest(known, digest):
                match = entry
        return Principal(match.actor, match.roles) if match else None

    def canonical_decider(self, actor: str) -> str | None:
        wanted = actor.strip().casefold()
        for e in self._hashes.values():
            if e.actor.casefold() == wanted and Role.DECIDER in e.roles:
                return e.actor
        return None


class DevAuthenticator:
    """Development only: every role, because nothing here is authenticated."""

    def authenticate(self, request: Request) -> Principal | None:
        actor = _actor(request.headers.get("x-dev-actor", ""))
        return Principal(actor, frozenset(Role)) if actor else None

    def canonical_decider(self, actor: str) -> str | None:
        return _actor(actor)


_FORMAT_ERROR = (
    'DIVESAFE_API_KEY_HASHES entries must be sha256-hex -> {"actor": name, "roles": [...]}'
)


def parse_api_key_hashes(raw: str) -> dict[str, KeyEntry]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("DIVESAFE_API_KEY_HASHES has a duplicate key hash")
        return dict(pairs)

    try:
        data = json.loads(raw, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError:
        raise ValueError("DIVESAFE_API_KEY_HASHES is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("DIVESAFE_API_KEY_HASHES must be a JSON object")
    entries: dict[str, KeyEntry] = {}
    for digest, value in data.items():
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ValueError(_FORMAT_ERROR)
        if not isinstance(value, dict) or set(value) != {"actor", "roles"}:
            raise ValueError(_FORMAT_ERROR)
        actor = _actor(value["actor"]) if isinstance(value["actor"], str) else None
        roles = value["roles"]
        valid_roles = {r.value for r in Role}
        if (
            actor is None
            or not isinstance(roles, list)
            or not roles
            or not all(isinstance(r, str) and r in valid_roles for r in roles)
        ):
            raise ValueError(_FORMAT_ERROR)
        role_set = frozenset(Role(r) for r in roles)
        if Role.DECIDER in role_set and Role.VIEWER not in role_set:
            raise ValueError("a decider key must also be a viewer: a decision needs the evidence")
        entries[digest] = KeyEntry(actor, role_set)
    names = [e.actor.casefold() for e in entries.values()]
    if len(set(names)) != len(names):
        raise ValueError("DIVESAFE_API_KEY_HASHES gives two keys the same actor name")
    return entries


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.auth_mode == "dev":
        logger.warning("dev authentication is enabled; never use it outside development")
        return DevAuthenticator()
    if settings.api_key_hashes is None:
        logger.warning("no API keys configured; every authenticated request will be rejected")
        return ApiKeyAuthenticator({})
    return ApiKeyAuthenticator(parse_api_key_hashes(settings.api_key_hashes.get_secret_value()))


def client_source(request: Request, trusted_proxies: frozenset[str]) -> str:
    """Who is calling, for failed-authentication limits.

    The TCP peer, unless that peer is a configured trusted proxy: then the last X-Forwarded-For
    entry, which is the address the proxy itself saw. Anything unparseable falls back to the
    peer, never to the header, so a client cannot choose its own bucket. Addresses are
    normalised (IPv4-mapped unwrapped, zone ids dropped, IPv6 grouped by /64).
    """
    raw = request.client.host if request.client else ""
    peer = canonical_ip(raw)
    if peer is None:
        return raw[:64].encode("unicode_escape").decode("ascii") or "unknown"
    if str(peer) not in trusted_proxies:
        return bucket(peer)
    forwarded = canonical_ip(request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1])
    return bucket(forwarded if forwarded is not None else peer)


def require_principal(request: Request) -> Principal:
    state = request.app.state.divesafe
    authenticator: Authenticator = state.authenticator
    source = client_source(request, state.trusted_proxies)
    # Count this attempt as a failure before the key is looked at; a correct key takes it back.
    # A source at its limit is refused without the key being checked, so it learns nothing.
    wait = state.limiter.reserve("auth_failure", source)
    if wait is not None:
        logger.debug("authentication blocked after repeated failures", extra={"client": source})
        raise HTTPException(
            status_code=429,
            detail=_REFUSED + "too many failed attempts; wait and try again",
            headers={"Retry-After": str(wait)},
        )
    try:
        principal = authenticator.authenticate(request)
    except Exception:
        state.limiter.release("auth_failure", source)  # an error of ours is not a wrong key
        raise
    if principal is not None:
        state.limiter.release("auth_failure", source)
    else:
        logger.warning(
            "authentication failed",
            extra={
                "path": request.url.path[:200].encode("unicode_escape").decode("ascii"),
                "client": source,
            },
        )
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_role(role: Role, *, limit_key: str) -> Callable[[Request], Principal]:
    """Authenticate, check the role (403), then apply the actor's rate limit (429).

    A denied request is not charged to the limiter."""
    return require_any_role((role,), limit_key=limit_key)


def require_any_role(roles: tuple[Role, ...], *, limit_key: str) -> Callable[[Request], Principal]:
    """As `require_role`, for an endpoint that more than one role may use."""

    if limit_key not in LIMIT_KEYS:
        raise ValueError(f"unknown limit key {limit_key!r}")
    if not roles:
        raise ValueError("at least one role is required")

    def dependency(request: Request) -> Principal:
        principal = require_principal(request)
        if not any(role in principal.roles for role in roles):
            logger.warning(
                "forbidden",
                extra={
                    "actor": principal.actor,
                    "role": "|".join(r.value for r in roles),
                    "limit_key": limit_key,
                },
            )
            raise HTTPException(
                status_code=403, detail=_REFUSED + "this key is not permitted to do that"
            )
        retry_after = request.app.state.divesafe.limiter.check(limit_key, principal.actor)
        if retry_after is not None:
            logger.warning("rate limited", extra={"actor": principal.actor, "limit_key": limit_key})
            raise HTTPException(
                status_code=429,
                detail=_REFUSED + "too many requests; slow down",
                headers={"Retry-After": str(retry_after)},
            )
        return principal

    return dependency
