"""The database enforces append-only on its own: raw SQL, bypassing the application, cannot
rewrite or delete a stored assessment. Needs DIVESAFE_TEST_DATABASE_URL."""

from __future__ import annotations

import json
import os

import psycopg
import pytest
from tests.unit.test_repository_contract import _decided, _postgres, _record

from divesafe.services import PostgresAssessmentRepository

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
    from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build

    from divesafe.api.app import create_app

    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.repository = stored
    api = TestClient(create_app(state))
    created = api.post("/v1/assessments", json=PLAN, headers=AUTH_A).json()
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
    from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build

    from divesafe.api.app import create_app
    from divesafe.services import InMemoryAssessmentRepository

    class Probe(InMemoryAssessmentRepository):
        on_event_loop: bool | None = None

        def create(self, record: object) -> None:
            try:
                asyncio.get_running_loop()
                Probe.on_event_loop = True
            except RuntimeError:
                Probe.on_event_loop = False
            super().create(record)  # type: ignore[arg-type]

    client, _, _ = _build()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.repository = Probe()
    TestClient(create_app(state)).post("/v1/assessments", json=PLAN, headers=AUTH_A)
    assert Probe.on_event_loop is False


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
    _sql("ALTER TABLE assessments ENABLE TRIGGER assessments_append_only_guard")
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
        _sql("ALTER TABLE assessments ENABLE TRIGGER assessments_no_truncate_guard")
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
    bad = Migration(2, "bad", "CREATE TABLE half_done (x int); SELECT 1/0;")
    with pytest.raises(Exception, match="could not apply"):
        migrate(DSN, (*MIGRATIONS, bad))  # type: ignore[arg-type]
    with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
        assert conn.execute("SELECT to_regclass('public.half_done')").fetchone() == (None,)
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (1,)


def test_a_good_second_migration_is_applied_once_and_recorded() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, migrate

    _postgres()
    extra = Migration(2, "scratch", "CREATE TABLE migration_scratch (x int)")
    try:
        assert migrate(DSN, (*MIGRATIONS, extra)) == [2]  # type: ignore[arg-type]
        assert migrate(DSN, (*MIGRATIONS, extra)) == []  # type: ignore[arg-type]
    finally:
        _sql("DROP TABLE IF EXISTS migration_scratch")
        _sql("DELETE FROM schema_migrations WHERE version = 2")


def test_an_edited_applied_migration_is_refused_by_the_runner() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, MigrationError, migrate

    _postgres()
    edited = Migration(1, MIGRATIONS[0].name, MIGRATIONS[0].sql + "\n-- edited")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(DSN, (edited,))  # type: ignore[arg-type]


def test_startup_refuses_a_database_behind_or_ahead_of_the_code() -> None:
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
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (2, 'future', %s)",
            ("1" * 64,),
        )
        assert any("ahead of this code" in p for p in repo.verify(least_privilege=False))
    finally:
        _sql("DROP TABLE IF EXISTS schema_migrations")
        _postgres()


def test_the_application_role_can_read_but_not_write_the_migration_history(
    app_role_dsn: str,
) -> None:
    with psycopg.connect(app_role_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (1,)
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
    slow = Migration(2, "slow", "SELECT pg_sleep(1); CREATE TABLE migration_scratch (x int)")
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(migrate, DSN, (*MIGRATIONS, slow)) for _ in range(2)]  # type: ignore[arg-type]
            results = sorted(f.result() for f in futures)
        assert results == [[], [2]]
    finally:
        _sql("DROP TABLE IF EXISTS migration_scratch")
        _sql("DELETE FROM schema_migrations WHERE version = 2")


def test_a_migration_that_leaves_the_guard_disabled_is_rolled_back() -> None:
    from divesafe.services.migrations import MIGRATIONS, Migration, MigrationError, migrate

    _postgres()
    sneaky = Migration(
        2, "sneaky", "ALTER TABLE public.assessments DISABLE TRIGGER assessments_insert_guard"
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
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (1,)
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
    evil = Migration(2, "evil", "DROP TRIGGER assessments_append_only_guard ON public.assessments")
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
