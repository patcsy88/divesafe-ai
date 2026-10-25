"""The database enforces append-only on its own: raw SQL, bypassing the application, cannot
rewrite or delete a stored assessment. Needs DIVESAFE_TEST_DATABASE_URL."""

from __future__ import annotations

import json
import os

import psycopg
import pytest
from tests.unit.test_repository_contract import _decided, _postgres, _record

from divesafe.services import PostgresAssessmentRepository
from divesafe.services.postgres import GUARD_TRIGGERS

DSN = os.environ.get("DIVESAFE_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
]


def _printed(exc: BaseException) -> str:
    """What a log handler would print for this exception, chain included."""
    import traceback

    return "".join(traceback.format_exception(exc))


def _sql(statement: str, params: tuple[object, ...] = ()) -> None:
    assert DSN is not None
    with psycopg.connect(DSN) as conn:
        conn.execute(statement, params)  # type: ignore[call-overload]


def _sql_value(statement: str) -> str:
    assert DSN is not None
    with psycopg.connect(DSN) as conn:
        row = conn.execute(statement).fetchone()  # type: ignore[call-overload]
    assert row is not None
    return str(row[0])


@pytest.fixture
def stored() -> PostgresAssessmentRepository:
    repo = _postgres()
    assert isinstance(repo, PostgresAssessmentRepository)
    repo.create(_record())
    return repo


def test_a_stored_assessment_cannot_be_deleted(stored: PostgresAssessmentRepository) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="never deleted"):
        _sql("DELETE FROM assessments WHERE id = 'r1'")
    assert stored.get("r1") is not None


def test_raw_sql_cannot_rewrite_a_stored_field(stored: PostgresAssessmentRepository) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="may be appended"):
        _sql(
            "UPDATE assessments SET record = jsonb_set(record, '{final_recommendation}', "
            "'\"GO\"') WHERE id = 'r1'"
        )
    assert stored.get("r1") == _record()


def test_raw_sql_cannot_change_the_id(stored: PostgresAssessmentRepository) -> None:
    with pytest.raises(psycopg.errors.RaiseException):
        _sql("UPDATE assessments SET id = 'other' WHERE id = 'r1'")


def test_raw_sql_cannot_replace_or_erase_a_decision(
    stored: PostgresAssessmentRepository,
) -> None:
    pending = _record()
    decided = _decided(pending, "alice")
    stored.replace(pending, decided)
    other = json.loads(_decided(pending, "mallory").model_dump_json())
    with pytest.raises(psycopg.errors.RaiseException):
        _sql("UPDATE assessments SET record = %s::jsonb WHERE id = 'r1'", (json.dumps(other),))
    with pytest.raises(psycopg.errors.RaiseException):
        _sql("UPDATE assessments SET record = record - 'human_decision' WHERE id = 'r1'")
    with pytest.raises(psycopg.errors.RaiseException):
        _sql(
            "UPDATE assessments SET record = jsonb_set(record, '{human_decision}', 'null') "
            "WHERE id = 'r1'"
        )
    assert stored.get("r1") == decided


def test_raw_sql_can_append_the_first_decision_but_only_once(
    stored: PostgresAssessmentRepository,
) -> None:
    first = json.loads(_decided(_record(), "alice").model_dump_json())
    _sql("UPDATE assessments SET record = %s::jsonb WHERE id = 'r1'", (json.dumps(first),))
    second = json.loads(_decided(_record(), "bob").model_dump_json())
    with pytest.raises(psycopg.errors.RaiseException):
        _sql("UPDATE assessments SET record = %s::jsonb WHERE id = 'r1'", (json.dumps(second),))


def test_raw_sql_cannot_rewrite_or_erase_actual_conditions(
    stored: PostgresAssessmentRepository,
) -> None:
    from datetime import timedelta

    from tests.conftest import NOW

    from divesafe.domain import ActualConditions, AssessmentRecord

    pending = _record()
    decided = _decided(pending)
    stored.replace(pending, decided)
    actual = ActualConditions(
        reported_at=NOW + timedelta(hours=2), reported_by="a", observations={}
    )
    done = AssessmentRecord.model_validate(
        {**decided.model_dump(), "actual_conditions": actual.model_dump()}
    )
    stored.replace(decided, done)
    forged = ActualConditions(
        reported_at=NOW + timedelta(hours=3), reported_by="mallory", observations={"x": 1}
    )
    for statement, params in (
        (
            "UPDATE assessments SET record = jsonb_set(record, '{actual_conditions}', %s::jsonb)",
            (forged.model_dump_json(),),
        ),
        ("UPDATE assessments SET record = record - 'actual_conditions'", ()),
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            _sql(statement, params)
    assert stored.get("r1") == done


def test_a_writer_waiting_on_the_row_lock_gets_a_conflict_not_a_database_error(
    stored: PostgresAssessmentRepository,
) -> None:
    """Deterministic: another transaction holds the row; our replace must block on it and, once
    it commits a decision, see the changed record and raise ConflictError."""
    import threading
    import time

    from divesafe.services import ConflictError

    assert DSN is not None
    pending = _record()
    outcome: list[BaseException | str] = []

    def contender() -> None:
        try:
            stored.replace(pending, _decided(pending, "bob"))
            outcome.append("ok")
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    with psycopg.connect(DSN) as holder, holder.transaction():
        holder.execute("SELECT 1 FROM assessments WHERE id = 'r1' FOR UPDATE")
        thread = threading.Thread(target=contender)
        thread.start()
        time.sleep(0.5)
        assert outcome == []  # still blocked on the lock
        holder.execute(
            "UPDATE assessments SET record = %s::jsonb WHERE id = 'r1'",
            (_decided(pending, "alice").model_dump_json(),),
        )
    thread.join(timeout=10)
    assert len(outcome) == 1 and isinstance(outcome[0], ConflictError), outcome


def test_the_api_stores_and_decides_through_postgres(stored: PostgresAssessmentRepository) -> None:
    from fastapi.testclient import TestClient
    from tests.api_polling import create_and_poll, started
    from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build

    from divesafe.api.app import create_app

    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.repository = stored
    api = started(TestClient(create_app(state)))
    created = create_and_poll(api, PLAN, AUTH_A).json()
    aid = created["record"]["id"]
    assert stored.get(aid) is not None
    decision = {"decision": "INSUFFICIENT EVIDENCE", "rationale": "reviewed"}
    assert (
        api.post(f"/v1/assessments/{aid}/decision", json=decision, headers=AUTH_A).status_code
        == 200
    )
    assert (
        api.post(f"/v1/assessments/{aid}/decision", json=decision, headers=AUTH_A).status_code
        == 409
    )
    stored_record = stored.get(aid)
    assert stored_record is not None and stored_record.human_decision is not None


# --- review findings: forged decisions, ordering, inserts, truncate, created_at -----------------


def _set_record(doc: dict[str, object]) -> None:
    _sql("UPDATE assessments SET record = %s::jsonb WHERE id = 'r1'", (json.dumps(doc),))


def _base() -> dict[str, object]:
    base: dict[str, object] = json.loads(_record().model_dump_json())
    return base


@pytest.mark.parametrize(
    "forged",
    [
        "not an object",
        {},
        {"decided_by": "  ", "decision": "GO", "is_override": False},
        {"decided_by": "mallory", "decision": "SAFE", "is_override": False},
        {"decided_by": "mallory", "decision": "GO", "is_override": "no"},
        {"decided_by": "mallory", "decision": "GO", "is_override": True},
        {"decided_by": "mallory", "decision": "GO", "is_override": True, "override_rationale": " "},
    ],
)
def test_the_database_refuses_a_malformed_first_decision(
    stored: PostgresAssessmentRepository, forged: object
) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="malformed"):
        _set_record({**_base(), "human_decision": forged})
    assert stored.get("r1") == _record()


