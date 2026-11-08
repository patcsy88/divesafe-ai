"""A write whose commit fails may have been recorded: it is never reported as "nothing was recorded"
(review of 3974b08). A failure before the commit still is."""

from __future__ import annotations

import inspect
from typing import Any

import psycopg
import pytest
from tests.api_polling import create_and_poll
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build

from divesafe.services import RepositoryOutcomeUnknownError, RepositoryUnavailableError, postgres

pytestmark = pytest.mark.safety

OUTCOME_UNKNOWN = (
    "storage is unavailable; the outcome is unknown: reload to check before trying again"
)
WRITES = ("create", "replace", "admit", "complete", "fail", "cleanup")


class _WriteConnection:
    """Statements succeed except at `fail_on`: "EXECUTE" (the caller's statement) or "COMMIT"
    (the caller's commit; the session setup's own commit always succeeds)."""

    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.log: list[str] = []
        self.commits = 0

    def execute(self, query: str, *_: object) -> _WriteConnection:
        self.log.append("EXECUTE")
        if self.fail_on == "EXECUTE" and self.commits:
            raise psycopg.OperationalError("server closed the connection")
        return self

    def commit(self) -> None:
        self.commits += 1
        self.log.append("COMMIT")
        if self.fail_on == "COMMIT" and self.commits > 1:
            raise psycopg.OperationalError("server closed the connection")

    def close(self) -> None:
        self.log.append("CLOSE")


def _write(module: Any, monkeypatch: pytest.MonkeyPatch, fail_on: str) -> tuple[Any, list[str]]:
    conn = _WriteConnection(fail_on)
    monkeypatch.setattr(module.psycopg, "connect", lambda dsn, **kwargs: conn)
    repo = module.PostgresAssessmentRepository("postgresql://u@db.invalid/x_test")
    try:
        with module._storage_errors(), repo._transaction() as tx:
            tx.execute("UPDATE public.pending_runs SET state = 'FAILED'")
    except Exception as exc:  # noqa: BLE001 - the type is what is under test
        return type(exc), conn.log
    return None, conn.log


def test_a_failed_commit_is_an_unknown_outcome_and_the_connection_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raised, log = _write(postgres, monkeypatch, "COMMIT")
    assert raised is RepositoryOutcomeUnknownError
    assert log[-2:] == ["COMMIT", "CLOSE"]


def test_a_failure_before_the_commit_is_plain_unavailable_and_nothing_is_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raised, log = _write(postgres, monkeypatch, "EXECUTE")
    assert raised is RepositoryUnavailableError
    assert log.count("COMMIT") == 1  # the session setup's only
    assert log[-1] == "CLOSE"


def test_an_unknown_outcome_is_still_a_storage_error_for_every_existing_handler() -> None:
    """The run runner and the decision race catch RepositoryUnavailableError."""
    assert issubclass(RepositoryOutcomeUnknownError, RepositoryUnavailableError)


@pytest.mark.parametrize("method", WRITES)
def test_every_write_commits_through_the_checked_transaction(method: str) -> None:
    source = inspect.getsource(getattr(postgres.PostgresAssessmentRepository, method))
    assert "self._transaction() as conn" in source
    assert "self._connect()" not in source


@pytest.mark.parametrize(
    "edit",
    [
        ("raise RepositoryOutcomeUnknownError(", "raise RepositoryUnavailableError("),
        ("        finally:\n            conn.close()\n", "        finally:\n            pass\n"),
    ],
    ids=["plain-unavailable", "not-closed"],
)
def test_mutation_of_the_commit_check_is_caught(
    monkeypatch: pytest.MonkeyPatch, edit: tuple[str, str]
) -> None:
    mutant = mutated_module(postgres, edit)
    raised, log = _write(mutant, monkeypatch, "COMMIT")
    assert not (raised is mutant.RepositoryOutcomeUnknownError and log[-1] == "CLOSE")


def _decide_with_storage_down(error: type[Exception]) -> Any:
    client, _, repo = _build()
    aid = create_and_poll(client, PLAN, AUTH_A).json()["record"]["id"]

    def down(*args: object, **kwargs: object) -> None:
        raise error("storage unavailable")

    repo.replace = down
    body = {"decision": "INSUFFICIENT EVIDENCE", "rationale": "reviewed"}
    return client.post(f"/v1/assessments/{aid}/decision", json=body, headers=AUTH_A)


def test_a_decision_whose_commit_fails_is_never_told_that_nothing_was_recorded() -> None:
    response = _decide_with_storage_down(RepositoryOutcomeUnknownError)
    assert response.status_code == 503
    assert response.json() == {"detail": OUTCOME_UNKNOWN}


def test_a_decision_that_failed_before_its_commit_still_says_nothing_was_recorded() -> None:
    response = _decide_with_storage_down(RepositoryUnavailableError)
    assert response.status_code == 503
    assert response.json() == {"detail": "storage is unavailable; nothing was recorded"}
