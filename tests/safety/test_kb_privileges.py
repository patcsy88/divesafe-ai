"""The knowledge loader's privilege self-check (ADR 0022 decision 11; ADR 0024 A3), offline.

A fake connection returns one row of flags, one per check, in the order of `_checks()`. That proves
the reporting logic and the transaction shape; it does NOT prove the SQL is right against a real
server (that is the live test, which has not been run). The label list is pinned here on purpose,
so removing or reordering a check in the source is noticed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from tests.safety.kb_fakes import FakeConnection

from divesafe.services import kb_privileges
from divesafe.services.kb_privileges import (
    SelfCheckIncomplete,
    loader_privilege_problems,
    running_as_privileged_role,
)

pytestmark = pytest.mark.safety

LABELS = [
    "is, or is a member of, the owner, app or bootstrap role",
    "owns a knowledge table, or the table is missing",
    "owns a knowledge table",
    "a knowledge table is missing",
    "owns the public schema",
    "owns the database",
    "can create objects in the public schema",
    "can create schemas in the database",
    "is a superuser or a member of one",
    "is a member of a role other than itself",
    "can replicate, or is a member of a role that can",
    "can create roles, or is a member of a role that can",
    "can create databases, or is a member of a role that can",
    "bypasses row-level security, or is a member of a role that does",
    "is a member of a role that can run programs or read or write server files",
    "has TEMPORARY on the database (directly or through PUBLIC)",
    "can reach TEMPORARY through a role it is a member of",
    "runs with session_replication_role other than origin",
    "has a stored session_replication_role setting",
    "has UPDATE, DELETE, TRUNCATE, TRIGGER or REFERENCES on a knowledge table",
    "has a column UPDATE or REFERENCES privilege on a knowledge table",
    "can INSERT a status-event column beyond its grant (ordering key or recorded-at)",
    "has a privilege on the status-event ordering sequence",
    "can EXECUTE a knowledge function",
    "owns a knowledge function",
    "has a privilege on assessments",
    "has a privilege on pending_runs",
    "has a privilege on run_failure_counts",
    "has a privilege on schema_migrations",
]
PARAMETER_LABEL = "may set session_replication_role"
TIMEOUT = 15_000


def problems(fake: FakeConnection) -> list[str]:
    return loader_privilege_problems(fake, statement_timeout_ms=TIMEOUT)  # type: ignore[arg-type]


def test_the_check_list_is_the_pinned_one() -> None:
    assert [label for _, label in kb_privileges._checks()] == LABELS


def test_a_clean_role_reports_nothing() -> None:
    assert problems(FakeConnection()) == []


@pytest.mark.parametrize("index", range(len(LABELS)), ids=LABELS)
def test_each_problem_class_is_reported_alone(index: int) -> None:
    assert problems(FakeConnection(selfcheck_flags={index: True})) == [LABELS[index]]


@pytest.mark.parametrize("index", [0, 9, 10, 15, 22, 28])
def test_an_unknown_answer_counts_as_a_problem(index: int) -> None:
    fake = FakeConnection()
    original = fake._answer

    def answer(text: str) -> Any:
        row = original(text)
        if "session_replication_role" in text and row is not None:
            row = tuple(None if i == index else v for i, v in enumerate(row))
        return row

    fake._answer = answer  # type: ignore[method-assign]
    assert problems(fake) == [LABELS[index]]


def test_the_parameter_privilege_is_checked_from_postgresql_15() -> None:
    assert problems(FakeConnection(server_version=140000)) == []
    clean = FakeConnection(server_version=150000)
    assert problems(clean) == []
    flagged = FakeConnection(server_version=150000, selfcheck_flags={len(LABELS): True})
    assert problems(flagged) == [PARAMETER_LABEL]


def test_no_label_contains_a_role_name_or_a_value() -> None:
    from divesafe.config.roles import RESERVED_ROLE_NAMES

    for label in [*LABELS, PARAMETER_LABEL]:
        assert not any(name in label for name in RESERVED_ROLE_NAMES)
        assert "{" not in label and "%" not in label


def test_the_check_runs_in_its_own_read_only_transaction_and_ends_it() -> None:
    fake = FakeConnection()
    problems(fake)
    texts = [text for _, text in fake.log]
    assert texts[0] == "SET LOCAL search_path = pg_catalog, pg_temp"
    assert texts[1] == "SET LOCAL statement_timeout = 15000"
    assert texts[-1] == "ROLLBACK" and not fake.in_txn
    assert len({txn for txn, _ in fake.log}) == 1


@pytest.mark.parametrize(
    "error", [psycopg.errors.QueryCanceled("timeout"), psycopg.OperationalError("lost")]
)
def test_a_check_that_cannot_finish_raises_incomplete_and_ends_the_transaction(
    error: Exception,
) -> None:
    fake = FakeConnection(selfcheck_error=error)
    with pytest.raises(SelfCheckIncomplete) as caught:
        problems(fake)
    assert caught.value.__cause__ is None and not fake.in_txn
    assert str(error) not in str(caught.value)


def test_a_short_result_row_is_incomplete_not_clean() -> None:
    fake = FakeConnection()
    original = fake._answer
    fake._answer = (  # type: ignore[method-assign]
        lambda text: original(text)[:-1] if "session_replication_role" in text else original(text)
    )
    with pytest.raises(SelfCheckIncomplete):
        problems(fake)


def test_an_unset_timeout_fails_closed() -> None:
    fake = FakeConnection()
    with pytest.raises(SelfCheckIncomplete):
        loader_privilege_problems(fake, statement_timeout_ms=0)  # type: ignore[arg-type]
    assert not fake.in_txn and not fake.has("SELECT ")


# --- mutation: a check removed from the list is noticed ------------------------------------------


def every_label_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    for index, label in enumerate(LABELS):
        assert problems(FakeConnection(selfcheck_flags={index: True})) == [label]


def _without(index: int) -> Callable[[], list[tuple[str, str]]]:
    real = kb_privileges._checks

    def checks() -> list[tuple[str, str]]:
        rows = real()
        del rows[index]
        return rows

    return checks


@pytest.mark.parametrize("index", [0, 5, 9, 10, 15, 21, 22, 28])
def test_mutation_a_removed_check_is_caught(monkeypatch: pytest.MonkeyPatch, index: int) -> None:
    every_label_is_reported(monkeypatch)
    monkeypatch.setattr(kb_privileges, "_checks", _without(index))
    with pytest.raises((AssertionError, SelfCheckIncomplete)):  # a short row also fails closed
        every_label_is_reported(monkeypatch)


def test_mutation_a_check_that_counts_unknown_as_clean_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_flag = "flag is not False"
    import inspect

    source = inspect.getsource(loader_privilege_problems)
    assert source_flag in source
    namespace: dict[str, Any] = {}
    exec(source.replace(source_flag, "flag is True"), vars(kb_privileges), namespace)  # noqa: S102
    mutated = namespace["loader_privilege_problems"]
    fake = FakeConnection()
    original = fake._answer
    fake._answer = (  # type: ignore[method-assign]
        lambda text: (
            (None, *original(text)[1:]) if "session_replication_role" in text else original(text)
        )
    )
    assert mutated(fake, statement_timeout_ms=TIMEOUT) == []  # the mutant misses the unknown
    fake2 = FakeConnection()
    original2 = fake2._answer
    fake2._answer = (  # type: ignore[method-assign]
        lambda text: (
            (None, *original2(text)[1:]) if "session_replication_role" in text else original2(text)
        )
    )
    assert problems(fake2) == [LABELS[0]]  # the real one reports it


# --- running as the owner or app role ------------------------------------------------------------


def role_check(fake: FakeConnection) -> bool:
    return running_as_privileged_role(fake, statement_timeout_ms=TIMEOUT)  # type: ignore[arg-type]


@pytest.mark.parametrize(("answer", "expected"), [(True, True), (None, True), (False, False)])
def test_running_as_a_privileged_role_fails_closed(answer: bool | None, expected: bool) -> None:
    assert role_check(FakeConnection(privileged=answer)) is expected


def test_the_role_check_has_its_own_transaction_and_timeout() -> None:
    fake = FakeConnection()
    role_check(fake)
    assert [text for _, text in fake.log][:2] == [
        "SET LOCAL search_path = pg_catalog, pg_temp",
        "SET LOCAL statement_timeout = 15000",
    ]
    assert fake.log[-1][1] == "ROLLBACK" and not fake.in_txn


def test_a_role_check_that_cannot_finish_raises_incomplete() -> None:
    with pytest.raises(SelfCheckIncomplete):
        role_check(FakeConnection(role_check_error=psycopg.OperationalError("lost")))


def test_the_role_check_names_the_reserved_roles_and_the_owner_of_the_events_table() -> None:
    fake = FakeConnection()
    role_check(fake)
    text = next(t for _, t in fake.log if "pg_has_role" in t)
    for name in ("divesafe_owner", "divesafe_bootstrap", "divesafe_app"):
        assert name in text
    assert "kb_status_events" in text and "relowner" in text