def test_the_database_requires_a_decision_before_actual_conditions(
    stored: PostgresAssessmentRepository,
) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="require a decision"):
        _set_record({**_base(), "actual_conditions": {"reported_by": "x"}})


def test_a_new_row_cannot_arrive_already_decided_or_with_a_mismatched_id(
    stored: PostgresAssessmentRepository,
) -> None:
    decided = json.loads(_decided(_record("r2")).model_dump_json())
    with pytest.raises(psycopg.errors.RaiseException):
        _sql(
            "INSERT INTO assessments (id, record) VALUES ('r2', %s::jsonb)", (json.dumps(decided),)
        )
    with pytest.raises(psycopg.errors.RaiseException):
        _sql(
            "INSERT INTO assessments (id, record) VALUES ('r3', %s::jsonb)", (json.dumps(_base()),)
        )
    with pytest.raises(psycopg.errors.Error):
        _sql("INSERT INTO assessments (id, record) VALUES ('r4', '[]'::jsonb)")


def test_truncate_is_refused(stored: PostgresAssessmentRepository) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="never truncated"):
        _sql("TRUNCATE assessments")
    assert stored.get("r1") is not None


def test_created_at_cannot_be_rewritten(stored: PostgresAssessmentRepository) -> None:
    with pytest.raises(psycopg.errors.RaiseException):
        _sql("UPDATE assessments SET created_at = created_at - interval '3 days'")


def test_the_repository_refuses_to_create_an_already_decided_record() -> None:
    from divesafe.services import InvalidSuccessorError

    repo = _postgres()
    with pytest.raises(InvalidSuccessorError):
        repo.create(_decided(_record()))


def test_the_test_fixture_refuses_a_database_not_named_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.unit.test_repository_contract as contract

    monkeypatch.setattr(contract, "TEST_DSN", "postgresql://u:p@localhost:5432/divesafe")
    with pytest.raises(RuntimeError, match="_test"):
        contract.reset_test_database()


# --- what cannot be stored faithfully ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a\x00b", float("nan"), float("inf")])
def test_content_that_jsonb_would_reject_or_alter_is_refused_before_anything_is_stored(
    bad: object,
) -> None:
    from divesafe.domain import AssessmentRecord
    from divesafe.services import UnstorableRecordError

    repo = _postgres()
    record = _record()
    evidence = record.evidence[0].model_copy(update={"value": {"x": bad}})
    tainted = AssessmentRecord.model_validate(
        {**record.model_dump(), "evidence": (evidence.model_dump(),)}
    )
    with pytest.raises(UnstorableRecordError):
        repo.create(tainted)
    assert repo.get(record.id) is None


def test_a_value_that_would_change_type_in_a_round_trip_is_refused() -> None:
    from divesafe.domain import AssessmentRecord
    from divesafe.services import UnstorableRecordError

    repo = _postgres()
    record = _record()
    evidence = record.evidence[0].model_copy(update={"value": {"x": 1e22}})
    payload = {**record.model_dump(), "evidence": (evidence.model_dump(),)}
    tainted = AssessmentRecord.model_validate(payload)
    with pytest.raises(UnstorableRecordError, match="read back identical"):
        repo.create(tainted)
    assert repo.get(record.id) is None  # the insert was rolled back


def test_an_update_that_applies_to_no_row_is_not_reported_as_success(
    stored: PostgresAssessmentRepository,
) -> None:
    from divesafe.services import RepositoryIntegrityError

    _sql("CREATE RULE swallow AS ON UPDATE TO assessments DO INSTEAD NOTHING")
    try:
        pending = _record()
        with pytest.raises(RepositoryIntegrityError):
            stored.replace(pending, _decided(pending))
    finally:
        _sql("DROP RULE swallow ON assessments")
    assert stored.get("r1") == _record()


