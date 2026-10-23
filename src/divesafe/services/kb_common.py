"""Pieces shared by the knowledge loader commands (ADR 0022 decisions 6, 10, 11; ADR 0024 A3).

Nothing here echoes an input value. Numbers that no ADR or owner approval states are `None` and
fail closed: the command refuses to run until a reviewer sets them (see `require_limit`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import psycopg

# The trigger functions take pg_advisory_xact_lock(KB_DOCUMENT_LOCK_CLASS, hashtext(document_id))
# (migration 4). `kb_load` `load` and `approve` take the global lock below; `withdraw` must NOT.
KB_DOCUMENT_LOCK_CLASS = 1220001
# Two-key form pg_advisory_xact_lock(KB_LOAD_GLOBAL_LOCK_CLASS, 0). It must differ from the
# per-document class so it can never collide with hashtext(document_id) (ADR 0022 decision 10).
# The value itself is not fixed by the ADRs: chosen at code time, reviewers to confirm.
KB_LOAD_GLOBAL_LOCK_CLASS = 1220002
KB_LOAD_GLOBAL_LOCK_OBJECT = 0

# Owner-approved 2026-10-23 (ADR 0024 A3). None means "not decided"; `require_limit` refuses.
CONNECT_TIMEOUT_SECONDS: int | None = 5
WITHDRAW_LOCK_TIMEOUT_MS: int | None = 10_000
WITHDRAW_STATEMENT_TIMEOUT_MS: int | None = 30_000
RECORDED_AT_READ_STATEMENT_TIMEOUT_MS: int | None = 10_000
SELF_CHECK_STATEMENT_TIMEOUT_MS: int | None = 15_000

_DOCUMENT_ID = re.compile(r"[a-z0-9]+(-[a-z0-9]+){0,2}")
_DOCUMENT_ID_MAX = 40
_VERSION = re.compile(r"[1-9][0-9]{0,5}")
_MANIFEST_SHA256 = re.compile(r"[0-9a-f]{64}")


class LoaderRefusal(Exception):
    """A refusal with a fixed message. It never carries a supplied value."""


class ArgumentRefused(LoaderRefusal):
    pass


class LimitNotSet(LoaderRefusal):
    pass


def require_limit(value: int | None) -> int:
    if value is None or value <= 0:
        raise LimitNotSet("a loader timeout is not set")
    return value


def valid_document_id(value: str) -> bool:
    return len(value) <= _DOCUMENT_ID_MAX and _DOCUMENT_ID.fullmatch(value) is not None


def valid_version(value: str) -> bool:
    return _VERSION.fullmatch(value) is not None


def valid_manifest_sha256(value: str) -> bool:
    return _MANIFEST_SHA256.fullmatch(value) is not None


def parse_document_version(args: Sequence[str]) -> tuple[str, int]:
    """Exactly two arguments, fitting the decision 6 grammar. Nothing else is accepted."""
    if len(args) != 2 or not valid_document_id(args[0]) or not valid_version(args[1]):
        raise ArgumentRefused("arguments refused: expected <document_id> <version>")
    return args[0], int(args[1])


def connect(dsn: str) -> psycopg.Connection[Any]:
    """A plain transaction-mode connection with `connect_timeout` (the keyword wins over any value
    in the string). Timeouts on the server side are SET LOCAL per transaction."""
    return psycopg.connect(
        dsn, connect_timeout=require_limit(CONNECT_TIMEOUT_SECONDS), autocommit=False
    )
