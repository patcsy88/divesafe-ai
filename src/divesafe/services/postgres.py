"""PostgreSQL adapter for the assessment repository (ADR 0012).

The record is one JSONB document. The application enforces the append-only rules
(`check_successor`, compare-and-set under a row lock); the database enforces them again with
triggers (no delete, no truncate, only a first valid decision and first actual conditions may be
appended, a new row cannot arrive already decided), so a bug or a SQL session through the
application role cannot rewrite history.

Schema and triggers are installed by a separate owner-role step (`db_init`). At run time the
application role only verifies that the guard is present and enabled, and refuses to start if not.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from divesafe.config.roles import RESERVED_ROLE_NAMES
from divesafe.domain import AssessmentRecord, DivePlan
from divesafe.services.migrations import (
    GUARD_FUNCTIONS,
    GUARD_TRIGGERS,
    KNOWLEDGE_TABLES,
    knowledge_problems,
    migrate,
    schema_problems,
)
from divesafe.services.repository import (
    ConflictError,
    InvalidSuccessorError,
    RecordExistsError,
    RecordNotFoundError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    UnstorableRecordError,
    check_successor,
)
from divesafe.services.runs import (
    Admission,
    CapacityError,
    CleanupResult,
    FailureCode,
    NewRun,
    PendingRun,
    RunMismatchError,
    RunState,
)

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 3000
_OPTIONS = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}"

_RUNS_LOCK_KEY = 7_281_903_115
_RUN_COLUMNS = (
    "id, created_by, assigned_decider, plan::text, created_at, state, lease_expires_at, "
    "failed_at, failure_code"
)

_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


LOADER_ROLE_MISSING = "the knowledge loader role does not exist; create it before running db_init"
# Columns the loader may insert into kb_status_events: never the ordering key or recorded_at.
_LOADER_EVENT_COLUMNS = ("document_id", "version", "status", "hash_checked")


class LoaderRoleError(RuntimeError):
    """The knowledge loader role is missing or its name is not acceptable. Fixed messages."""


class TempPrivilegeError(LoaderRoleError):
    """TEMPORARY on the database is still held by PUBLIC or the loader after the revokes."""


TEMP_STILL_HELD = "TEMPORARY on the database is still held by PUBLIC or the loader role"
LOADER_ROLE_INVALID = "invalid loader role name"


def check_role_name(role: str, *, what: str) -> None:
    if not _ROLE_NAME.fullmatch(role) or role == "public" or role.startswith("pg_"):
        raise ValueError(f"invalid {what} role name")


def check_loader_role_name(role: str, app_role: str | None = None) -> None:
    """Everything `install_schema` refuses for a loader role name, without echoing the value."""
    check_role_name(role, what="loader")
    if role == app_role or role in RESERVED_ROLE_NAMES:
        raise ValueError(LOADER_ROLE_INVALID)


def _kb_grants(
    conn: psycopg.Connection[Any], app_role: str | None, loader_role: str | None
) -> None:
    tables = sql.SQL("public.kb_documents, public.kb_chunks, public.kb_status_events")
    conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(tables))
    conn.execute(
        "REVOKE EXECUTE ON FUNCTION public.kb_no_update_delete(), public.kb_no_truncate(), "
        "public.kb_documents_insert(), public.kb_chunks_insert(), "
        "public.kb_status_events_insert() FROM PUBLIC"
    )
    conn.execute("REVOKE ALL ON SEQUENCE public.kb_status_event_order_seq FROM PUBLIC")
    row = conn.execute("SELECT current_database()").fetchone()
    assert row is not None
    conn.execute(
        sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(row[0]))
    )
    for role_name in (app_role, loader_role):
        if role_name is None:
            continue
        role = sql.Identifier(role_name)
        conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(tables, role))
        conn.execute(
            sql.SQL("REVOKE ALL ON SEQUENCE public.kb_status_event_order_seq FROM {}").format(role)
        )
        conn.execute(
            sql.SQL(
                "REVOKE EXECUTE ON FUNCTION public.kb_no_update_delete(), "
                "public.kb_no_truncate(), public.kb_documents_insert(), "
                "public.kb_chunks_insert(), public.kb_status_events_insert() FROM {}"
            ).format(role)
        )
        conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(tables, role))
    if loader_role is not None:
        role = sql.Identifier(loader_role)
        conn.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(sql.Identifier(row[0]), role)
        )
        conn.execute(sql.SQL("GRANT INSERT ON public.kb_documents TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT ON public.kb_chunks TO {}").format(role))
        columns = sql.SQL(", ").join(sql.Identifier(c) for c in _LOADER_EVENT_COLUMNS)
        conn.execute(
            sql.SQL("GRANT INSERT ({}) ON public.kb_status_events TO {}").format(columns, role)
        )
    # REVOKE only warns when it changes nothing, so read the result back.
    public_temp = conn.execute(
        "SELECT has_database_privilege('public', current_database(), 'TEMPORARY')"
    ).fetchone()
    held = public_temp is None or public_temp[0] is not False
    if loader_role is not None:
        loader_temp = conn.execute(
            "SELECT has_database_privilege(%s, current_database(), 'TEMPORARY')", (loader_role,)
        ).fetchone()
        held = held or loader_temp is None or loader_temp[0] is not False
        # NOINHERIT / SET ROLE: any role the loader is a member of that holds TEMPORARY counts.
        member_temp = conn.execute(
            "SELECT r.rolname FROM pg_roles r "
            "WHERE has_database_privilege(r.oid, current_database(), 'TEMPORARY') IS NOT FALSE "
            "AND COALESCE(pg_has_role(%s, r.oid, 'MEMBER'), true) "
            "AND r.rolname <> %s LIMIT 1",
            (loader_role, loader_role),
        ).fetchone()
        held = held or member_temp is not None
    if held:
        raise TempPrivilegeError(TEMP_STILL_HELD)


def install_schema(
    admin_dsn: str, app_role: str | None = None, *, loader_role: str | None = None
) -> None:
    """Create the table and guard as the OWNER role, and optionally grant a least-privilege
    application role and the knowledge loader role (which must already exist; it is never created
    here). Idempotent. Never run with the application's own credentials."""
    if app_role is not None:
        check_role_name(app_role, what="application")
    if loader_role is not None:
        check_loader_role_name(loader_role, app_role)
    migrate(admin_dsn, app_role=app_role)
    try:
        with psycopg.connect(admin_dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            if loader_role is not None:
                owner = conn.execute("SELECT current_user").fetchone()
                found = conn.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s", (loader_role,)
                ).fetchone()
                if owner is not None and owner[0] == loader_role:
                    raise LoaderRoleError(LOADER_ROLE_INVALID)
                if found is None:
                    raise LoaderRoleError(LOADER_ROLE_MISSING)
            conn.execute("REVOKE ALL ON public.assessments FROM PUBLIC")
            conn.execute("REVOKE ALL ON public.pending_runs FROM PUBLIC")
            conn.execute("REVOKE ALL ON public.run_failure_counts FROM PUBLIC")
            if app_role is not None:
                role = sql.Identifier(app_role)
                conn.execute(sql.SQL("GRANT SELECT ON public.schema_migrations TO {}").format(role))
                conn.execute(
                    sql.SQL(
                        "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES "
                        "ON public.schema_migrations FROM {}"
                    ).format(role)
                )
                conn.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE ON public.assessments TO {}").format(role)
                )
                conn.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON public.pending_runs TO {}"
                    ).format(role)
                )
                conn.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE ON public.run_failure_counts TO {}"
                    ).format(role)
                )
                conn.execute(
                    sql.SQL(
                        "REVOKE TRUNCATE, TRIGGER, REFERENCES ON public.pending_runs FROM {}"
                    ).format(role)
                )
                conn.execute(
                    sql.SQL(
                        "REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES "
                        "ON public.run_failure_counts FROM {}"
                    ).format(role)
                )
                conn.execute(
                    sql.SQL(
                        "REVOKE DELETE, TRUNCATE, TRIGGER, REFERENCES ON public.assessments FROM {}"
                    ).format(role)
                )
            _kb_grants(conn, app_role, loader_role)
    except psycopg.Error:
        raise RepositoryUnavailableError("could not install the schema") from None