def test_the_api_does_not_block_the_event_loop_on_storage() -> None:
    import asyncio

    from fastapi.testclient import TestClient
    from tests.api_polling import create_and_poll, started
    from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build

    from divesafe.api.app import create_app
    from divesafe.services import InMemoryAssessmentRepository

    class Probe(InMemoryAssessmentRepository):
        seen: list[bool] = []

        def _probe(self) -> None:
            try:
                asyncio.get_running_loop()
                Probe.seen.append(True)
            except RuntimeError:
                Probe.seen.append(False)

        def admit(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            self._probe()
            return super().admit(*args, **kwargs)  # type: ignore[arg-type]

        def complete(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            self._probe()
            return super().complete(*args, **kwargs)  # type: ignore[arg-type]

    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.repository = Probe()
    create_and_poll(started(TestClient(create_app(state))), PLAN, AUTH_A)
    assert Probe.seen and not any(Probe.seen)  # admission and completion both ran off the loop


# --- outage, errors and secrets ------------------------------------------------------------------


def test_an_unreachable_database_is_a_typed_error_that_carries_no_connection_details() -> None:
    from divesafe.services import RepositoryUnavailableError

    repo = PostgresAssessmentRepository("postgresql://leaky_user:leaky_pw@127.0.0.1:1/leaky_db")
    for call in (lambda: repo.get("r1"), lambda: repo.create(_record())):
        with pytest.raises(RepositoryUnavailableError) as caught:
            call()
        assert "leaky" not in _printed(caught.value)


def test_a_stored_row_that_fails_validation_raises_integrity_error_without_its_content(
    stored: PostgresAssessmentRepository,
) -> None:
    from divesafe.services import RepositoryIntegrityError

    broken = {**_base(), "explanation": {"secret rationale": "do not log"}}
    _sql("ALTER TABLE assessments DISABLE TRIGGER assessments_append_only_guard")
    _set_record(broken)
    _sql("ALTER TABLE assessments ENABLE ALWAYS TRIGGER assessments_append_only_guard")
    with pytest.raises(RepositoryIntegrityError) as caught:
        stored.get("r1")
    assert "secret rationale" not in _printed(caught.value)


def test_the_api_returns_503_and_records_nothing_when_storage_is_down() -> None:
    from fastapi.testclient import TestClient
    from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build

    from divesafe.api.app import create_app

    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.repository = PostgresAssessmentRepository("postgresql://u:leaky_pw@127.0.0.1:1/db")
    response = TestClient(create_app(state)).post("/v1/assessments", json=PLAN, headers=AUTH_A)
    assert response.status_code == 503 and "leaky_pw" not in response.text
    assert "nothing was recorded" in response.json()["detail"]


# --- least privilege: the real application role --------------------------------------------------


@pytest.fixture
def app_role_dsn() -> str:
    assert DSN is not None
    from divesafe.services.postgres import install_schema

    base = psycopg.conninfo.conninfo_to_dict(DSN)
    with psycopg.connect(DSN, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'divesafe_app_test'"
        ).fetchone()
        if exists:
            conn.execute("DROP OWNED BY divesafe_app_test")
            conn.execute("DROP ROLE divesafe_app_test")
        conn.execute("CREATE ROLE divesafe_app_test LOGIN PASSWORD 'app-test-only'")
    install_schema(DSN, "divesafe_app_test")
    base.update(user="divesafe_app_test", password="app-test-only")
    return psycopg.conninfo.make_conninfo(**base)


def test_the_application_role_can_work_but_cannot_disable_or_bypass_the_guard(
    app_role_dsn: str,
) -> None:
    _postgres()  # empty table, guard enabled
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    app.create(_record())
    pending = _record()
    app.replace(pending, _decided(pending))
    assert app.get("r1") is not None
    for statement in (
        "DELETE FROM assessments",
        "TRUNCATE assessments",
        "ALTER TABLE assessments DISABLE TRIGGER assessments_append_only_guard",
        "DROP TRIGGER assessments_append_only_guard ON assessments",
        "ALTER TABLE assessments DISABLE TRIGGER ALL",
        "CREATE OR REPLACE FUNCTION public.assessments_append_only() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    ):
        with psycopg.connect(app_role_dsn) as conn, pytest.raises(psycopg.errors.Error):
            conn.execute(statement)  # type: ignore[call-overload]


def test_startup_verification_reports_a_missing_disabled_or_overpowered_setup(
    app_role_dsn: str,
) -> None:
    assert DSN is not None
    owner = PostgresAssessmentRepository(DSN)
    assert any("owns the table" in p for p in owner.verify(least_privilege=True))
    assert owner.verify(least_privilege=False) == []
    _sql("ALTER TABLE assessments DISABLE TRIGGER assessments_no_truncate_guard")
    try:
        problems = owner.verify(least_privilege=False)
        assert any("assessments_no_truncate_guard" in p for p in problems)
    finally:
        _sql("ALTER TABLE assessments ENABLE ALWAYS TRIGGER assessments_no_truncate_guard")
    _sql("DROP TABLE assessments")
    assert any("does not exist" in p for p in owner.verify(least_privilege=False))
    _sql("DROP TABLE schema_migrations")  # a fresh database has no history either
    _postgres()  # restore for other tests


def test_verification_flags_an_application_role_that_can_delete(app_role_dsn: str) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    _sql("GRANT DELETE ON assessments TO divesafe_app_test")
    assert any("can DELETE" in p for p in app.verify(least_privilege=True))
    _sql("REVOKE DELETE ON assessments FROM divesafe_app_test")
    _sql("GRANT TRUNCATE ON assessments TO divesafe_app_test")
    assert any("can TRUNCATE" in p for p in app.verify(least_privilege=True))


# --- migrations ------------------------------------------------------------------------------


def test_migrating_twice_applies_nothing_the_second_time() -> None:
    from divesafe.services.migrations import migrate

    _postgres()
    assert migrate(DSN) == []  # type: ignore[arg-type]


def test_a_failing_migration_rolls_back_completely_and_is_not_recorded() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, migrate

    _postgres()
    bad = Migration(len(MIGRATIONS) + 1, "bad", "CREATE TABLE half_done (x int); SELECT 1/0;")
    with pytest.raises(Exception, match="could not apply"):
        migrate(DSN, (*MIGRATIONS, bad))  # type: ignore[arg-type]
    with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
        assert conn.execute("SELECT to_regclass('public.half_done')").fetchone() == (None,)
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (
            len(MIGRATIONS),
        )


def test_a_good_second_migration_is_applied_once_and_recorded() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, migrate

    _postgres()
    nxt = len(MIGRATIONS) + 1
    extra = Migration(nxt, "scratch", "CREATE TABLE migration_scratch (x int)")
    try:
        assert migrate(DSN, (*MIGRATIONS, extra)) == [nxt]  # type: ignore[arg-type]
        assert migrate(DSN, (*MIGRATIONS, extra)) == []  # type: ignore[arg-type]
    finally:
        _sql("DROP TABLE IF EXISTS migration_scratch")
        _sql("DELETE FROM schema_migrations WHERE version = %s", (nxt,))


def test_an_edited_applied_migration_is_refused_by_the_runner() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, MigrationError, migrate

    _postgres()
    edited = Migration(1, MIGRATIONS[0].name, MIGRATIONS[0].sql + "\n-- edited")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(DSN, (edited, *MIGRATIONS[1:]))  # type: ignore[arg-type]


def test_startup_refuses_a_database_behind_or_ahead_of_the_code() -> None:
    from divesafe.services.migrations import MIGRATIONS

    repo = _postgres()
    assert isinstance(repo, PostgresAssessmentRepository)
    try:
        assert repo.verify(least_privilege=False) == []
        _sql("DELETE FROM schema_migrations")
        assert any("schema version 0" in p for p in repo.verify(least_privilege=False))
        _sql(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (1, 'x', %s)",
            ("0" * 64,),
        )
        assert any("changed after" in p for p in repo.verify(least_privilege=False))
        _sql("DELETE FROM schema_migrations")
        _postgres()
        _sql(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, 'future', %s)",
            (len(MIGRATIONS) + 1, "1" * 64),
        )
        assert any("ahead of this code" in p for p in repo.verify(least_privilege=False))
    finally:
        _sql("DROP TABLE IF EXISTS schema_migrations")
        _postgres()


def test_the_application_role_can_read_but_not_write_the_migration_history(
    app_role_dsn: str,
) -> None:
    from divesafe.services.migrations import MIGRATIONS

    with psycopg.connect(app_role_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (
            len(MIGRATIONS),
        )
    for statement in (
        "DELETE FROM schema_migrations",
        "UPDATE schema_migrations SET checksum = repeat('0', 64)",
        "INSERT INTO schema_migrations (version, name, checksum) VALUES (9, 'x', repeat('0', 64))",
        "DROP TABLE schema_migrations",
    ):
        with psycopg.connect(app_role_dsn) as conn, pytest.raises(psycopg.errors.Error):
            conn.execute(statement)  # type: ignore[call-overload]


def test_two_runners_at_once_apply_a_migration_exactly_once() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from divesafe.services.migrations import MIGRATIONS, Migration, migrate

    _postgres()
    nxt = len(MIGRATIONS) + 1
    slow = Migration(nxt, "slow", "SELECT pg_sleep(1); CREATE TABLE migration_scratch (x int)")
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(migrate, DSN, (*MIGRATIONS, slow)) for _ in range(2)]  # type: ignore[arg-type]
            results = sorted(f.result() for f in futures)
        assert results == [[], [nxt]]
    finally:
        _sql("DROP TABLE IF EXISTS migration_scratch")
        _sql("DELETE FROM schema_migrations WHERE version = %s", (nxt,))


def test_a_migration_that_leaves_the_guard_disabled_is_rolled_back() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, MigrationError, migrate

    _postgres()
    sneaky = Migration(
        len(MIGRATIONS) + 1,
        "sneaky",
        "ALTER TABLE public.assessments DISABLE TRIGGER assessments_insert_guard",
    )
    # `guarded` refuses the wording; bypass it to prove the post-check also holds
    import divesafe.services.migrations as mod

    original = mod.guarded
    mod.guarded = lambda known: None  # type: ignore[assignment]
    try:
        with pytest.raises(MigrationError, match="not intact"):
            migrate(DSN, (*MIGRATIONS, sneaky))  # type: ignore[arg-type]
    finally:
        mod.guarded = original
    with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (
            len(MIGRATIONS),
        )
    assert PostgresAssessmentRepository(DSN).verify(least_privilege=False) == []  # type: ignore[arg-type]


def test_migrations_refuse_to_run_as_the_application_role(app_role_dsn: str) -> None:
    from divesafe.services.migrations import MigrationError, migrate

    with pytest.raises(MigrationError, match="owner role"):
        migrate(app_role_dsn, app_role="divesafe_app_test")


@pytest.mark.parametrize("role", ["public", "pg_monitor", "Bad Role", "ok\n"])
def test_dangerous_application_role_names_are_refused(role: str) -> None:
    from divesafe.services.postgres import install_schema

    with pytest.raises(ValueError, match="invalid application role"):
        install_schema(DSN, role)  # type: ignore[arg-type]


def test_startup_flags_an_application_role_that_can_write_the_migration_history(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    _sql("GRANT UPDATE ON schema_migrations TO divesafe_app_test")
    assert any("migration history" in p for p in app.verify(least_privilege=True))


def test_startup_reports_a_role_that_cannot_read_the_migration_history(app_role_dsn: str) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    _sql("REVOKE SELECT ON schema_migrations FROM divesafe_app_test")
    problems = app.verify(least_privilege=True)
    assert any("cannot read the migration history" in p for p in problems)


def test_migrate_itself_refuses_a_migration_that_touches_the_guard() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, MigrationError, migrate

    _postgres()
    evil = Migration(
        len(MIGRATIONS) + 1,
        "evil",
        "DROP TRIGGER assessments_append_only_guard ON public.assessments",
    )
    with pytest.raises(MigrationError, match="touches the audit guard"):
        migrate(DSN, (*MIGRATIONS, evil))  # type: ignore[arg-type]
    assert PostgresAssessmentRepository(DSN).verify(least_privilege=False) == []  # type: ignore[arg-type]


def test_installing_again_takes_back_write_access_to_the_migration_history(
    app_role_dsn: str,
) -> None:
    from divesafe.services.postgres import install_schema

    assert DSN is not None
    _sql("GRANT INSERT, UPDATE, DELETE, TRUNCATE ON schema_migrations TO divesafe_app_test")
    app = PostgresAssessmentRepository(app_role_dsn)
    assert any("migration history" in p for p in app.verify(least_privilege=True))
    install_schema(DSN, "divesafe_app_test")
    assert app.verify(least_privilege=True) == []


# --- pending runs (ADR 0019) -----------------------------------------------------------------


def test_the_application_role_can_use_runs_but_cannot_touch_assessments_through_them(
    app_role_dsn: str,
) -> None:
    from datetime import timedelta

    from tests.unit.test_run_store_contract import _run

    from divesafe.services import NewRun

    _postgres()
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    admission = app.admit(_run(), max_in_flight=4, max_per_actor=4, lease=timedelta(minutes=5))
    assert admission.kind == "started" and admission.run_token
    assert app.cleanup(retention=timedelta(days=7), batch=10).deleted == 0
    assert isinstance(_run(), NewRun)
    for statement in (
        "TRUNCATE pending_runs",
        "ALTER TABLE pending_runs DISABLE TRIGGER ALL",
        "DELETE FROM assessments",
        "DELETE FROM run_failure_counts",
        "TRUNCATE run_failure_counts",
        "DROP TABLE pending_runs",
    ):
        with psycopg.connect(app_role_dsn) as conn, pytest.raises(psycopg.errors.Error):
            conn.execute(statement)  # type: ignore[call-overload]


def test_verification_flags_an_application_role_that_can_truncate_runs_or_edit_counts(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    _sql("GRANT TRUNCATE ON pending_runs TO divesafe_app_test")
    assert any("pending runs table" in p for p in app.verify(least_privilege=True))
    _sql("REVOKE TRUNCATE ON pending_runs FROM divesafe_app_test")
    _sql("GRANT DELETE ON run_failure_counts TO divesafe_app_test")
    assert any("failure counts" in p for p in app.verify(least_privilege=True))


def test_verification_flags_an_application_role_that_owns_the_failure_counts(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    _sql("ALTER TABLE run_failure_counts OWNER TO divesafe_app_test")
    try:
        assert any("owns the failure counts" in p for p in app.verify(least_privilege=True))
    finally:
        _sql("ALTER TABLE run_failure_counts OWNER TO CURRENT_USER")
    assert app.verify(least_privilege=True) == []


def test_verification_flags_an_application_role_that_can_create_objects_in_public(
    app_role_dsn: str,
) -> None:
    """A schema owner or a role with CREATE can drop the table; no trigger fires for that."""
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    _sql("GRANT CREATE ON SCHEMA public TO divesafe_app_test")
    try:
        assert any(
            "create objects in the public schema" in p for p in app.verify(least_privilege=True)
        )
    finally:
        _sql("REVOKE CREATE ON SCHEMA public FROM divesafe_app_test")
    assert app.verify(least_privilege=True) == []


def test_verification_flags_an_application_role_that_owns_the_database_or_the_schema(
    app_role_dsn: str,
) -> None:
    assert DSN is not None
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    name = psycopg.conninfo.conninfo_to_dict(DSN)["dbname"]
    _sql("ALTER SCHEMA public OWNER TO divesafe_app_test")
    try:
        assert any("owns the public schema" in p for p in app.verify(least_privilege=True))
    finally:
        _sql("ALTER SCHEMA public OWNER TO pg_database_owner")
    _sql(f'ALTER DATABASE "{name}" OWNER TO divesafe_app_test')
    try:
        assert any("owns the database" in p for p in app.verify(least_privilege=True))
    finally:
        _sql(f'ALTER DATABASE "{name}" OWNER TO CURRENT_USER')
    assert app.verify(least_privilege=True) == []


def test_verification_flags_an_application_role_that_is_a_member_of_a_superuser_role(
    app_role_dsn: str,
) -> None:
    """The superuser attribute is not inherited, but a member can SET ROLE to the superuser."""
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    _sql("DROP ROLE IF EXISTS divesafe_super_group_test")
    _sql("CREATE ROLE divesafe_super_group_test NOLOGIN SUPERUSER")
    _sql("GRANT divesafe_super_group_test TO divesafe_app_test")
    try:
        assert any("superuser or a member of one" in p for p in app.verify(least_privilege=True))
    finally:
        _sql("REVOKE divesafe_super_group_test FROM divesafe_app_test")
        _sql("DROP ROLE divesafe_super_group_test")
    assert app.verify(least_privilege=True) == []


def test_verification_flags_an_application_role_in_a_server_file_or_program_role(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    for role in ("pg_write_server_files", "pg_execute_server_program", "pg_read_server_files"):
        _sql(f"GRANT {role} TO divesafe_app_test")
        try:
            assert any("read or write files" in p for p in app.verify(least_privilege=True))
        finally:
            _sql(f"REVOKE {role} FROM divesafe_app_test")
    assert app.verify(least_privilege=True) == []


def test_verification_flags_an_application_role_in_a_role_that_can_create_roles(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    _sql("DROP ROLE IF EXISTS divesafe_creator_group_test")
    _sql("CREATE ROLE divesafe_creator_group_test NOLOGIN CREATEROLE")
    _sql("GRANT divesafe_creator_group_test TO divesafe_app_test")
    try:
        assert any("can create roles" in p for p in app.verify(least_privilege=True))
    finally:
        _sql("REVOKE divesafe_creator_group_test FROM divesafe_app_test")
        _sql("DROP ROLE divesafe_creator_group_test")
    assert app.verify(least_privilege=True) == []


def test_verification_flags_an_application_role_that_can_create_roles(app_role_dsn: str) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    _sql("ALTER ROLE divesafe_app_test CREATEROLE")
    try:
        assert any("can create roles" in p for p in app.verify(least_privilege=True))
    finally:
        _sql("ALTER ROLE divesafe_app_test NOCREATEROLE")
    assert app.verify(least_privilege=True) == []


@pytest.mark.parametrize("scope", ["role", "database"])
def test_verification_flags_replica_mode_set_on_the_role_or_the_database(
    app_role_dsn: str, scope: str
) -> None:
    """The setting itself, not only the right to change it, switches the guard triggers off.
    It is reported even without the least-privilege check."""
    assert DSN is not None
    app = PostgresAssessmentRepository(app_role_dsn)
    name = psycopg.conninfo.conninfo_to_dict(DSN)["dbname"]
    target = "ROLE divesafe_app_test" if scope == "role" else f'DATABASE "{name}"'
    _sql(f"ALTER {target} SET session_replication_role = replica")
    try:
        assert any("session_replication_role" in p for p in app.verify(least_privilege=False))
        assert any("session_replication_role" in p for p in app.verify(least_privilege=True))
    finally:
        _sql(f"ALTER {target} RESET session_replication_role")
    assert app.verify(least_privilege=True) == []


def test_verification_flags_an_application_role_that_can_create_schemas(app_role_dsn: str) -> None:
    assert DSN is not None
    app = PostgresAssessmentRepository(app_role_dsn)
    name = psycopg.conninfo.conninfo_to_dict(DSN)["dbname"]
    _sql(f'GRANT CREATE ON DATABASE "{name}" TO divesafe_app_test')
    try:
        assert any("create schemas in the database" in p for p in app.verify(least_privilege=True))
    finally:
        _sql(f'REVOKE CREATE ON DATABASE "{name}" FROM divesafe_app_test')
    assert app.verify(least_privilege=True) == []


def test_verification_flags_a_role_that_may_set_session_replication_role(
    app_role_dsn: str,
) -> None:
    """With replica mode the ordinary guard triggers do not fire (PostgreSQL 15 and later)."""
    app = PostgresAssessmentRepository(app_role_dsn)
    with psycopg.connect(DSN or "") as conn:
        version = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()
    if version is None or version[0] < 150000:
        pytest.skip("GRANT SET ON PARAMETER needs PostgreSQL 15")
    _sql("GRANT SET ON PARAMETER session_replication_role TO divesafe_app_test")
    try:
        assert any("session_replication_role" in p for p in app.verify(least_privilege=True))
    finally:
        _sql("REVOKE SET ON PARAMETER session_replication_role FROM divesafe_app_test")
    assert app.verify(least_privilege=True) == []


@pytest.mark.parametrize(
    ("table", "problem"),
    [
        ("assessments", "owns the table"),
        ("pending_runs", "owns the pending runs table"),
        ("run_failure_counts", "owns the failure counts table"),
    ],
)
def test_verification_counts_membership_in_the_owning_role_as_ownership(
    app_role_dsn: str, table: str, problem: str
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    _sql("DROP ROLE IF EXISTS divesafe_owner_group_test")
    _sql("CREATE ROLE divesafe_owner_group_test NOLOGIN")
    _sql(f"ALTER TABLE {table} OWNER TO divesafe_owner_group_test")
    _sql("GRANT divesafe_owner_group_test TO divesafe_app_test")
    try:
        assert any(problem in p for p in app.verify(least_privilege=True))
    finally:
        _sql(f"ALTER TABLE {table} OWNER TO CURRENT_USER")
        _sql("DROP ROLE divesafe_owner_group_test")
    assert app.verify(least_privilege=True) == []


def test_verification_flags_a_guard_function_the_application_role_owns(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    _sql("ALTER FUNCTION public.assessments_no_truncate() OWNER TO divesafe_app_test")
    try:
        assert any("owns a guard function" in p for p in app.verify(least_privilege=True))
    finally:
        _sql("ALTER FUNCTION public.assessments_no_truncate() OWNER TO CURRENT_USER")
    assert app.verify(least_privilege=True) == []


def test_verification_of_a_database_missing_the_run_tables_is_a_problem_not_an_outage(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    assert app.verify(least_privilege=True) == []
    _sql("ALTER TABLE pending_runs RENAME TO pending_runs_hidden")
    _sql("ALTER TABLE run_failure_counts RENAME TO run_failure_counts_hidden")
    try:
        # A missing table must be reported, never hidden behind "storage unavailable", and its
        # absence must not read as a privilege the role lacks.
        problems = app.verify(least_privilege=True)
    finally:
        _sql("ALTER TABLE pending_runs_hidden RENAME TO pending_runs")
        _sql("ALTER TABLE run_failure_counts_hidden RENAME TO run_failure_counts")
    assert sorted(problems) == [
        "the pending_runs table does not exist (run db_init as the owner role)",
        "the run_failure_counts table does not exist (run db_init as the owner role)",
    ]
    assert app.verify(least_privilege=True) == []


def test_verification_flags_a_guard_trigger_that_calls_another_function() -> None:
    repo = _postgres()
    _sql(
        "CREATE OR REPLACE FUNCTION public.not_a_guard() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RETURN NEW; END; $$"
    )
    _sql("DROP TRIGGER assessments_insert_guard ON public.assessments")
    _sql(
        "CREATE TRIGGER assessments_insert_guard BEFORE INSERT ON public.assessments "
        "FOR EACH ROW EXECUTE FUNCTION public.not_a_guard()"
    )
    _sql("ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER assessments_insert_guard")
    try:
        problems = repo.verify(least_privilege=False)
        assert any("does not call assessments_insert_guard" in p for p in problems)
    finally:
        _sql("DROP TRIGGER assessments_insert_guard ON public.assessments")
        _sql(
            "CREATE TRIGGER assessments_insert_guard BEFORE INSERT ON public.assessments "
            "FOR EACH ROW EXECUTE FUNCTION public.assessments_insert_guard()"
        )
        _sql("ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER assessments_insert_guard")
        _sql("DROP FUNCTION public.not_a_guard()")
    assert repo.verify(least_privilege=False) == []


def test_the_run_table_refuses_a_failed_row_without_a_code_and_unknown_codes() -> None:
    _postgres()
    for state, failed_at, code in (
        ("FAILED", "now()", "NULL"),
        ("RUNNING", "NULL", "'timeout'"),
        ("FAILED", "now()", "'boom'"),
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            _sql(
                "INSERT INTO pending_runs (id, created_by, assigned_decider, plan, state, "
                f"token_hash, lease_expires_at, failed_at, failure_code) VALUES ('x', 'a', 'b', "
                f"'{{}}', '{state}', repeat('a', 64), now(), {failed_at}, {code})"
            )


def test_a_complete_and_a_late_run_are_decided_by_the_database_clock() -> None:
    from datetime import timedelta

    from tests.unit.test_run_store_contract import _completed, _run

    repo = _postgres()
    assert isinstance(repo, PostgresAssessmentRepository)
    token = repo.admit(
        _run(), max_in_flight=4, max_per_actor=4, lease=timedelta(minutes=5)
    ).run_token
    assert token is not None
    _sql("UPDATE pending_runs SET lease_expires_at = now() - interval '1 second'")
    assert repo.complete(_completed(), token) is False
    assert repo.get("r1") is None


# --- ENABLE ALWAYS and the owner check (migration 3) -----------------------------------------


@pytest.mark.parametrize("trigger", GUARD_TRIGGERS)
def test_the_migrated_guard_triggers_are_enable_always(trigger: str) -> None:
    _postgres()
    with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
        state = conn.execute(
            "SELECT tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'public.assessments'::regclass AND tgname = %s",
            (trigger,),
        ).fetchone()
    assert state == ("A",)


@pytest.mark.parametrize("trigger", GUARD_TRIGGERS)
def test_verification_flags_a_guard_trigger_that_is_only_enabled_not_always(trigger: str) -> None:
    repo = _postgres()
    assert isinstance(repo, PostgresAssessmentRepository)
    _sql(f"ALTER TABLE assessments ENABLE TRIGGER {trigger}")
    try:
        problems = repo.verify(least_privilege=False)
        assert any(trigger in p and "ENABLE ALWAYS" in p for p in problems)
    finally:
        _sql(f"ALTER TABLE assessments ENABLE ALWAYS TRIGGER {trigger}")
    assert repo.verify(least_privilege=False) == []


@pytest.mark.parametrize("trigger", GUARD_TRIGGERS)
def test_verification_flags_a_guard_trigger_that_fires_only_in_replica_mode(trigger: str) -> None:
    repo = _postgres()
    assert isinstance(repo, PostgresAssessmentRepository)
    _sql(f"ALTER TABLE assessments ENABLE REPLICA TRIGGER {trigger}")
    try:
        assert any(trigger in p for p in repo.verify(least_privilege=False))
    finally:
        _sql(f"ALTER TABLE assessments ENABLE ALWAYS TRIGGER {trigger}")
    assert repo.verify(least_privilege=False) == []


def test_the_guard_still_fires_when_the_session_is_in_replica_mode(
    stored: PostgresAssessmentRepository,
) -> None:
    """The point of migration 3: a session that sets session_replication_role = replica (a
    superuser, or a role with the right to) no longer switches the guard off."""
    assert DSN is not None
    for statement in (
        "DELETE FROM assessments WHERE id = 'r1'",
        "UPDATE assessments SET record = jsonb_set(record, '{final_recommendation}', '\"GO\"')",
        "TRUNCATE assessments",
    ):
        with psycopg.connect(DSN) as conn:
            conn.execute("SET session_replication_role = replica")
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute(statement)  # type: ignore[call-overload]
    assert stored.get("r1") == _record()


def test_an_insert_that_breaks_the_insert_guard_is_refused_in_replica_mode() -> None:
    _postgres()
    assert DSN is not None
    decided = json.loads(_decided(_record(), "alice").model_dump_json())
    with psycopg.connect(DSN) as conn:
        conn.execute("SET session_replication_role = replica")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "INSERT INTO assessments (id, record) VALUES ('r1', %s::jsonb)",
                (json.dumps(decided),),
            )


def test_migration_three_upgrades_a_database_that_has_the_plain_triggers() -> None:
    """A database at version 2 (triggers 'O') is brought to 'A' by db_init, and not before."""
    from tests.unit.test_repository_contract import drop_knowledge_objects

    from divesafe.services.migrations import MIGRATIONS, migrate

    _postgres()
    try:
        # Back to version 2: migration 4 sits above 3, so it goes too, with its objects.
        _sql("DELETE FROM schema_migrations WHERE version >= 3")
        drop_knowledge_objects()
        for trigger in GUARD_TRIGGERS:
            _sql(f"ALTER TABLE assessments ENABLE TRIGGER {trigger}")
        repo = PostgresAssessmentRepository(DSN)  # type: ignore[arg-type]
        problems = repo.verify(least_privilege=False)
        assert any("schema version 2" in p for p in problems)
        assert all(any(t in p for p in problems) for t in GUARD_TRIGGERS)
        assert migrate(DSN) == [3, 4]  # type: ignore[arg-type]
        assert repo.verify(least_privilege=False) == []
    finally:
        _postgres()
    assert MIGRATIONS[2].version == 3


def test_a_migration_that_leaves_a_guard_trigger_not_always_is_rolled_back() -> None:
    import divesafe.services.migrations as mod
    from divesafe.services.migrations import MIGRATIONS, Migration, MigrationError, migrate

    _postgres()
    sneaky = Migration(
        len(MIGRATIONS) + 1,
        "sneaky",
        "ALTER TABLE public.assessments ENABLE TRIGGER assessments_insert_guard",
    )
    original = mod.guarded
    mod.guarded = lambda known: None  # type: ignore[assignment]
    try:
        with pytest.raises(MigrationError, match="not intact"):
            migrate(DSN, (*MIGRATIONS, sneaky))  # type: ignore[arg-type]
    finally:
        mod.guarded = original
    assert PostgresAssessmentRepository(DSN).verify(least_privilege=False) == []  # type: ignore[arg-type]


def test_the_owner_superuser_check_passes_for_an_ordinary_owner_and_flags_a_superuser(
    app_role_dsn: str,
) -> None:
    assert DSN is not None
    app = PostgresAssessmentRepository(app_role_dsn)
    # The knowledge guard (ADR 0022 condition 9) judges the kb tables and the public schema against
    # the owner of public.assessments, so a new assessments owner must own them too, as one owner
    # does in a real deployment. (Changing only assessments made it report 4 owner problems.)
    kb_tables = ("kb_documents", "kb_chunks", "kb_status_events")
    original_public_owner = _sql_value(
        "SELECT pg_catalog.pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace "
        "WHERE nspname = 'public'"
    )
    _sql("DROP ROLE IF EXISTS divesafe_plain_owner_test")
    _sql("CREATE ROLE divesafe_plain_owner_test NOLOGIN")
    _sql("ALTER TABLE assessments OWNER TO divesafe_plain_owner_test")
    for table in kb_tables:
        _sql(f"ALTER TABLE {table} OWNER TO divesafe_plain_owner_test")
    _sql("ALTER SCHEMA public OWNER TO divesafe_plain_owner_test")
    try:
        assert app.verify(least_privilege=True, owner_not_superuser=True) == []
        _sql("ALTER ROLE divesafe_plain_owner_test SUPERUSER")
        problems = app.verify(least_privilege=True, owner_not_superuser=True)
        assert any("owns the assessments table is a superuser" in p for p in problems)
        assert not any("superuser" in p for p in app.verify(least_privilege=True))
        _sql("ALTER ROLE divesafe_plain_owner_test NOSUPERUSER")
    finally:
        _sql("ALTER ROLE divesafe_plain_owner_test NOSUPERUSER")
        _sql(f'ALTER SCHEMA public OWNER TO "{original_public_owner}"')
        for table in kb_tables:
            _sql(f"ALTER TABLE {table} OWNER TO CURRENT_USER")
        _sql("ALTER TABLE assessments OWNER TO CURRENT_USER")
        _sql("DROP ROLE divesafe_plain_owner_test")


def test_the_owner_superuser_check_counts_membership_in_a_superuser_role(
    app_role_dsn: str,
) -> None:
    app = PostgresAssessmentRepository(app_role_dsn)
    _sql("DROP ROLE IF EXISTS divesafe_super_group_test")
    _sql("DROP ROLE IF EXISTS divesafe_member_owner_test")
    _sql("CREATE ROLE divesafe_super_group_test NOLOGIN SUPERUSER")
    _sql("CREATE ROLE divesafe_member_owner_test NOLOGIN")
    _sql("GRANT divesafe_super_group_test TO divesafe_member_owner_test")
    _sql("ALTER TABLE assessments OWNER TO divesafe_member_owner_test")
    try:
        problems = app.verify(least_privilege=False, owner_not_superuser=True)
        assert any("superuser or a member of one" in p for p in problems)
    finally:
        _sql("ALTER TABLE assessments OWNER TO CURRENT_USER")
        _sql("DROP ROLE divesafe_member_owner_test")
        _sql("DROP ROLE divesafe_super_group_test")


def test_the_owner_superuser_check_is_off_by_default_and_refuses_a_superuser_owner() -> None:
    repo = _postgres()
    assert isinstance(repo, PostgresAssessmentRepository)
    assert repo.verify(least_privilege=False, owner_not_superuser=False) == []
    assert any(
        "superuser" in p for p in repo.verify(least_privilege=False, owner_not_superuser=True)
    ), "the test database owner is a superuser, so the production check must refuse it"


def test_a_search_path_setting_cannot_let_a_look_alike_view_hide_a_superuser_owner(
    app_role_dsn: str,
) -> None:
    """verify() pins its own search path, so a view in `public` named like a catalog cannot
    stand in for it. The test database owner is a superuser, which the check must report."""
    app = PostgresAssessmentRepository(app_role_dsn)
    _sql("CREATE VIEW public.pg_roles AS SELECT * FROM pg_catalog.pg_roles WHERE false")
    _sql("ALTER ROLE divesafe_app_test SET search_path = public, pg_catalog")
    try:
        problems = app.verify(least_privilege=True, owner_not_superuser=True)
        assert any("owns the assessments table is a superuser" in p for p in problems)
    finally:
        _sql("ALTER ROLE divesafe_app_test RESET search_path")
        _sql("DROP VIEW public.pg_roles")
