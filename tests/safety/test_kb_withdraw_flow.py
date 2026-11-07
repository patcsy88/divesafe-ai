"""`withdraw` against a fake connection (ADR 0024 A3): statement order, transaction boundaries,
each exit status and the output lines. Offline; the real server is not involved, so server-side
behaviour (locks, the trigger, SQLSTATE values) is covered only by the live integration tests.
Mutations run on COPIES of the module source, never on `src`."""

from __future__ import annotations

import inspect
import sys
import types
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from tests.safety.kb_fakes import (
    EARLIER_RECORDED_AT,
    RECORDED_AT,
    FakeConnection,
    install,
)

from divesafe.services import kb_withdraw
from divesafe.services.kb_common import LimitNotSet
from divesafe.services.kb_withdraw import Outcome

pytestmark = pytest.mark.safety

DSN = "postgresql://u:never-echoed@127.0.0.1:1/x_test"
ARGS = ["site-guide", "2"]
Run = Callable[[list[str], str], Outcome]


def db_error(code: str, message: str = "refused") -> psycopg.DatabaseError:
    return type("Refusal", (psycopg.DatabaseError,), {"sqlstate": code})(message)


def go(monkeypatch: pytest.MonkeyPatch, fake: FakeConnection, run: Run | None = None) -> Outcome:
    install(monkeypatch, fake)
    result = (run or kb_withdraw.run_withdraw)(ARGS, DSN)
    # A mutant has its own Outcome class; compare by value so a catch is never a class mismatch.
    return Outcome(result.status, result.message)


def mutant(*edits: tuple[str, str]) -> Run:
    source = inspect.getsource(kb_withdraw)
    for old, new in edits:
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    module = types.ModuleType("kb_withdraw_mutant")
    module.__dict__.update({k: v for k, v in vars(kb_withdraw).items() if not k.startswith("__")})
    sys.modules["kb_withdraw_mutant"] = module  # @dataclass looks its module up here
    exec(compile(source, "<mutant>", "exec"), module.__dict__)  # noqa: S102
    run = module.__dict__["run_withdraw"]
    return run  # type: ignore[no-any-return]


# --- statement order and transaction boundaries --------------------------------------------------


def check_isolation_first(monkeypatch: pytest.MonkeyPatch, run: Run | None = None) -> None:
    fake = FakeConnection()
    go(monkeypatch, fake, run)
    iso = fake.index_of("ISOLATION LEVEL READ COMMITTED")
    lock = fake.index_of("SET LOCAL lock_timeout = 10000")
    statement = fake.index_of("SET LOCAL statement_timeout = 30000")
    idle = fake.index_of("SET LOCAL idle_in_transaction_session_timeout = 10000")
    insert = fake.index_of("INSERT INTO public.kb_status_events")
    assert iso < lock < statement < idle < insert
    assert fake.log[iso][0] == fake.log[lock][0] == fake.log[idle][0] == fake.log[insert][0]


def test_mutation_without_the_idle_in_transaction_timeout_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = (
        "    _timeout(\n"
        "        conn,\n"
        '        "idle_in_transaction_session_timeout",\n'
        "        require_limit(kb_common.WITHDRAW_IDLE_IN_TRANSACTION_TIMEOUT_MS),\n"
        "    )\n"
    )
    with pytest.raises(AssertionError):
        check_isolation_first(monkeypatch, mutant((old, "")))


def test_read_committed_is_set_before_the_timeouts_and_the_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_isolation_first(monkeypatch)


def test_mutation_moving_read_committed_later_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    moved = mutant(
        ('    conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")\n', ""),
        (
            "    exists = conn.execute(",
            '    conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")\n'
            "    exists = conn.execute(",
        ),
    )
    with pytest.raises(AssertionError):
        check_isolation_first(monkeypatch, moved)


