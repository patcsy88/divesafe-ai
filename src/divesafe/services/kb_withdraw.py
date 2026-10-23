"""`withdraw <document_id> <version>`: append a WITHDRAWN event (ADR 0022 decision 11; ADR 0024 A3).

    python -m divesafe.services.kb_withdraw withdraw <document_id> <version>

It takes ONLY the per-document lock (inside the status-event trigger), never the global kb_load
lock. The WITHDRAWN event is committed first, its recorded_at printed, and only then does the
privilege self-check run, in its own read-only transaction: the check reports, it never refuses.
Messages are fixed and never echo an argument, a connection string or driver text.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql

from divesafe.config.loader_settings import LoaderConfigError, load_loader_settings
from divesafe.services import kb_common, kb_privileges
from divesafe.services.kb_common import (
    ArgumentRefused,
    LimitNotSet,
    parse_document_version,
    require_limit,
)
from divesafe.services.kb_privileges import SelfCheckIncomplete

SQLSTATE_ALREADY_WITHDRAWN = (
    "KB001"  # used only by the WITHDRAWN -> WITHDRAWN refusal (migration 4)
)
SQLSTATE_ACTIVE_TRANSACTION = "25001"  # SET TRANSACTION ISOLATION LEVEL after a query


@dataclass(frozen=True)
class Outcome:
    status: int
    message: str


# One table: status number, fixed message. Listed in precedence order, highest first. A table test
# pins code -> message -> status; numbers are fixed here and appear nowhere else.
FAILED_ARGUMENTS = Outcome(2, "withdraw refused: expected <document_id> <version>; nothing written")
FAILED_CONFIGURATION = Outcome(3, "withdraw refused: loader configuration; nothing written")
FAILED_CONNECTION = Outcome(4, "withdraw failed: database connection; nothing written")
FAILED_ROLE = Outcome(5, "withdraw refused: running as an owner or app role; nothing written")
FAILED_LOCK_BUSY = Outcome(6, "withdraw failed: lock busy: see the runbook; nothing written")
FAILED_STATEMENT_TIMEOUT = Outcome(
    7, "withdraw failed: statement timeout: see the runbook; nothing written"
)
FAILED_ISOLATION = Outcome(8, "withdraw refused: isolation level; nothing written")
FAILED_DEADLOCK = Outcome(9, "withdraw failed: deadlock; nothing written")
FAILED_NOT_FOUND = Outcome(10, "withdraw refused: version not found; nothing written")
FAILED_OTHER = Outcome(11, "withdraw refused: the database refused the event; nothing written")
OUTCOME_UNKNOWN = Outcome(12, "outcome unknown: re-run the same command")
WITHDRAWN_OVER_PRIVILEGED = Outcome(
    13, "withdrawn; loader role over-privileged, fix before any load"
)
WITHDRAWN_SELF_CHECK_INCOMPLETE = Outcome(14, "withdrawn; self-check incomplete")
ALREADY_WITHDRAWN_NO_TIME = Outcome(15, "already withdrawn; recorded_at unavailable")
ALREADY_WITHDRAWN = Outcome(16, "already withdrawn")
OK = Outcome(0, "withdrawn")

EXIT_TABLE: tuple[Outcome, ...] = (
    FAILED_ARGUMENTS,
    FAILED_CONFIGURATION,
    FAILED_CONNECTION,
    FAILED_ROLE,
    FAILED_LOCK_BUSY,
    FAILED_STATEMENT_TIMEOUT,
    FAILED_ISOLATION,
    FAILED_DEADLOCK,
    FAILED_NOT_FOUND,
    FAILED_OTHER,
    OUTCOME_UNKNOWN,
    WITHDRAWN_OVER_PRIVILEGED,
    WITHDRAWN_SELF_CHECK_INCOMPLETE,
    ALREADY_WITHDRAWN_NO_TIME,
    ALREADY_WITHDRAWN,
    OK,
)

_BY_SQLSTATE: dict[str, Outcome] = {
    "55P03": FAILED_LOCK_BUSY,
    "57014": FAILED_STATEMENT_TIMEOUT,
    "40P01": FAILED_DEADLOCK,
    SQLSTATE_ACTIVE_TRANSACTION: FAILED_ISOLATION,
}


class _AlreadyWithdrawn(Exception):
    pass


class _NotFound(Exception):
    pass


def _emit(stream: Any, text: str) -> None:
    stream.write(text + "\n")
    stream.flush()


_TIMEOUT_NAMES = frozenset({"lock_timeout", "statement_timeout"})


def _timeout(conn: psycopg.Connection[Any], name: str, milliseconds: int) -> None:
    if name not in _TIMEOUT_NAMES:
        raise ValueError("not a timeout setting")
    conn.execute(
        sql.SQL("SET LOCAL {} = {}").format(sql.SQL(name), sql.Literal(require_limit(milliseconds)))
    )


def _send_insert(conn: psycopg.Connection[Any], document_id: str, version: int) -> datetime:
    """The insert through the trigger, in the transaction `_prepare` opened. Returns the
    trigger-stamped recorded_at; the caller commits."""
    try:
        row = conn.execute(
            "INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) "
            "VALUES (%s, %s, 'WITHDRAWN', false) RETURNING recorded_at",
            (document_id, version),
        ).fetchone()
    except psycopg.Error as exc:
        if exc.sqlstate == SQLSTATE_ALREADY_WITHDRAWN:  # exact code; never P0001 or message text
            raise _AlreadyWithdrawn from None
        raise
    if row is None or not isinstance(row[0], datetime):
        raise psycopg.DatabaseError("no recorded_at")
    return row[0]


def _existing_recorded_at(
    conn: psycopg.Connection[Any], document_id: str, version: int
) -> datetime | None:
    """A NEW transaction with its own statement_timeout (the refused one is aborted)."""
    try:
        conn.rollback()
        conn.read_only = True
        _timeout(
            conn,
            "statement_timeout",
            require_limit(kb_common.RECORDED_AT_READ_STATEMENT_TIMEOUT_MS),
        )
        row = conn.execute(
            "SELECT recorded_at FROM public.kb_status_events "
            "WHERE document_id = %s AND version = %s AND status = 'WITHDRAWN' "
            "ORDER BY event_order DESC LIMIT 1",
            (document_id, version),
        ).fetchone()
        conn.rollback()
    except Exception:  # noqa: BLE001 - any failure here is "recorded_at unavailable"
        _discard(conn)
        return None
    if row is None or not isinstance(row[0], datetime):
        return None
    return row[0]


def _discard(conn: psycopg.Connection[Any]) -> None:
    with contextlib.suppress(Exception):
        conn.rollback()


def _failure(exc: psycopg.Error) -> Outcome:
    return _BY_SQLSTATE.get(exc.sqlstate or "", FAILED_OTHER)


def run_withdraw(args: Sequence[str], dsn: str) -> Outcome:
    """Everything after the argument check. Prints recorded_at lines as it learns them."""
    document_id, version = parse_document_version(args)
    try:
        conn = kb_common.connect(dsn)
    except LimitNotSet:
        return FAILED_CONFIGURATION
    except psycopg.Error:
        return FAILED_CONNECTION
    try:
        return _withdraw_on(conn, document_id, version)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _prepare(conn: psycopg.Connection[Any], document_id: str, version: int) -> None:
    """Everything before the INSERT is sent. Raises `_NotFound` for a missing version."""
    conn.read_only = False
    conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    conn.execute(
        sql.SQL("SET LOCAL search_path = {}, {}").format(
            sql.Identifier("pg_catalog"), sql.Identifier("public")
        )
    )
    _timeout(conn, "lock_timeout", require_limit(kb_common.WITHDRAW_LOCK_TIMEOUT_MS))
    _timeout(conn, "statement_timeout", require_limit(kb_common.WITHDRAW_STATEMENT_TIMEOUT_MS))
    # Assumes kb_documents is append-only (triggers refuse UPDATE/DELETE), so a row seen here
    # cannot vanish before the INSERT; the foreign key still decides if that assumption breaks.
    exists = conn.execute(
        "SELECT 1 FROM public.kb_documents WHERE document_id = %s AND version = %s",
        (document_id, version),
    ).fetchone()
    if exists is None:
        raise _NotFound


def _uncertain(exc: psycopg.Error) -> bool:
    """True when the error does not prove the server refused the statement (no SQLSTATE, a
    connection-class or operator-intervention one): the INSERT may have been applied."""
    state = exc.sqlstate
    return state is None or state.startswith(("08", "57P"))


def _withdraw_on(conn: psycopg.Connection[Any], document_id: str, version: int) -> Outcome:
    try:
        # Refuse the owner or app role before anything is written, in its own transaction.
        if kb_privileges.running_as_privileged_role(
            conn, statement_timeout_ms=kb_common.SELF_CHECK_STATEMENT_TIMEOUT_MS or 0
        ):
            return FAILED_ROLE
    except kb_privileges.SelfCheckConnectionLost:
        return FAILED_CONNECTION
    except (SelfCheckIncomplete, LimitNotSet):
        return FAILED_CONFIGURATION
    except Exception:  # noqa: BLE001 - nothing has been written yet
        _discard(conn)
        return FAILED_OTHER
    try:
        _prepare(conn, document_id, version)
    except LimitNotSet:
        _discard(conn)
        return FAILED_CONFIGURATION
    except _NotFound:
        _discard(conn)
        return FAILED_NOT_FOUND
    except psycopg.Error as exc:
        _discard(conn)
        return _failure(exc)
    except Exception:  # noqa: BLE001 - nothing has been written yet
        _discard(conn)
        return FAILED_OTHER
    # From here the INSERT may reach the server: any unexpected failure is "unknown".
    try:
        recorded_at = _send_insert(conn, document_id, version)
    except _AlreadyWithdrawn:
        existing = _existing_recorded_at(conn, document_id, version)
        if existing is None:
            return ALREADY_WITHDRAWN_NO_TIME
        with contextlib.suppress(OSError):  # a broken stdout changes nothing about the state
            _emit(sys.stdout, f"recorded_at={existing.isoformat()}")
        return ALREADY_WITHDRAWN
    except psycopg.Error as exc:
        _discard(conn)
        return OUTCOME_UNKNOWN if _uncertain(exc) else _failure(exc)
    except Exception:  # noqa: BLE001 - the INSERT was sent; its fate is unknown
        _discard(conn)
        return OUTCOME_UNKNOWN
    try:
        conn.commit()
    except Exception:  # noqa: BLE001 - re-running is safe: "already withdrawn" is idempotent
        return OUTCOME_UNKNOWN
    try:
        _emit(sys.stdout, f"recorded_at={recorded_at.isoformat()}")  # before the self-check
    except OSError:  # the event is committed but the operator may not have seen the time
        return OUTCOME_UNKNOWN
    try:
        problems = kb_privileges.loader_privilege_problems(
            conn, statement_timeout_ms=kb_common.SELF_CHECK_STATEMENT_TIMEOUT_MS or 0
        )
    except (SelfCheckIncomplete, LimitNotSet):
        return WITHDRAWN_SELF_CHECK_INCOMPLETE
    return WITHDRAWN_OVER_PRIVILEGED if problems else OK


def main(argv: Sequence[str] | None = None) -> int:
    """After `withdraw`, exactly two arguments fitting the decision 6 grammar, and nothing else."""
    args = list(sys.argv[1:] if argv is None else argv)
    outcome = _run(args)
    with contextlib.suppress(OSError):  # the exit status is the record; a closed pipe is not news
        _emit(sys.stdout if outcome.status == 0 else sys.stderr, outcome.message)
    return outcome.status


def _run(args: list[str]) -> Outcome:
    if not args or args[0] != "withdraw":
        return FAILED_ARGUMENTS
    try:
        parse_document_version(args[1:])
    except ArgumentRefused:
        return FAILED_ARGUMENTS
    try:
        settings = load_loader_settings()
    except LoaderConfigError:
        return FAILED_CONFIGURATION
    return run_withdraw(args[1:], settings.loader_database_url.get_secret_value())


if __name__ == "__main__":
    raise SystemExit(main())
