"""`running_with_app_or_owner_privileges` (ADR 0024 A3): the answer handling, the failure classes
and the SQL's coverage. Offline with a fake; whether the SQL is right against a real server is
covered only by the live tests. Mutations run on COPIES of the module source."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from tests.safety.kb_fakes import FakeConnection, mutated_module

from divesafe.services import kb_privileges
from divesafe.services.kb_privileges import (
    SelfCheckConnectionLost,
    SelfCheckIncomplete,
)

pytestmark = pytest.mark.safety

CAUGHT = (AssertionError, Exception)


class Conn(FakeConnection):
    """Answers the single combined statement with a scripted first column."""

    def __init__(self, answer: Any = (False,), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.answer = answer

    def _answer(self, text: str) -> Any:
        if text.startswith("SET "):
            return None
        if "has_table_privilege" in text:
            if self.role_check_error is not None:
                raise self.role_check_error
            return self.answer
        raise AssertionError("an unexpected statement reached the fake")


def ask(module: Any, conn: Conn) -> bool:
    return bool(module.running_with_app_or_owner_privileges(conn, statement_timeout_ms=15000))


def check_answers(module: Any) -> None:
    assert ask(module, Conn((False,))) is False
    assert ask(module, Conn((True,))) is True
    assert ask(module, Conn((None,))) is True  # unknown counts as a problem
    assert ask(module, Conn(None)) is True  # no row counts as a problem


def test_only_an_explicit_false_means_no_app_or_owner_privileges() -> None:
    check_answers(kb_privileges)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "    return row is None or row[0] is not False\n\n\ndef _checks",
            "    return row is not None and row[0] is True\n\n\ndef _checks",
        ),
        (
            "    return row is None or row[0] is not False\n\n\ndef _checks",
            "    return row is not None and bool(row[0])\n\n\ndef _checks",
        ),
        (
            "    return row is None or row[0] is not False\n\n\ndef _checks",
            "    return False\n\n\ndef _checks",
        ),
    ],
)
def test_mutation_an_unknown_answer_that_passes_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_privileges, (old, new))
    with pytest.raises(CAUGHT):
        check_answers(mutant)
    check_answers(kb_privileges)


def test_it_runs_one_read_only_statement_in_its_own_transaction() -> None:
    conn = Conn((False,))
    ask(kb_privileges, conn)
    texts = [t for _, t in conn.log]
    assert texts[0] == "SET LOCAL search_path = pg_catalog, pg_temp"
    assert texts[1] == "SET LOCAL statement_timeout = 15000"
    assert sum("has_table_privilege" in t for t in texts) == 1
    assert texts[-1] == "ROLLBACK"
    assert len({txn for txn, _ in conn.log}) == 1
    assert conn.read_only is True
    assert not conn.committed()


def test_the_statement_covers_each_class_of_privilege() -> None:
    conn = Conn((False,))
    ask(kb_privileges, conn)
    text = next(t for _, t in conn.log if "has_table_privilege" in t)
    for table in ("assessments", "pending_runs", "run_failure_counts"):
        assert f"to_regclass('public.{table}') IS NULL" in text  # a missing table is a problem
        assert (
            f"has_table_privilege(to_regclass('public.{table}'), 'INSERT, UPDATE, DELETE')" in text
        )
        assert f"has_any_column_privilege(to_regclass('public.{table}'), 'INSERT, UPDATE')" in text
        assert f"has_table_privilege(r.oid, to_regclass('public.{table}')" in text  # via a role
    assert "pg_has_role(current_user, r.oid, 'MEMBER')" in text  # membership, not only the name
    assert "r.rolname <> current_user" in text
    for kb_table in kb_privileges.KNOWLEDGE_TABLES:
        assert f"'{kb_table}'" in text  # ownership of a knowledge table
    for other in ("assessments", "pending_runs", "run_failure_counts", "schema_migrations"):
        assert f"'{other}'" in text  # ownership of an application table
    assert "pg_has_role(current_user, c.relowner, 'MEMBER')" in text
    assert "nspowner" in text and "datdba" in text  # schema and database ownership
    assert "r.rolsuper" in text  # superuser
    assert text.count("IS NOT FALSE") == 2  # unknown on either half is a problem


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        (
            "'INSERT, UPDATE, DELETE') \"\n            f\"OR has_any_column_privilege({reg}, 'INSERT, UPDATE')",  # noqa: E501
            "'INSERT, UPDATE, DELETE') \"\n            f\"OR False",
            "has_any_column_privilege(to_regclass",
        ),
        (
            "            f\"({reg} IS NULL OR has_table_privilege({reg}, 'INSERT, UPDATE, DELETE') \"",  # noqa: E501
            "            f\"(False OR has_table_privilege({reg}, 'INSERT, UPDATE, DELETE') \"",
            "IS NULL",
        ),
        (
            "AND pg_has_role(current_user, r.oid, 'MEMBER') \"\n            f\"AND (has_table_privilege",  # noqa: E501
            'AND False "\n            f"AND (has_table_privilege',
            "AND pg_has_role(current_user, r.oid, 'MEMBER') AND (has_table_privilege",
        ),
        (
            "COALESCE((SELECT pg_has_role(current_user, nspowner, 'MEMBER') FROM pg_namespace \"\n        \"WHERE nspname = 'public'), true) OR \"",  # noqa: E501
            '"',
            "nspowner",
        ),
        (
            'COALESCE((SELECT pg_has_role(current_user, datdba, \'MEMBER\') FROM pg_database "\n        "WHERE datname = current_database()), true) OR "',  # noqa: E501
            '"',
            "datdba",
        ),
        ('OR " + _member_of("r.rolsuper")', '" + "false"', "r.rolsuper"),
        (
            '_APP_WRITE_TABLES = ("assessments", "pending_runs", "run_failure_counts")',
            '_APP_WRITE_TABLES = ("assessments", "pending_runs")',
            "run_failure_counts')",
        ),
    ],
)
def test_mutation_removing_a_part_of_the_statement_is_caught(
    old: str, new: str, needle: str
) -> None:
    mutant = mutated_module(kb_privileges, (old, new))
    real = Conn((False,))
    ask(kb_privileges, real)
    real_text = next(t for _, t in real.log if "has_table_privilege" in t)
    changed = Conn((False,))
    ask(mutant, changed)
    changed_text = next(t for _, t in changed.log if "has_table_privilege" in t)
    assert changed_text != real_text  # the edit changed the statement ...
    assert needle in real_text  # ... by removing something the real one has


# --- failures ------------------------------------------------------------------------------------


def test_a_statement_timeout_is_an_incomplete_check_not_a_lost_connection() -> None:
    error = type("Timeout", (psycopg.OperationalError,), {"sqlstate": "57014"})("x")
    conn = Conn(role_check_error=error)
    with pytest.raises(SelfCheckIncomplete) as caught:
        ask(kb_privileges, conn)
    assert not isinstance(caught.value, SelfCheckConnectionLost)
    assert conn.log[-1][1] == "ROLLBACK"


@pytest.mark.parametrize("code", [None, "08006", "57P01", "53300"])
def test_any_other_operational_error_is_a_lost_connection(code: str | None) -> None:
    error = type("Lost", (psycopg.OperationalError,), {"sqlstate": code})("never-echoed")
    with pytest.raises(SelfCheckConnectionLost) as caught:
        ask(kb_privileges, Conn(role_check_error=error))
    assert "never-echoed" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("error", [RuntimeError("boom"), psycopg.DatabaseError("boom")])
def test_any_other_error_is_an_incomplete_check(error: Exception) -> None:
    with pytest.raises(SelfCheckIncomplete) as caught:
        ask(kb_privileges, Conn(role_check_error=error))
    assert not isinstance(caught.value, SelfCheckConnectionLost)
    assert "boom" not in str(caught.value)


def test_a_check_that_could_not_finish_never_answers_false() -> None:
    for error in (RuntimeError("x"), psycopg.OperationalError("x")):
        with pytest.raises(SelfCheckIncomplete):
            ask(kb_privileges, Conn(role_check_error=error))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            '        if exc.sqlstate != "57014":\n            raise SelfCheckConnectionLost',
            "        if False:\n            raise SelfCheckConnectionLost",
        ),
        (
            '        if exc.sqlstate != "57014":\n            raise SelfCheckConnectionLost',
            "        if True:\n            raise SelfCheckConnectionLost",
        ),
    ],
)
def test_mutation_swapping_lost_connection_and_timeout_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_privileges, (old, new))
    timeout = type("Timeout", (psycopg.OperationalError,), {"sqlstate": "57014"})("x")
    lost = type("Lost", (psycopg.OperationalError,), {"sqlstate": "08006"})("x")

    def kinds(module: Any) -> list[str]:
        found = []
        for error in (timeout, lost):
            try:
                ask(module, Conn(role_check_error=error))
            except module.SelfCheckConnectionLost:
                found.append("lost")
            except module.SelfCheckIncomplete:
                found.append("incomplete")
        return found

    assert kinds(kb_privileges) == ["incomplete", "lost"]
    assert kinds(mutant) != ["incomplete", "lost"]


def test_a_missing_timeout_limit_is_refused_not_defaulted() -> None:
    from divesafe.services.kb_common import LimitNotSet

    for value in (0, -1):
        with pytest.raises((LimitNotSet, SelfCheckIncomplete)):
            kb_privileges.running_with_app_or_owner_privileges(
                Conn((False,)), statement_timeout_ms=value
            )