def check_commit_then_print_then_selfcheck(
    monkeypatch: pytest.MonkeyPatch, run: Run | None = None
) -> None:
    fake = FakeConnection(selfcheck_flags={0: True})
    go(monkeypatch, fake, run)
    insert = fake.index_of("INSERT INTO public.kb_status_events")
    commit = fake.index_of("COMMIT", insert)
    printed = fake.index_of("OUT:recorded_at=", insert)
    check = fake.index_of("session_replication_role", insert)
    assert insert < commit < printed < check
    assert fake.log[check][0] != fake.log[insert][0]  # the self-check has its own transaction
    assert fake.committed_inserts == 1


def test_commit_comes_before_the_print_and_the_print_before_the_self_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_commit_then_print_then_selfcheck(monkeypatch)


def test_mutation_the_self_check_inside_the_withdraw_transaction_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside = mutant(
        (
            "        conn.commit()\n    except Exception:  # noqa: BLE001 - re-running",
            "        pass\n    except Exception:  # noqa: BLE001 - re-running",
        )
    )
    with pytest.raises(AssertionError):
        check_commit_then_print_then_selfcheck(monkeypatch, inside)


def test_mutation_printing_after_the_self_check_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    late = mutant(
        (
            "    try:\n"
            '        _emit(sys.stdout, f"recorded_at={recorded_at.isoformat()}")'
            "  # before the self-check\n"
            "    except OSError:  # the event is committed but the operator may not have seen"
            " the time\n"
            "        return OUTCOME_UNKNOWN\n",
            "",
        ),
        (
            "    return WITHDRAWN_OVER_PRIVILEGED if problems else OK",
            '    _emit(sys.stdout, f"recorded_at={recorded_at.isoformat()}")\n'
            "    return WITHDRAWN_OVER_PRIVILEGED if problems else OK",
        ),
    )
    with pytest.raises(AssertionError):
        check_commit_then_print_then_selfcheck(monkeypatch, late)


def test_the_self_check_sets_its_own_statement_timeout_in_its_own_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection()
    assert go(monkeypatch, fake) == kb_withdraw.OK
    commit = fake.index_of("COMMIT")
    after = fake.index_of("SET LOCAL statement_timeout = 15000", commit)
    assert fake.log[after][0] > fake.log[commit][0]


def test_the_role_check_runs_before_anything_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection()
    go(monkeypatch, fake)
    assert fake.index_of("pg_has_role") < fake.index_of("INSERT INTO public.kb_status_events")
    assert fake.index_of("ROLLBACK") < fake.index_of("ISOLATION LEVEL READ COMMITTED")


# --- success and the post-commit self-check ------------------------------------------------------


def test_success_prints_recorded_at_once_and_exits_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection()
    assert go(monkeypatch, fake) == kb_withdraw.OK
    assert fake.out_lines() == [f"recorded_at={RECORDED_AT.isoformat()}"]
    assert fake.committed_inserts == 1 and fake.closed


def test_the_insert_writes_hash_checked_false_and_binds_the_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection()
    go(monkeypatch, fake)
    text = fake.log[fake.index_of("INSERT INTO public.kb_status_events")][1]
    assert "'WITHDRAWN', false" in text and "site-guide" not in text