def _storable(value: Any) -> None:
    """Refuse what a JSONB round trip would alter or reject."""
    if isinstance(value, float) and value != value or value in (float("inf"), float("-inf")):
        raise UnstorableRecordError("non-finite numbers cannot be stored")
    if isinstance(value, str) and "\x00" in value:
        raise UnstorableRecordError("NUL characters cannot be stored")
    if isinstance(value, dict):
        for key, item in value.items():
            _storable(key)
            _storable(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _storable(item)


def _same(a: Any, b: Any) -> bool:
    """Equality that also distinguishes 1 from 1.0 and true from 1."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _without_new_defaults(new: AssessmentRecord, stored: Any) -> dict[str, Any]:
    """The successor's JSON, minus top-level keys the stored row does not have whose value is the
    model default. A row written before a field existed lacks its key, and the append-only
    trigger refuses any other change to the record, so adding the default key would refuse every
    update. Keys present in the stored row are always kept. Only TOP-LEVEL keys are handled:
    defaults nested inside other values are not (a nested field added later needs its own plan).
    The omitted key still loads as the same default, which the read-back checks."""
    data: dict[str, Any] = json.loads(new.model_dump_json())
    if not isinstance(stored, dict):
        return data
    for name, field in AssessmentRecord.model_fields.items():
        if name in stored or name not in data or field.is_required():
            continue
        default = to_jsonable_python(field.get_default(call_default_factory=True))
        if _same(default, data[name]):
            del data[name]
    return data


@contextmanager
def _storage_errors() -> Iterator[None]:
    """Translate driver and validation failures into typed errors that carry no connection
    details and no record content."""
    try:
        yield
    except (
        RecordExistsError,
        RecordNotFoundError,
        ConflictError,
        InvalidSuccessorError,
        UnstorableRecordError,
        RepositoryIntegrityError,
        RepositoryUnavailableError,
    ):
        raise
    except psycopg.errors.RaiseException:
        raise InvalidSuccessorError("rejected by the database guard") from None
    except psycopg.errors.DataError:
        raise UnstorableRecordError("the record cannot be stored") from None
    except psycopg.Error:
        raise RepositoryUnavailableError("storage unavailable") from None
    except ValidationError:
        raise RepositoryIntegrityError("a stored record failed validation") from None


# has_parameter_privilege() exists from PostgreSQL 15.
_PARAMETER_PRIVILEGES_FROM = 150000


class PostgresAssessmentRepository:
    """One short-lived connection per operation, with connect, statement and lock timeouts."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, options=_OPTIONS)

    def verify(self, *, least_privilege: bool, owner_not_superuser: bool = False) -> list[str]:
        """Problems that make this store unsafe to use. Empty means the guard is in place.
        With `owner_not_superuser`, also report a table owner that is a superuser (a policy
        choice: it refuses any deployment whose owner role is one). With `least_privilege`, also
        report an application role that owns the table, the public
        schema or the database, is (or can become) a superuser, can create objects in the public
        schema or schemas in the database (a schema owner can drop the table without firing any
        trigger), may set the replica mode, can create roles, or can delete, truncate or create
        triggers."""
        problems: list[str] = []
        with _storage_errors(), self._connect() as conn:
            # A role or database setting of search_path must not let an object in another schema
            # stand in for a built-in function or catalog; every table below is named in `public`.
            conn.execute("SET search_path = pg_catalog, pg_temp")
            if conn.execute("SELECT to_regclass('public.assessments')").fetchone() == (None,):
                return ["the assessments table does not exist (run db_init as the owner role)"]
            try:
                with conn.transaction():
                    problems += schema_problems(conn)
            except psycopg.errors.InsufficientPrivilege:
                problems.append("the application role cannot read the migration history")
            for table in ("pending_runs", "run_failure_counts"):
                if conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone() == (
                    None,
                ):
                    problems.append(
                        f"the {table} table does not exist (run db_init as the owner role)"
                    )
            problems += self._knowledge_problems(conn)
            # Each guard trigger must be enabled and call its own function in `public`.
            calls = {
                str(name): str(function)
                for name, function in conn.execute(
                    "SELECT t.tgname, p.proname FROM pg_trigger t "
                    "JOIN pg_proc p ON p.oid = t.tgfoid "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE t.tgrelid = 'public.assessments'::regclass AND t.tgenabled = 'A' "
                    "AND n.nspname = 'public'"
                )
            }
            problems += [
                f"guard trigger '{t}' is missing, disabled or not ENABLE ALWAYS"
                for t in GUARD_TRIGGERS
                if t not in calls
            ]
            problems += [
                f"guard trigger '{t}' does not call {GUARD_FUNCTIONS[t]}"
                for t in GUARD_TRIGGERS
                if t in calls and calls[t] != GUARD_FUNCTIONS[t]
            ]
            # In replica mode ordinary triggers and constraints do not fire. The guard triggers
            # are ENABLE ALWAYS (migration 3) and fire anyway, so this is defence in depth, and it
            # stays refused. A role, database or server setting can put every session there; a
            # privilege check does not see that.
            mode = conn.execute("SELECT current_setting('session_replication_role')").fetchone()
            if mode is not None and mode[0] == "replica":
                problems.append(
                    "session_replication_role is 'replica': ordinary triggers and constraints "
                    "do not fire in that mode"
                )
            if owner_not_superuser:
                owner = conn.execute(
                    "SELECT COALESCE((SELECT EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolsuper "
                    "AND pg_has_role(c.relowner, r.oid, 'MEMBER')) "
                    "FROM pg_class c WHERE c.oid = to_regclass('public.assessments')), true)"
                ).fetchone()
                if owner is None or owner[0] is not False:
                    problems.append(
                        "the role that owns the assessments table is a superuser or a member of "
                        "one, or its owner could not be read"
                    )
            if least_privilege:
                problems += self._privilege_problems(conn)
        return problems

    @staticmethod
    def _knowledge_problems(conn: psycopg.Connection[Any]) -> list[str]:
        """ADR 0022 condition 9. Read-only through SET LOCAL transaction_read_only, so it also
        works inside a savepoint. A missing knowledge table is one problem (the schema check also
        reports the pending migration); the detailed checks need all three."""
        missing = [
            f"the {table} table does not exist (run db_init as the owner role)"
            for table in KNOWLEDGE_TABLES
            if conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone() == (None,)
        ]
        if missing:
            return missing
        try:
            with conn.transaction():
                return knowledge_problems(conn, owner_is_current_user=False, read_only=True)
        except psycopg.errors.InsufficientPrivilege:
            return ["the application role cannot read the knowledge table definitions"]

    @staticmethod
    def _privilege_problems(conn: psycopg.Connection[Any]) -> list[str]:
        """Ownership counts through role membership too: a member of the owning role has the
        owner's powers. Tables are passed as oids, so a missing table reads as false, not an
        error."""

        def owns(table: str) -> str:
            return (
                "COALESCE((SELECT pg_has_role(current_user, relowner, 'MEMBER') FROM pg_class "
                f"WHERE oid = to_regclass('{table}')), false)"
            )

        def can(table: str, privileges: str) -> str:
            return f"COALESCE(has_table_privilege(to_regclass('{table}'), '{privileges}'), false)"

        functions = ", ".join(f"'{f}'" for f in sorted(set(GUARD_FUNCTIONS.values())))
        checks = (
            (owns("public.assessments"), "owns the table"),
            (
                "COALESCE((SELECT pg_has_role(current_user, nspowner, 'MEMBER') FROM pg_namespace "
                "WHERE nspname = 'public'), false)",
                "owns the public schema",
            ),
            (
                "COALESCE((SELECT pg_has_role(current_user, datdba, 'MEMBER') FROM pg_database "
                "WHERE datname = current_database()), false)",
                "owns the database",
            ),
            (
                "COALESCE(has_schema_privilege('public', 'CREATE'), false)",
                "can create objects in the public schema",
            ),
            (
                "COALESCE(has_database_privilege(current_database(), 'CREATE'), false)",
                "can create schemas in the database",
            ),
            (
                "EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolsuper "
                "AND pg_has_role(current_user, r.oid, 'MEMBER'))",
                "is a superuser or a member of one (it could SET ROLE to it)",
            ),
            (
                "EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname IN "
                "('pg_execute_server_program', 'pg_write_server_files', 'pg_read_server_files') "
                "AND pg_has_role(current_user, r.oid, 'MEMBER'))",
                "is a member of a role that can run programs or read or write files on the server",
            ),
            (
                "EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolcreaterole "
                "AND pg_has_role(current_user, r.oid, 'MEMBER'))",
                "can create roles, or is a member of a role that can (it could grant others)",
            ),
            (can("public.assessments", "DELETE"), "can DELETE"),
            (can("public.assessments", "TRUNCATE"), "can TRUNCATE"),
            (can("public.assessments", "TRIGGER"), "can create triggers"),
            (
                can("public.schema_migrations", "INSERT, UPDATE, DELETE, TRUNCATE"),
                "can write the migration history",
            ),
            (owns("public.pending_runs"), "owns the pending runs table"),
            (
                can("public.pending_runs", "TRUNCATE, TRIGGER"),
                "can TRUNCATE or create triggers on the pending runs table",
            ),
            (can("public.run_failure_counts", "DELETE, TRUNCATE"), "can delete the failure counts"),
            (owns("public.run_failure_counts"), "owns the failure counts table"),
            (
                "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                f"WHERE n.nspname = 'public' AND p.proname IN ({functions}) "
                "AND pg_has_role(current_user, p.proowner, 'MEMBER'))",
                "owns a guard function",
            ),
        )
        all_checks = list(checks)
        version = conn.execute("SELECT current_setting('server_version_num')::int").fetchone()
        if version is not None and version[0] >= _PARAMETER_PRIVILEGES_FROM:
            # With this right the role could set session_replication_role = replica, which skips
            # ordinary triggers and constraints (the guard triggers are ENABLE ALWAYS, so they
            # still fire; the right stays refused as defence in depth).
            all_checks.append(
                (
                    "COALESCE(has_parameter_privilege('session_replication_role', 'SET'), false)",
                    "may set session_replication_role (replica mode skips ordinary triggers)",
                )
            )
        row = conn.execute("SELECT " + ", ".join(sql for sql, _ in all_checks)).fetchone()
        assert row is not None
        return [
            f"the application role {name}"
            for (_, name), flag in zip(all_checks, row, strict=True)
            if flag
        ]

    def create(self, record: AssessmentRecord) -> None:
        if record.human_decision is not None or record.actual_conditions is not None:
            raise InvalidSuccessorError("a new record cannot already carry a decision")
        _storable(record.model_dump())
        with _storage_errors():
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO public.assessments (id, record) VALUES (%s, %s::jsonb)",
                        (record.id, record.model_dump_json()),
                    )
                    self._read_back_must_match(conn, record)
            except UniqueViolation:
                raise RecordExistsError(record.id) from None

    def get(self, assessment_id: str) -> AssessmentRecord | None:
        with _storage_errors(), self._connect() as conn:
            row = conn.execute(
                "SELECT record::text FROM public.assessments WHERE id = %s", (assessment_id,)
            ).fetchone()
            return None if row is None else AssessmentRecord.model_validate_json(row[0])

    def replace(self, expected: AssessmentRecord, new: AssessmentRecord) -> None:
        if expected.id != new.id:
            raise InvalidSuccessorError("id may not change")
        _storable(new.model_dump())
        with _storage_errors(), self._connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT record::text FROM public.assessments WHERE id = %s FOR UPDATE",
                (expected.id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(expected.id)
            if AssessmentRecord.model_validate_json(row[0]) != expected:
                raise ConflictError(expected.id)
            check_successor(expected, new)
            intended = _without_new_defaults(new, json.loads(row[0]))
            cursor = conn.execute(
                "UPDATE public.assessments SET record = %s::jsonb WHERE id = %s",
                (json.dumps(intended), new.id),
            )
            if cursor.rowcount != 1:
                raise RepositoryIntegrityError("the update did not apply")
            self._read_back_must_match(conn, new, intended)

    @staticmethod
    def _read_back_must_match(
        conn: psycopg.Connection[Any],
        record: AssessmentRecord,
        intended: dict[str, Any] | None = None,
    ) -> None:
        """`intended` is the exact JSON object that was written (default: the full dump). The
        stored text must equal it exactly (types included), and must also load as a record equal
        to `record`, so an omitted default key is tolerated only because it loads as the default."""
        row = conn.execute(
            "SELECT record::text FROM public.assessments WHERE id = %s", (record.id,)
        ).fetchone()
        if row is None:
            raise RepositoryIntegrityError("the record did not persist")
        wanted = json.loads(record.model_dump_json()) if intended is None else intended
        if not _same(wanted, json.loads(row[0])):
            raise UnstorableRecordError("the record would not read back identical")
        if AssessmentRecord.model_validate_json(row[0]) != record:
            raise UnstorableRecordError("the record would not read back identical")

    # --- pending runs (ADR 0019) ----------------------------------------------------------------
    # Leases and counts use the database clock, so several workers cannot disagree about time.

    @staticmethod
    def _run_from(row: tuple[Any, ...]) -> PendingRun:
        return PendingRun(
            id=row[0],
            created_by=row[1],
            assigned_decider=row[2],
            plan=DivePlan.model_validate_json(row[3]),
            created_at=row[4],
            state=RunState(row[5]),
            lease_expires_at=row[6],
            failed_at=row[7],
            failure_code=None if row[8] is None else FailureCode(row[8]),
        )

    @staticmethod
    def _expire(conn: psycopg.Connection[Any], run_id: str) -> None:
        conn.execute(
            "UPDATE public.pending_runs SET state = 'FAILED', failed_at = clock_timestamp(), "
            "failure_code = 'timeout' WHERE id = %s AND state = 'RUNNING' "
            "AND lease_expires_at <= clock_timestamp()",
            (run_id,),
        )

    def admit(
        self, run: NewRun, *, max_in_flight: int, max_per_actor: int, lease: timedelta
    ) -> Admission:
        _storable(run.plan.model_dump(mode="json"))
        token = secrets.token_urlsafe(32)
        with _storage_errors(), self._connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RUNS_LOCK_KEY,))
            self._expire(conn, run.id)
            # The run is read before the record: a completion that commits between the two reads
            # is then seen by the second, so a finished id never starts a second run.
            row = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM public.pending_runs WHERE id = %s", (run.id,)
            ).fetchone()
            if (
                row is None
                and conn.execute(
                    "SELECT 1 FROM public.assessments WHERE id = %s", (run.id,)
                ).fetchone()
            ):
                return Admission(kind="completed")
            if row is not None:
                existing = self._run_from(row)
                if (
                    existing.plan != run.plan
                    or existing.assigned_decider != run.assigned_decider
                    or existing.created_by != run.created_by
                ):
                    raise RunMismatchError(run.id)
                kind = "joined" if existing.state is RunState.RUNNING else "failed"
                return Admission(kind=kind, run=existing)
            counts = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE created_by = %s) "
                "FROM public.pending_runs "
                "WHERE state = 'RUNNING' AND lease_expires_at > clock_timestamp()",
                (run.created_by,),
            ).fetchone()
            assert counts is not None
            if counts[0] >= max_in_flight:
                raise CapacityError("server")
            if counts[1] >= max_per_actor:
                raise CapacityError("actor")
            conn.execute(
                "INSERT INTO public.pending_runs (id, created_by, assigned_decider, plan, "
                "created_at, state, token_hash, lease_expires_at) VALUES "
                "(%s, %s, %s, %s::jsonb, clock_timestamp(), 'RUNNING', %s, "
                "clock_timestamp() + %s)",
                (
                    run.id,
                    run.created_by,
                    run.assigned_decider,
                    run.plan.model_dump_json(),
                    hashlib.sha256(token.encode()).hexdigest(),
                    lease,
                ),
            )
            stored = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM public.pending_runs WHERE id = %s", (run.id,)
            ).fetchone()
            assert stored is not None
            return Admission(kind="started", run=self._run_from(stored), run_token=token)

    def get_run(self, run_id: str) -> PendingRun | None:
        with _storage_errors(), self._connect() as conn, conn.transaction():
            self._expire(conn, run_id)
            row = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM public.pending_runs WHERE id = %s", (run_id,)
            ).fetchone()
            return None if row is None else self._run_from(row)

    def complete(self, record: AssessmentRecord, run_token: str) -> bool:
        """Store the record and drop the run in one transaction, only if this run still holds a
        live lease and the token matches. Otherwise nothing is stored."""
        if record.human_decision is not None or record.actual_conditions is not None:
            raise InvalidSuccessorError("a new record cannot already carry a decision")
        _storable(record.model_dump())
        with _storage_errors(), self._connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT token_hash FROM public.pending_runs WHERE id = %s AND state = 'RUNNING' "
                "AND lease_expires_at > clock_timestamp() FOR UPDATE",
                (record.id,),
            ).fetchone()
            if row is None or not secrets.compare_digest(
                row[0], hashlib.sha256(run_token.encode()).hexdigest()
            ):
                return False
            try:
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO public.assessments (id, record) VALUES (%s, %s::jsonb)",
                        (record.id, record.model_dump_json()),
                    )
            except UniqueViolation:
                return False
            self._read_back_must_match(conn, record)
            conn.execute("DELETE FROM public.pending_runs WHERE id = %s", (record.id,))
            return True

    def fail(self, run_id: str, run_token: str, code: FailureCode) -> bool:
        with _storage_errors(), self._connect() as conn, conn.transaction():
            cursor = conn.execute(
                "UPDATE public.pending_runs SET state = 'FAILED', failed_at = clock_timestamp(), "
                "failure_code = %s WHERE id = %s AND state = 'RUNNING' AND token_hash = %s",
                (code.value, run_id, hashlib.sha256(run_token.encode()).hexdigest()),
            )
            return cursor.rowcount == 1

    def cleanup(self, *, retention: timedelta, batch: int) -> CleanupResult:
        """Mark expired running rows failed, then delete failed rows older than `retention` in a
        bounded batch, counting them by code first. Never touches `assessments`."""
        with _storage_errors(), self._connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RUNS_LOCK_KEY,))
            marked = conn.execute(
                "UPDATE public.pending_runs SET state = 'FAILED', failed_at = clock_timestamp(), "
                "failure_code = 'timeout' WHERE id IN (SELECT id FROM public.pending_runs "
                "WHERE state = 'RUNNING' AND lease_expires_at <= clock_timestamp() LIMIT %s)",
                (batch,),
            ).rowcount
            old = conn.execute(
                "DELETE FROM public.pending_runs WHERE id IN (SELECT id FROM "
                "public.pending_runs WHERE state = 'FAILED' "
                "AND failed_at <= clock_timestamp() - %s ORDER BY failed_at LIMIT %s) "
                "RETURNING failure_code",
                (retention, batch),
            ).fetchall()
            tally: dict[str, int] = {}
            for (code,) in old:
                tally[str(code)] = tally.get(str(code), 0) + 1
            for code, n in tally.items():
                conn.execute(
                    "INSERT INTO public.run_failure_counts (code, n) VALUES (%s, %s) "
                    "ON CONFLICT (code) DO UPDATE SET n = public.run_failure_counts.n + %s",
                    (code, n, n),
                )
            return CleanupResult(marked_failed=marked, deleted=len(old))

    def failure_counts(self) -> dict[str, int]:
        with _storage_errors(), self._connect() as conn:
            rows = conn.execute("SELECT code, n FROM public.run_failure_counts").fetchall()
            return {str(c): int(n) for c, n in rows}
