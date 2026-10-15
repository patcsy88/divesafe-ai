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

from divesafe.domain import AssessmentRecord, DivePlan
from divesafe.services.migrations import GUARD_TRIGGERS, migrate, schema_problems
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


def install_schema(admin_dsn: str, app_role: str | None = None) -> None:
    """Create the table and guard as the OWNER role, and optionally grant a least-privilege
    application role. Idempotent. Never run with the application's own credentials."""
    if app_role is not None and (
        not _ROLE_NAME.fullmatch(app_role) or app_role == "public" or app_role.startswith("pg_")
    ):
        raise ValueError("invalid application role name")
    migrate(admin_dsn, app_role=app_role)
    try:
        with psycopg.connect(admin_dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
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


class PostgresAssessmentRepository:
    """One short-lived connection per operation, with connect, statement and lock timeouts."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, options=_OPTIONS)

    def verify(self, *, least_privilege: bool) -> list[str]:
        """Problems that make this store unsafe to use. Empty means the guard is in place.
        With `least_privilege`, also report an application role that owns the table, is a
        superuser, or can delete, truncate or alter triggers."""
        problems: list[str] = []
        with _storage_errors(), self._connect() as conn:
            if conn.execute("SELECT to_regclass('public.assessments')").fetchone() == (None,):
                return ["the assessments table does not exist (run db_init as the owner role)"]
            try:
                with conn.transaction():
                    problems += schema_problems(conn)
            except psycopg.errors.InsufficientPrivilege:
                problems.append("the application role cannot read the migration history")
            enabled = {
                row[0]
                for row in conn.execute(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'public.assessments'::regclass AND tgenabled = 'O'"
                )
            }
            problems += [
                f"guard trigger '{t}' is missing or disabled"
                for t in GUARD_TRIGGERS
                if t not in enabled
            ]
            if least_privilege:
                row = conn.execute(
                    "SELECT pg_get_userbyid(c.relowner) = current_user, "
                    "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                    "has_table_privilege('public.assessments', 'DELETE'), "
                    "has_table_privilege('public.assessments', 'TRUNCATE'), "
                    "has_table_privilege('public.assessments', 'TRIGGER'), "
                    "COALESCE(has_table_privilege('public.schema_migrations', 'INSERT, UPDATE, "
                    "DELETE, TRUNCATE'), false), "
                    "COALESCE((SELECT pg_get_userbyid(relowner) = current_user FROM pg_class "
                    "WHERE oid = to_regclass('public.pending_runs')), false), "
                    "COALESCE(has_table_privilege('public.pending_runs', 'TRUNCATE, TRIGGER'), "
                    "false), "
                    "COALESCE(has_table_privilege('public.run_failure_counts', "
                    "'DELETE, TRUNCATE'), false), "
                    "COALESCE((SELECT pg_get_userbyid(relowner) = current_user FROM pg_class "
                    "WHERE oid = to_regclass('public.run_failure_counts')), false) "
                    "FROM pg_class c WHERE c.oid = 'public.assessments'::regclass"
                ).fetchone()
                assert row is not None
                names = (
                    "owns the table",
                    "is a superuser",
                    "can DELETE",
                    "can TRUNCATE",
                    "can alter triggers",
                    "can write the migration history",
                    "owns the pending runs table",
                    "can TRUNCATE or alter triggers on the pending runs table",
                    "can delete the failure counts",
                    "owns the failure counts table",
                )
                problems += [
                    f"the application role {n}" for n, flag in zip(names, row, strict=True) if flag
                ]
        return problems

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
            cursor = conn.execute(
                "UPDATE public.assessments SET record = %s::jsonb WHERE id = %s",
                (new.model_dump_json(), new.id),
            )
            if cursor.rowcount != 1:
                raise RepositoryIntegrityError("the update did not apply")
            self._read_back_must_match(conn, new)

    @staticmethod
    def _read_back_must_match(conn: psycopg.Connection[Any], record: AssessmentRecord) -> None:
        row = conn.execute(
            "SELECT record::text FROM public.assessments WHERE id = %s", (record.id,)
        ).fetchone()
        if row is None:
            raise RepositoryIntegrityError("the record did not persist")
        if not _same(json.loads(record.model_dump_json()), json.loads(row[0])):
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