def test_an_over_privileged_loader_still_withdraws_and_exits_13(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection(selfcheck_flags={3: True})
    assert go(monkeypatch, fake) == kb_withdraw.WITHDRAWN_OVER_PRIVILEGED
    assert fake.committed_inserts == 1
    assert fake.out_lines() == [f"recorded_at={RECORDED_AT.isoformat()}"]


@pytest.mark.parametrize("error", [Exception("boom"), psycopg.OperationalError("lost")])
def test_an_incomplete_self_check_after_the_commit_exits_14(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    fake = FakeConnection(selfcheck_error=error)
    assert go(monkeypatch, fake) == kb_withdraw.WITHDRAWN_SELF_CHECK_INCOMPLETE
    assert fake.committed_inserts == 1 and len(fake.out_lines()) == 1


def test_mutation_a_withdraw_that_refuses_on_an_over_privileged_role_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role check before the insert refuses an OWNER/APP role only; a self-check problem
    after the commit must never undo or hide the withdrawal."""
    refusing = mutant(
        (
            "    return WITHDRAWN_OVER_PRIVILEGED if problems else OK",
            "    if problems:\n        conn.rollback()\n        return FAILED_OTHER\n    return OK",
        )
    )
    fake = FakeConnection(selfcheck_flags={3: True})
    assert go(monkeypatch, fake, refusing) != kb_withdraw.WITHDRAWN_OVER_PRIVILEGED
    fake = FakeConnection(selfcheck_flags={3: True})
    assert go(monkeypatch, fake) == kb_withdraw.WITHDRAWN_OVER_PRIVILEGED


def test_the_over_privileged_text_appears_only_after_a_successful_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeConnection(selfcheck_flags={3: True}, commit_error=psycopg.OperationalError("x"))
    assert go(monkeypatch, fake) == kb_withdraw.OUTCOME_UNKNOWN
    assert fake.out_lines() == [] and not fake.selfcheck_ran()
    assert "over-privileged" not in kb_withdraw.OUTCOME_UNKNOWN.message


# --- failures that write nothing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("55P03", kb_withdraw.FAILED_LOCK_BUSY),
        ("57014", kb_withdraw.FAILED_STATEMENT_TIMEOUT),
        ("40P01", kb_withdraw.FAILED_DEADLOCK),
        ("25001", kb_withdraw.FAILED_ISOLATION),
        ("P0001", kb_withdraw.FAILED_OTHER),
        ("23514", kb_withdraw.FAILED_OTHER),
        ("XX000", kb_withdraw.FAILED_OTHER),
    ],
)
def test_each_database_refusal_maps_to_its_own_status_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, code: str, expected: Outcome
) -> None:
    fake = FakeConnection(insert_error=db_error(code, "withdraw secret-text never-echoed"))
    assert go(monkeypatch, fake) == expected
    assert fake.committed_inserts == 0 and fake.out_lines() == []
    assert not fake.selfcheck_ran() and not fake.committed()
    assert "secret-text" not in expected.message


def test_another_trigger_refusal_is_never_reported_as_already_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = db_error("P0001", "this version is already withdrawn")  # same words, wrong code
    fake = FakeConnection(insert_error=refusal)
    assert go(monkeypatch, fake) == kb_withdraw.FAILED_OTHER
    assert fake.out_lines() == [] and not fake.has("SELECT recorded_at")


def test_a_missing_version_exits_10_before_any_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(exists=False)
    assert go(monkeypatch, fake) == kb_withdraw.FAILED_NOT_FOUND
    assert not fake.has("INSERT INTO") and fake.out_lines() == []


def test_an_owner_or_app_role_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for privileged in (True, None):  # unknown fails closed
        fake = FakeConnection(privileged=privileged)
        assert go(monkeypatch, fake) == kb_withdraw.FAILED_ROLE
        assert not fake.has("INSERT INTO") and not fake.has("ISOLATION LEVEL")


def test_a_role_check_that_loses_the_connection_exits_4_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection(role_check_error=psycopg.OperationalError("lost"))
    assert go(monkeypatch, fake) == kb_withdraw.FAILED_CONNECTION
    assert not fake.has("INSERT INTO")


def test_a_role_check_statement_timeout_exits_3_not_4(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(role_check_error=psycopg.errors.QueryCanceled("timeout"))
    assert go(monkeypatch, fake) == kb_withdraw.FAILED_CONFIGURATION
    assert not fake.has("INSERT INTO")


def test_a_connection_failure_exits_4_and_an_unset_limit_exits_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(error: Exception) -> Callable[[str], Any]:
        def connect(dsn: str) -> Any:
            raise error

        return connect

    monkeypatch.setattr(kb_withdraw.kb_common, "connect", refuse(psycopg.OperationalError("down")))
    assert kb_withdraw.run_withdraw(ARGS, DSN) == kb_withdraw.FAILED_CONNECTION
    monkeypatch.setattr(kb_withdraw.kb_common, "connect", refuse(LimitNotSet("x")))
    assert kb_withdraw.run_withdraw(ARGS, DSN) == kb_withdraw.FAILED_CONFIGURATION


def test_an_unset_withdraw_limit_fails_closed_before_the_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb_withdraw.kb_common, "WITHDRAW_LOCK_TIMEOUT_MS", None)
    fake = FakeConnection()
    assert go(monkeypatch, fake) == kb_withdraw.FAILED_CONFIGURATION
    assert not fake.has("INSERT INTO")


def test_a_failing_commit_exits_12_and_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(commit_error=psycopg.OperationalError("connection dropped"))
    assert go(monkeypatch, fake) == kb_withdraw.OUTCOME_UNKNOWN
    assert fake.out_lines() == [] and not fake.selfcheck_ran()


# --- already withdrawn ---------------------------------------------------------------------------


def check_already_withdrawn(monkeypatch: pytest.MonkeyPatch, run: Run | None = None) -> None:
    fake = FakeConnection(insert_error=db_error("KB001", "this version is already withdrawn"))
    assert go(monkeypatch, fake, run) == kb_withdraw.ALREADY_WITHDRAWN
    assert fake.out_lines() == [f"recorded_at={EARLIER_RECORDED_AT.isoformat()}"]
    insert = fake.index_of("INSERT INTO public.kb_status_events")
    read = fake.index_of("SELECT recorded_at", insert)
    assert fake.log[read][0] > fake.log[insert][0]  # a NEW transaction
    own = fake.index_of("SET LOCAL statement_timeout = 10000", insert)
    assert fake.log[own][0] == fake.log[read][0] and own < read
    assert fake.committed_inserts == 0 and not fake.selfcheck_ran()


def test_already_withdrawn_prints_the_existing_recorded_at_from_a_new_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_already_withdrawn(monkeypatch)


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            (
                '        _emit(sys.stdout, f"recorded_at={existing.isoformat()}")\n',
                "",
            ),
            id="prints nothing",
        ),
        pytest.param(
            ("existing.isoformat()", "datetime(2000, 1, 1).isoformat()"),
            id="prints another time",
        ),
        pytest.param(
            (
                "        conn.rollback()\n        conn.read_only = True\n        _timeout(",
                "        _timeout(",
            ),
            id="reuses the aborted transaction",
        ),
        pytest.param(
            (
                '        row = conn.execute(\n            "SELECT recorded_at FROM',
                '        row = conn.execute(\n            "SELECT recorded_at FROM',
            ),
            id="control (no change) is not caught",
        ),
    ],
)
def test_mutation_in_the_existing_recorded_at_read_is_caught(
    monkeypatch: pytest.MonkeyPatch, edit: tuple[str, str]
) -> None:
    broken = mutant(edit)
    if edit[0] == edit[1]:
        check_already_withdrawn(monkeypatch, broken)  # the control must pass
        return
    with pytest.raises(AssertionError):
        check_already_withdrawn(monkeypatch, broken)


def test_the_match_is_on_sqlstate_kb001_not_p0001_and_not_the_message() -> None:
    assert kb_withdraw.SQLSTATE_ALREADY_WITHDRAWN == "KB001"


def check_kb001_decides(monkeypatch: pytest.MonkeyPatch, run: Run | None = None) -> None:
    fake = FakeConnection(insert_error=db_error("KB001", "wording changed"))
    assert go(monkeypatch, fake, run) == kb_withdraw.ALREADY_WITHDRAWN
    fake = FakeConnection(insert_error=db_error("P0001", "this version is already withdrawn"))
    assert go(monkeypatch, fake, run) == kb_withdraw.FAILED_OTHER


def test_kb001_alone_means_already_withdrawn(monkeypatch: pytest.MonkeyPatch) -> None:
    check_kb001_decides(monkeypatch)


@pytest.mark.parametrize(
    "edit",
    [
        ("exc.sqlstate == SQLSTATE_ALREADY_WITHDRAWN", 'exc.sqlstate == "P0001"'),
        ("exc.sqlstate == SQLSTATE_ALREADY_WITHDRAWN", '"already withdrawn" in str(exc)'),
    ],
    ids=["matches P0001", "matches the message text"],
)
def test_mutation_matching_something_other_than_kb001_is_caught(
    monkeypatch: pytest.MonkeyPatch, edit: tuple[str, str]
) -> None:
    with pytest.raises(AssertionError):
        check_kb001_decides(monkeypatch, mutant(edit))


@pytest.mark.parametrize(
    "fake",
    [
        FakeConnection(existing_row=None),
        FakeConnection(existing_error=psycopg.errors.QueryCanceled("timeout")),
        FakeConnection(existing_error=psycopg.OperationalError("lost")),
        FakeConnection(existing_row=("not a datetime",)),
    ],
    ids=["no row", "statement timeout", "connection lost", "wrong type"],
)
def test_an_unreadable_recorded_at_exits_15_and_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, fake: FakeConnection
) -> None:
    fake.insert_error = db_error("KB001")
    assert go(monkeypatch, fake) == kb_withdraw.ALREADY_WITHDRAWN_NO_TIME
    assert fake.out_lines() == []


def test_already_withdrawn_and_an_over_privileged_role_report_only_already_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection(insert_error=db_error("KB001"), selfcheck_flags={3: True})
    assert go(monkeypatch, fake) == kb_withdraw.ALREADY_WITHDRAWN
    assert not fake.selfcheck_ran()
    assert not any("over-privileged" in text for _, text in fake.log)


def test_a_rerun_after_an_unknown_commit_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeConnection(commit_error=psycopg.OperationalError("dropped"))
    assert go(monkeypatch, first) == kb_withdraw.OUTCOME_UNKNOWN
    second = FakeConnection(insert_error=db_error("KB001"))
    assert go(monkeypatch, second) == kb_withdraw.ALREADY_WITHDRAWN
    assert second.committed_inserts == 0 and len(second.out_lines()) == 1


# --- main(): fixed messages, stream, no echo -----------------------------------------------------


def test_main_writes_the_fixed_message_to_stderr_on_failure_and_never_the_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pydantic import SecretStr

    settings = type("S", (), {"loader_database_url": SecretStr(DSN)})()
    monkeypatch.setattr(kb_withdraw, "load_loader_settings", lambda: settings)
    fake = FakeConnection(insert_error=db_error("55P03", "never-echoed detail"))
    monkeypatch.setattr(kb_withdraw.kb_common, "connect", lambda dsn: fake)
    status = kb_withdraw.main(["withdraw", *ARGS])
    captured = capsys.readouterr()
    assert status == 6 and captured.err.strip() == kb_withdraw.FAILED_LOCK_BUSY.message
    assert "never-echoed" not in captured.err + captured.out
    assert "site-guide" not in captured.err + captured.out


# --- unexpected failures, a broken stdout, search_path, _timeout ---


def test_a_commit_that_raises_oserror_exits_12(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(commit_error=OSError("broken pipe"))
    assert go(monkeypatch, fake) == kb_withdraw.OUTCOME_UNKNOWN
    assert fake.out_lines() == [] and not fake.selfcheck_ran()


def test_a_non_psycopg_exception_after_the_insert_exits_12(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeConnection(insert_error=RuntimeError("surprise secret-text"))
    assert go(monkeypatch, fake) == kb_withdraw.OUTCOME_UNKNOWN
    assert fake.out_lines() == [] and not fake.committed()


@pytest.mark.parametrize("error", [RuntimeError("x"), KeyError("x"), OSError("x")])
def test_a_non_psycopg_exception_before_the_insert_exits_11(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    fake = FakeConnection()
    original = fake._answer

    def answer(text: str) -> Any:
        if "FROM public.kb_documents" in text:
            raise error
        return original(text)

    fake._answer = answer  # type: ignore[method-assign]
    assert go(monkeypatch, fake) == kb_withdraw.FAILED_OTHER
    assert not fake.has("INSERT INTO") and fake.out_lines() == []


def test_mutation_the_exit_for_an_unexpected_exception_swapped_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swapped = mutant(
        (
            "    except Exception:  # noqa: BLE001 - the INSERT was sent; its fate is unknown\n"
            "        _discard(conn)\n"
            "        return OUTCOME_UNKNOWN",
            "    except Exception:  # noqa: BLE001\n"
            "        _discard(conn)\n"
            "        return FAILED_OTHER",
        )
    )
    fake = FakeConnection(insert_error=RuntimeError("x"))
    assert go(monkeypatch, fake, swapped) != kb_withdraw.OUTCOME_UNKNOWN
    fake = FakeConnection(insert_error=RuntimeError("x"))
    assert go(monkeypatch, fake) == kb_withdraw.OUTCOME_UNKNOWN


class _BrokenOut:
    def write(self, text: str) -> int:
        raise OSError("closed")

    def flush(self) -> None:
        raise OSError("closed")


def test_a_broken_stdout_after_the_commit_never_raises_and_keeps_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection()
    monkeypatch.setattr(kb_withdraw.kb_common, "connect", lambda dsn: fake)
    monkeypatch.setattr("sys.stdout", _BrokenOut())
    outcome = kb_withdraw.run_withdraw(ARGS, DSN)  # no traceback
    # SRC QUESTION: the event is committed, yet the status is 12 ("re-run"); a re-run then prints
    # the time as "already withdrawn". The status is pinned as the source defines it.
    assert fake.committed_inserts == 1
    assert outcome == kb_withdraw.OUTCOME_UNKNOWN


def test_a_broken_stdout_on_the_already_withdrawn_print_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeConnection(insert_error=db_error("KB001"))
    monkeypatch.setattr(kb_withdraw.kb_common, "connect", lambda dsn: fake)
    monkeypatch.setattr("sys.stdout", _BrokenOut())
    assert kb_withdraw.run_withdraw(ARGS, DSN) == kb_withdraw.ALREADY_WITHDRAWN


def test_main_survives_a_closed_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stderr", _BrokenOut())
    assert kb_withdraw.main([]) == kb_withdraw.FAILED_ARGUMENTS.status


def test_the_timeout_helper_refuses_any_other_setting_name() -> None:
    class Conn:
        def execute(self, *a: Any) -> None:
            raise AssertionError("must not execute")

    for name in ("idle_session_timeout", "lock_timeout; DROP", "", "work_mem"):
        with pytest.raises(ValueError):  # noqa: PT011
            kb_withdraw._timeout(Conn(), name, 5)  # type: ignore[arg-type]


def test_mutation_a_timeout_helper_that_takes_any_name_is_caught() -> None:
    source = inspect.getsource(kb_withdraw._timeout)
    old = "    if name not in _TIMEOUT_NAMES:\n        raise ValueError"
    assert old in source
    ns: dict[str, Any] = {}
    exec(source.replace(old, "    if False:\n        raise ValueError"), vars(kb_withdraw), ns)  # noqa: S102
    seen: list[Any] = []

    class Conn:
        def execute(self, q: Any) -> None:
            seen.append(q)

    ns["_timeout"](Conn(), "work_mem", 5)
    assert seen  # the mutant accepts it; the real helper raises (previous test)


SEARCH_PATH = 'SET LOCAL search_path = "pg_catalog", "public"'


def check_search_path_first(monkeypatch: pytest.MonkeyPatch, run: Run | None = None) -> None:
    fake = FakeConnection()
    go(monkeypatch, fake, run)
    path = fake.index_of(SEARCH_PATH)
    insert = fake.index_of("INSERT INTO public.kb_status_events")
    lookup = fake.index_of("FROM public.kb_documents")
    assert path < lookup < insert
    assert fake.log[path][0] == fake.log[insert][0]


def test_search_path_is_pinned_inside_the_withdraw_transaction_before_the_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_search_path_first(monkeypatch)


def test_mutation_without_the_search_path_statement_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = (
        "    conn.execute(\n"
        '        sql.SQL("SET LOCAL search_path = {}, {}").format(\n'
        '            sql.Identifier("pg_catalog"), sql.Identifier("public")\n'
        "        )\n"
        "    )\n"
    )
    with pytest.raises(AssertionError):
        check_search_path_first(monkeypatch, mutant((old, "")))
