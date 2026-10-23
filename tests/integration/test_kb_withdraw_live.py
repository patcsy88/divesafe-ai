"""`withdraw` against a real PostgreSQL as a real loader role (ADR 0024 A3; ADR 0022 stage 1b).

Needs DIVESAFE_TEST_DATABASE_URL (a database named *_test); skipped otherwise. Offline these
only collect and skip, so they prove nothing until a live run. The lock timeout is patched to 0.5 s
(statement timeout 5 s, still above it) for speed; the approved 10 s / 30 s values are pinned by
the offline tests. Scratch roles end in _test and are dropped in a finally. The DSN is never
printed or stored."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import psycopg
import pytest
from tests.integration.test_knowledge_store import (
    PASSWORD,
    _add_doc,
    _add_event,
    _drop_role,
    _fresh_id,
    _role_dsn,
    _run,
)
from tests.unit.test_repository_contract import TEST_DSN as DSN

from divesafe.services import kb_common, kb_privileges, kb_withdraw
from divesafe.services.postgres import install_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
]

APP = "divesafe_kb_wdapp_test"
LOADER = "divesafe_kb_wdloader_test"
SHORT_LOCK_MS = 500
SHORT_STATEMENT_MS = 5_000
EXACT_OK = {kb_withdraw.OK.status}  # the scratch loader is least-privilege: 13 is a finding


@pytest.fixture(scope="module")
def loader_dsn() -> Iterator[str]:
    assert DSN is not None
    name = str(psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", ""))
    if not name.endswith("_test"):
        raise RuntimeError("DIVESAFE_TEST_DATABASE_URL must point at a database named *_test")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for role in (APP, LOADER):
            _drop_role(conn, role)
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'")  # type: ignore[call-overload]
    try:
        install_schema(DSN, APP, loader_role=LOADER)
        yield _role_dsn(LOADER)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for role in (APP, LOADER):
                _drop_role(conn, role)


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kb_common, "WITHDRAW_LOCK_TIMEOUT_MS", SHORT_LOCK_MS)
    monkeypatch.setattr(kb_common, "WITHDRAW_STATEMENT_TIMEOUT_MS", SHORT_STATEMENT_MS)


def _draft(did: str, version: int = 1) -> None:
    assert DSN is not None
    _add_doc(DSN, did, version)
    _add_event(DSN, did, "DRAFT", version)


def _events(did: str) -> list[tuple[Any, ...]]:
    assert DSN is not None
    return _run(
        DSN,
        "SELECT status, hash_checked, recorded_at FROM public.kb_status_events "
        "WHERE document_id = %s ORDER BY event_order",
        (did,),
    )


def _withdraw(dsn: str, did: str, version: int = 1) -> kb_withdraw.Outcome:
    result = kb_withdraw.run_withdraw([did, str(version)], dsn)
    return kb_withdraw.Outcome(result.status, result.message)


def _recorded_lines(captured: str) -> list[str]:
    return [line for line in captured.splitlines() if line.startswith("recorded_at=")]


# --- end to end ----------------------------------------------------------------------------------


def test_withdraw_as_the_loader_writes_a_withdrawn_event_and_prints_its_recorded_at(
    loader_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    did = _fresh_id()
    _draft(did)
    outcome = _withdraw(loader_dsn, did)
    # Exactly 0: the scratch loader is provisioned least-privilege. A 13 here would be a real
    # finding (for example PUBLIC CREATE on schema public before PostgreSQL 15), not noise.
    assert outcome.status in EXACT_OK
    events = _events(did)
    assert [e[0] for e in events] == ["DRAFT", "WITHDRAWN"]
    assert events[1][1] is False
    printed = _recorded_lines(capsys.readouterr().out)
    assert printed == [f"recorded_at={events[1][2].isoformat()}"]
    assert isinstance(events[1][2], datetime)


def test_withdrawing_an_already_withdrawn_version_exits_16_with_the_existing_recorded_at(
    loader_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    did = _fresh_id()
    _draft(did)
    assert _withdraw(loader_dsn, did).status in EXACT_OK
    first = _events(did)[1][2]
    capsys.readouterr()
    outcome = _withdraw(loader_dsn, did)
    assert outcome == kb_withdraw.ALREADY_WITHDRAWN
    assert _recorded_lines(capsys.readouterr().out) == [f"recorded_at={first.isoformat()}"]
    assert [e[0] for e in _events(did)] == ["DRAFT", "WITHDRAWN"]  # nothing was written


def test_a_missing_version_exits_10_and_writes_nothing(loader_dsn: str) -> None:
    assert _withdraw(loader_dsn, _fresh_id()) == kb_withdraw.FAILED_NOT_FOUND


def test_running_as_the_owner_is_refused_before_anything_is_written() -> None:
    assert DSN is not None
    did = _fresh_id()
    _draft(did)
    assert _withdraw(DSN, did) == kb_withdraw.FAILED_ROLE
    assert [e[0] for e in _events(did)] == ["DRAFT"]


def test_a_withdrawn_version_can_be_withdrawn_again_only_as_already_withdrawn(
    loader_dsn: str,
) -> None:
    did = _fresh_id()
    _draft(did)
    _withdraw(loader_dsn, did)
    for _ in range(2):
        assert _withdraw(loader_dsn, did) == kb_withdraw.ALREADY_WITHDRAWN
    assert [e[0] for e in _events(did)].count("WITHDRAWN") == 1


# --- concurrency and locks -----------------------------------------------------------------------


def test_two_concurrent_withdrawals_give_one_success_and_one_already_withdrawn(
    loader_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kb_common, "WITHDRAW_LOCK_TIMEOUT_MS", 5_000)
    monkeypatch.setattr(kb_common, "WITHDRAW_STATEMENT_TIMEOUT_MS", 20_000)
    did = _fresh_id()
    _draft(did)
    results: list[kb_withdraw.Outcome] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        barrier.wait(10)
        results.append(_withdraw(loader_dsn, did))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    statuses = sorted(r.status for r in results)
    assert len(results) == 2
    assert statuses.count(kb_withdraw.ALREADY_WITHDRAWN.status) == 1
    assert len([s for s in statuses if s in EXACT_OK]) == 1
    assert [e[0] for e in _events(did)].count("WITHDRAWN") == 1


def test_withdraw_succeeds_while_another_session_holds_the_global_load_lock(
    loader_dsn: str,
) -> None:
    """A mutation that makes withdraw take the global lock (class 1220002) would wait here until
    the short lock timeout and exit 6; this test is what notices
    (the mutation itself has not been run live)."""
    assert DSN is not None
    did = _fresh_id()
    _draft(did)
    with psycopg.connect(DSN) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),
        )
        started = time.monotonic()
        outcome = _withdraw(loader_dsn, did)
        elapsed = time.monotonic() - started
    assert outcome.status in EXACT_OK
    assert elapsed < SHORT_LOCK_MS / 1000 + 2
    assert [e[0] for e in _events(did)] == ["DRAFT", "WITHDRAWN"]


def test_withdraw_behind_the_same_documents_lock_refuses_within_the_lock_timeout(
    loader_dsn: str,
) -> None:
    """Uses a 0.5 s lock timeout (patched), not the approved 10 s, for speed."""
    assert DSN is not None
    did = _fresh_id()
    _draft(did)
    with psycopg.connect(DSN) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (kb_common.KB_DOCUMENT_LOCK_CLASS, did),
        )
        started = time.monotonic()
        outcome = _withdraw(loader_dsn, did)
        elapsed = time.monotonic() - started
    assert outcome == kb_withdraw.FAILED_LOCK_BUSY
    assert SHORT_LOCK_MS / 1000 <= elapsed < SHORT_STATEMENT_MS / 1000
    assert [e[0] for e in _events(did)] == ["DRAFT"]


def test_a_lock_on_another_document_does_not_block_a_withdrawal(loader_dsn: str) -> None:
    assert DSN is not None
    did, other = _fresh_id(), _fresh_id()
    _draft(did)
    with psycopg.connect(DSN) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (kb_common.KB_DOCUMENT_LOCK_CLASS, other),
        )
        # (a hashtext collision with `other` is possible but astronomically unlikely)
        assert _withdraw(loader_dsn, did).status in EXACT_OK


# --- an over-privileged loader still withdraws ---------------------------------------------------


def test_an_over_privileged_loader_still_withdraws_and_exits_13(
    loader_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert DSN is not None
    did = _fresh_id()
    _draft(did)
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"GRANT UPDATE ON public.kb_documents TO {LOADER}")  # type: ignore[call-overload]
    try:
        outcome = _withdraw(loader_dsn, did)
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"REVOKE UPDATE ON public.kb_documents FROM {LOADER}")  # type: ignore[call-overload]
    assert outcome == kb_withdraw.WITHDRAWN_OVER_PRIVILEGED
    events = _events(did)
    assert [e[0] for e in events] == ["DRAFT", "WITHDRAWN"]
    assert _recorded_lines(capsys.readouterr().out) == [f"recorded_at={events[1][2].isoformat()}"]


# --- the status-event trigger and the isolation level --------------------------------------------


@pytest.mark.parametrize(
    "level", [psycopg.IsolationLevel.REPEATABLE_READ, psycopg.IsolationLevel.SERIALIZABLE]
)
@pytest.mark.parametrize("who", ["owner", "loader"])
def test_a_status_event_insert_under_a_stricter_isolation_level_is_refused(
    loader_dsn: str, who: str, level: psycopg.IsolationLevel
) -> None:
    assert DSN is not None
    dsn = DSN if who == "owner" else loader_dsn
    did = _fresh_id()
    _add_doc(DSN, did)
    with pytest.raises(psycopg.errors.RaiseException, match="READ COMMITTED"):
        _run(
            dsn,
            "INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) "
            "VALUES (%s, 1, 'DRAFT', false)",
            (did,),
            isolation=level,
        )
    assert _events(did) == []


# --- the self-check against a real server ---
# One live test per check where a grant can provoke it. Checks WITHOUT a live test:
#  0 (member of the owner/app/bootstrap role by reserved name): needs a role with a reserved
#    name, which would collide with a real deployment's roles; covered offline only.
#  1-3 (owns a knowledge table / table missing), 4-5 (owns the schema / database), 24 (owns a
#    function): would need an ownership transfer of objects the migration owns; covered offline.
#  16 is covered by test_temporary_reached_only_through_a_noinherit_role_is_reported below.
#  17 (session_replication_role other than origin NOW): the loader's own session starts at its
#    default; only the stored setting (18) can be provoked from outside.
# The SQL of those checks is therefore not proven against a server by this file.

_X_ROLE = "divesafe_kb_wdextra_test"


def _admin(*statements: str) -> None:
    assert DSN is not None
    name = str(psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", ""))
    with psycopg.connect(DSN, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement.replace("{db}", f'"{name}"'))  # type: ignore[call-overload]


def _server_version() -> int:
    assert DSN is not None
    with psycopg.connect(DSN) as conn:
        return conn.info.server_version


def _problems(dsn: str) -> list[str]:
    conn = psycopg.connect(dsn)
    try:
        return kb_privileges.loader_privilege_problems(conn, statement_timeout_ms=15_000)
    finally:
        conn.close()


def _label(index: int) -> str:
    return kb_privileges._checks()[index][1]


def test_a_correctly_provisioned_loader_has_no_problems_on_postgresql_15_and_later(
    loader_dsn: str,
) -> None:
    if _server_version() < 150000:
        pytest.skip("before PostgreSQL 15 PUBLIC may CREATE in schema public, a real finding")
    problems = _problems(loader_dsn)
    assert problems == []


CASES: list[tuple[str, list[str], list[str], int]] = [
    ("superuser", [f"ALTER ROLE {LOADER} SUPERUSER"], [f"ALTER ROLE {LOADER} NOSUPERUSER"], 8),
    (
        "member of another role",
        [f"CREATE ROLE {_X_ROLE}", f"GRANT {_X_ROLE} TO {LOADER}"],
        [f"REVOKE {_X_ROLE} FROM {LOADER}", f"DROP ROLE {_X_ROLE}"],
        9,
    ),
    (
        "replication",
        [f"ALTER ROLE {LOADER} REPLICATION"],
        [f"ALTER ROLE {LOADER} NOREPLICATION"],
        10,
    ),
    ("createrole", [f"ALTER ROLE {LOADER} CREATEROLE"], [f"ALTER ROLE {LOADER} NOCREATEROLE"], 11),
    ("createdb", [f"ALTER ROLE {LOADER} CREATEDB"], [f"ALTER ROLE {LOADER} NOCREATEDB"], 12),
    ("bypassrls", [f"ALTER ROLE {LOADER} BYPASSRLS"], [f"ALTER ROLE {LOADER} NOBYPASSRLS"], 13),
    (
        "program or file role",
        [f"GRANT pg_execute_server_program TO {LOADER}"],
        [f"REVOKE pg_execute_server_program FROM {LOADER}"],
        14,
    ),
    (
        "temporary on the database",
        ["GRANT TEMPORARY ON DATABASE {db} TO " + LOADER],
        ["REVOKE TEMPORARY ON DATABASE {db} FROM " + LOADER],
        15,
    ),
    (
        "create in schema public",
        [f"GRANT CREATE ON SCHEMA public TO {LOADER}"],
        [f"REVOKE CREATE ON SCHEMA public FROM {LOADER}"],
        6,
    ),
    (
        "create schema in the database",
        ["GRANT CREATE ON DATABASE {db} TO " + LOADER],
        ["REVOKE CREATE ON DATABASE {db} FROM " + LOADER],
        7,
    ),
    (
        "stored session_replication_role",
        [f"ALTER ROLE {LOADER} SET session_replication_role = replica"],
        [f"ALTER ROLE {LOADER} RESET session_replication_role"],
        18,
    ),
    (
        "update on a knowledge table",
        [f"GRANT UPDATE ON public.kb_documents TO {LOADER}"],
        [f"REVOKE UPDATE ON public.kb_documents FROM {LOADER}"],
        19,
    ),
    (
        "column update on a knowledge table",
        [f"GRANT UPDATE (document_id) ON public.kb_status_events TO {LOADER}"],
        [f"REVOKE UPDATE (document_id) ON public.kb_status_events FROM {LOADER}"],
        20,
    ),
    (
        "insert beyond the column grant",
        [f"GRANT INSERT (recorded_at) ON public.kb_status_events TO {LOADER}"],
        [f"REVOKE INSERT (recorded_at) ON public.kb_status_events FROM {LOADER}"],
        21,
    ),
    (
        "ordering sequence",
        [f"GRANT USAGE ON SEQUENCE public.kb_status_event_order_seq TO {LOADER}"],
        [f"REVOKE USAGE ON SEQUENCE public.kb_status_event_order_seq FROM {LOADER}"],
        22,
    ),
    (
        "execute a knowledge function",
        [f"GRANT EXECUTE ON FUNCTION public.kb_no_truncate() TO {LOADER}"],
        [f"REVOKE EXECUTE ON FUNCTION public.kb_no_truncate() FROM {LOADER}"],
        23,
    ),
    (
        "assessments",
        [f"GRANT SELECT ON public.assessments TO {LOADER}"],
        [f"REVOKE SELECT ON public.assessments FROM {LOADER}"],
        25,
    ),
    (
        "pending_runs",
        [f"GRANT SELECT ON public.pending_runs TO {LOADER}"],
        [f"REVOKE SELECT ON public.pending_runs FROM {LOADER}"],
        26,
    ),
    (
        "run_failure_counts",
        [f"GRANT SELECT ON public.run_failure_counts TO {LOADER}"],
        [f"REVOKE SELECT ON public.run_failure_counts FROM {LOADER}"],
        27,
    ),
    (
        "schema_migrations",
        [f"GRANT SELECT ON public.schema_migrations TO {LOADER}"],
        [f"REVOKE SELECT ON public.schema_migrations FROM {LOADER}"],
        28,
    ),
]


@pytest.mark.parametrize(
    ("setup", "undo", "index"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_each_provoked_problem_is_reported_with_its_label(
    loader_dsn: str, setup: list[str], undo: list[str], index: int
) -> None:
    try:
        _admin(*setup)
        problems = _problems(loader_dsn)
    finally:
        _admin(*undo)
    assert _label(index) in problems  # other labels may join it (membership implies more)


def test_the_loader_is_clean_again_after_the_grants_are_undone(loader_dsn: str) -> None:
    if _server_version() < 150000:
        pytest.skip("before PostgreSQL 15 PUBLIC may CREATE in schema public")
    problems = _problems(loader_dsn)
    assert problems == []


def test_the_parameter_privilege_is_reported_on_postgresql_15_and_later(loader_dsn: str) -> None:
    if _server_version() < 150000:
        pytest.skip("GRANT ... ON PARAMETER needs PostgreSQL 15")
    try:
        _admin(f"GRANT SET ON PARAMETER session_replication_role TO {LOADER}")
        problems = _problems(loader_dsn)
    finally:
        _admin(f"REVOKE SET ON PARAMETER session_replication_role FROM {LOADER}")
    assert "may set session_replication_role" in problems


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP: running_as_privileged_role matches the app role by the reserved NAMES only; "
        "the loader does not know a custom app role's name (open question, ADR 0024 A3). When the "
        "name is supplied this test starts to pass and strict xfail will say so."
    ),
)
def test_a_loader_in_a_custom_named_app_role_is_refused_before_a_write(loader_dsn: str) -> None:
    try:
        _admin(f"GRANT {APP} TO {LOADER}")
        conn = psycopg.connect(loader_dsn)
        try:
            privileged = kb_privileges.running_as_privileged_role(conn, statement_timeout_ms=15_000)
        finally:
            conn.close()
    finally:
        _admin(f"REVOKE {APP} FROM {LOADER}")
    assert privileged is True


_T_ROLE = "divesafe_kb_wdtemp_test"


def test_temporary_reached_only_through_a_noinherit_role_is_reported_as_check_16(
    loader_dsn: str,
) -> None:
    """The loader holds no TEMPORARY itself and does not inherit it (NOINHERIT), but it can SET
    ROLE to a member role that has it: check 16, not check 15."""
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as admin:
        _drop_role(admin, _T_ROLE)
    try:
        _admin(
            f"CREATE ROLE {_T_ROLE}",
            "GRANT TEMPORARY ON DATABASE {db} TO " + _T_ROLE,
            f"ALTER ROLE {LOADER} NOINHERIT",
            f"GRANT {_T_ROLE} TO {LOADER}",
        )
        problems = _problems(loader_dsn)
    finally:
        _admin(f"ALTER ROLE {LOADER} INHERIT")
        with psycopg.connect(DSN, autocommit=True) as admin:
            _drop_role(admin, _T_ROLE)  # DROP OWNED revokes its grants, then the role goes
    assert _label(16) in problems
    assert _label(15) not in problems  # the loader itself has no TEMPORARY
    assert _label(9) in problems  # and it is, of course, a member of another role
