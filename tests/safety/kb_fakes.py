"""A fake psycopg connection for the knowledge loader tests (no network, no database).

It records every statement, commit, rollback and line written to stdout in ONE ordered log, tracks
transaction numbers, refuses statements in an aborted transaction (like PostgreSQL) and refuses a
`read_only` change inside a transaction (like psycopg). Not a test file."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from divesafe.services import kb_common, kb_privileges

RECORDED_AT = datetime(2026, 10, 23, 1, 2, 3, tzinfo=UTC)
EARLIER_RECORDED_AT = datetime(2026, 10, 22, 4, 5, 6, tzinfo=UTC)


class KB001(psycopg.DatabaseError):
    sqlstate = "KB001"


class Cursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class Out:
    """Stands in for sys.stdout: lines land in the connection's ordered log."""

    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def write(self, text: str) -> int:
        self.conn.log.append((self.conn.txn, "OUT:" + text.rstrip("\n")))
        return len(text)

    def flush(self) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        privileged: bool | None = False,
        exists: bool = True,
        insert_error: Exception | None = None,
        commit_error: Exception | None = None,
        existing_row: tuple[Any, ...] | None = (EARLIER_RECORDED_AT,),
        existing_error: Exception | None = None,
        selfcheck_flags: dict[int, bool] | None = None,
        selfcheck_error: Exception | None = None,
        role_check_error: Exception | None = None,
        server_version: int = 140000,
    ) -> None:
        self.privileged = privileged
        self.exists = exists
        self.insert_error = insert_error
        self.commit_error = commit_error
        self.existing_row = existing_row
        self.existing_error = existing_error
        self.selfcheck_flags = selfcheck_flags or {}
        self.selfcheck_error = selfcheck_error
        self.role_check_error = role_check_error
        self.server_version = server_version
        self.log: list[tuple[int, str]] = []
        self.txn = 0
        self.in_txn = False
        self.aborted = False
        self.pending_inserts = 0
        self.committed_inserts = 0
        self.closed = False
        self._read_only = False

    # -- psycopg surface -----------------------------------------------------------------------

    @property
    def read_only(self) -> bool:
        return self._read_only

    @read_only.setter
    def read_only(self, value: bool) -> None:
        if self.in_txn:
            raise psycopg.ProgrammingError(
                "can't change 'read_only' now: connection in transaction"
            )
        self._read_only = value

    def execute(self, query: Any, params: Any = None) -> Cursor:
        text = query if isinstance(query, str) else query.as_string()
        if self.aborted:
            raise psycopg.errors.InFailedSqlTransaction("current transaction is aborted")
        self.in_txn = True
        self.log.append((self.txn, text))
        try:
            return Cursor(self._answer(text))
        except Exception:
            self.aborted = True
            raise

    def commit(self) -> None:
        if self.commit_error is not None:
            self._end("COMMIT-FAILED", committed=False)
            raise self.commit_error
        self._end("COMMIT", committed=True)

    def rollback(self) -> None:
        self._end("ROLLBACK", committed=False)

    def close(self) -> None:
        self.closed = True

    # -- the scripted server -------------------------------------------------------------------

    def _end(self, label: str, *, committed: bool) -> None:
        self.log.append((self.txn, label))
        if committed:
            self.committed_inserts += self.pending_inserts
        self.pending_inserts = 0
        self.aborted = False
        self.in_txn = False
        self.txn += 1

    def _answer(self, text: str) -> tuple[Any, ...] | None:
        if text.startswith("SET "):
            return None
        if "INSERT INTO public.kb_status_events" in text:
            if self.insert_error is not None:
                raise self.insert_error
            self.pending_inserts += 1
            return (RECORDED_AT,)
        if "FROM public.kb_documents" in text:
            return (1,) if self.exists else None
        if "SELECT recorded_at FROM" in text:
            if self.existing_error is not None:
                raise self.existing_error
            return self.existing_row
        if "server_version_num" in text:
            return (self.server_version,)
        if "session_replication_role" in text:
            if self.selfcheck_error is not None:
                raise self.selfcheck_error
            flags = [False] * len(kb_privileges._checks())
            if self.server_version >= 150000:
                flags.append(False)
            for index, value in self.selfcheck_flags.items():
                flags[index] = value
            return tuple(flags)
        if "pg_has_role" in text:
            if self.role_check_error is not None:
                raise self.role_check_error
            return (self.privileged,)
        raise AssertionError("an unexpected statement reached the fake")

    # -- helpers for assertions ----------------------------------------------------------------

    def index_of(self, needle: str, start: int = 0) -> int:
        for index in range(start, len(self.log)):
            if needle in self.log[index][1]:
                return index
        raise AssertionError("statement not found in the log")

    def has(self, needle: str) -> bool:
        return any(needle in text for _, text in self.log)

    def committed(self) -> bool:
        return any(text == "COMMIT" for _, text in self.log)

    def selfcheck_ran(self) -> bool:
        return any("session_replication_role" in text for _, text in self.log)

    def out_lines(self) -> list[str]:
        return [text[4:] for _, text in self.log if text.startswith("OUT:")]


def install(monkeypatch: pytest.MonkeyPatch, conn: FakeConnection) -> None:
    """`kb_common.connect` returns the fake; stdout writes go to its log."""
    monkeypatch.setattr(kb_common, "connect", lambda dsn: conn)
    monkeypatch.setattr("sys.stdout", Out(conn))
