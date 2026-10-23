"""A fake psycopg connection for the knowledge loader tests (no network, no database).

It records every statement, commit, rollback and line written to stdout in ONE ordered log, tracks
transaction numbers, refuses statements in an aborted transaction (like PostgreSQL) and refuses a
`read_only` change inside a transaction (like psycopg). Not a test file."""

from __future__ import annotations

import inspect
import sys
import types
from collections.abc import Callable
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
    def __init__(self, row: tuple[Any, ...] | list[tuple[Any, ...]] | None) -> None:
        self._rows = row if isinstance(row, list) else ([] if row is None else [row])
        self._row = self._rows[0] if self._rows else None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


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

    def _answer(self, text: str) -> Any:
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


STORED_HASH = "a" * 64


class LoadFakeConnection(FakeConnection):
    """The load/approve server. Statements that carry parameters are logged with them
    (`<sql> -- <params>`), so lock keys and written values can be asserted in order.

    `fail_on` maps a statement fragment to the exception raised when it is sent.
    `on_statement` is called with each statement's text (to advance a fake clock)."""

    def __init__(
        self,
        *,
        app_privileged: bool | None = False,
        site_rows: list[tuple[Any, ...]] | None = None,
        stored_hash: str = STORED_HASH,
        doc_exists: bool = True,
        latest_status: str | None = "DRAFT",
        fail_on: dict[str, Exception] | None = None,
        on_statement: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.app_privileged = app_privileged
        self.site_rows = site_rows or []
        self.stored_hash = stored_hash
        self.doc_exists = doc_exists
        self.latest_status = latest_status
        self.fail_on = fail_on or {}
        self.on_statement = on_statement

    def execute(self, query: Any, params: Any = None) -> Cursor:
        start = len(self.log)
        try:
            return super().execute(query, params)
        finally:
            if params is not None and len(self.log) > start:
                txn, text = self.log[start]
                self.log[start] = (txn, f"{text} -- {params!r}")

    def _answer(self, text: str) -> Any:
        if self.on_statement is not None:
            self.on_statement(text)
        for needle, error in self.fail_on.items():
            if needle in text:
                raise error
        if "session_replication_role" in text or "server_version_num" in text:
            return super()._answer(text)
        if text.startswith("SET "):
            return None
        if "pg_advisory_xact_lock" in text:
            return None
        if "has_table_privilege" in text:
            if self.role_check_error is not None:
                raise self.role_check_error
            return (self.app_privileged,)
        if "FROM public.kb_documents d" in text:
            return list(self.site_rows)
        if "SELECT manifest_sha256" in text:
            return (self.stored_hash,) if self.doc_exists else None
        if "SELECT status FROM public.kb_status_events" in text:
            return None if self.latest_status is None else (self.latest_status,)
        if "INSERT INTO public.kb_documents" in text or "INSERT INTO public.kb_chunks" in text:
            self.pending_inserts += 1
            return None
        if "INSERT INTO public.kb_status_events" in text:
            self.pending_inserts += 1
            return (RECORDED_AT,)
        return super()._answer(text)

    def statements(self) -> list[tuple[int, str]]:
        return list(self.log)

    def position(self, needle: str, start: int = 0) -> int:
        return self.index_of(needle, start)

    def writes_sent(self) -> bool:
        return any("INSERT INTO public.kb_" in text for _, text in self.log)


def mutated_module(module: types.ModuleType, *edits: tuple[str, str], many: bool = False) -> Any:
    """A COPY of `module` built from its source with each (old, new) edit applied. Registered in
    sys.modules so @dataclass and pydantic can find it. Never touches the real module."""
    source = inspect.getsource(module)
    for old, new in edits:
        found = source.count(old)
        assert found >= 1 and (many or found == 1), f"edit target not unique: {old!r} ({found})"
        source = source.replace(old, new)
    copy = types.ModuleType(module.__name__ + "_mutant")
    copy.__dict__.update({k: v for k, v in vars(module).items() if not k.startswith("__")})
    sys.modules[copy.__name__] = copy
    exec(compile(source, "<mutant>", "exec"), copy.__dict__)  # noqa: S102
    return copy
