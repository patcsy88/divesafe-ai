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

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from pydantic import ValidationError

from divesafe.domain import AssessmentRecord
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

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 3000
_OPTIONS = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}"

GUARD_TRIGGERS = (
    "assessments_append_only_guard",
    "assessments_insert_guard",
    "assessments_no_truncate_guard",
)
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS public.assessments (
    id          text PRIMARY KEY CHECK (id ~ '^[0-9A-Za-z._-]{1,64}$'),
    record      jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.assessments_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    json_null constant jsonb := 'null'::jsonb;
    old_decision jsonb := COALESCE(OLD.record->'human_decision', json_null);
    old_actual jsonb := COALESCE(OLD.record->'actual_conditions', json_null);
    new_decision jsonb := COALESCE(NEW.record->'human_decision', json_null);
    new_actual jsonb := COALESCE(NEW.record->'actual_conditions', json_null);
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'assessments are never deleted';
    END IF;
    IF NEW.id <> OLD.id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (NEW.record - 'human_decision' - 'actual_conditions')
          IS DISTINCT FROM (OLD.record - 'human_decision' - 'actual_conditions')
       OR (old_decision <> json_null AND new_decision <> old_decision)
       OR (old_actual <> json_null AND new_actual <> old_actual)
    THEN
        RAISE EXCEPTION 'only a first human decision and first actual conditions may be appended';
    END IF;
    IF new_actual <> json_null AND new_decision = json_null THEN
        RAISE EXCEPTION 'actual conditions require a decision';
    END IF;
    IF old_decision = json_null AND new_decision <> json_null THEN
        IF NOT COALESCE(
            jsonb_typeof(new_decision) = 'object'
            AND length(btrim(new_decision->>'decided_by')) > 0
            AND new_decision->>'decision' IN ('GO', 'CAUTION', 'NO-GO', 'INSUFFICIENT EVIDENCE')
            AND jsonb_typeof(new_decision->'is_override') = 'boolean'
            AND (
                (new_decision->'is_override') = 'false'::jsonb
                OR length(btrim(COALESCE(new_decision->>'override_rationale', ''))) > 0
            ),
            false
        ) THEN
            RAISE EXCEPTION 'the human decision is malformed';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.assessments_insert_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.record->>'id' IS DISTINCT FROM NEW.id
       OR COALESCE(NEW.record->'human_decision', 'null'::jsonb) <> 'null'::jsonb
       OR COALESCE(NEW.record->'actual_conditions', 'null'::jsonb) <> 'null'::jsonb
    THEN
        RAISE EXCEPTION 'a new assessment must match its id and carry no decision';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.assessments_no_truncate() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    RAISE EXCEPTION 'assessments are never truncated';
END;
$$;

DROP TRIGGER IF EXISTS assessments_append_only_guard ON public.assessments;
CREATE TRIGGER assessments_append_only_guard
    BEFORE UPDATE OR DELETE ON public.assessments
    FOR EACH ROW EXECUTE FUNCTION public.assessments_append_only();

DROP TRIGGER IF EXISTS assessments_insert_guard ON public.assessments;
CREATE TRIGGER assessments_insert_guard
    BEFORE INSERT ON public.assessments
    FOR EACH ROW EXECUTE FUNCTION public.assessments_insert_guard();

DROP TRIGGER IF EXISTS assessments_no_truncate_guard ON public.assessments;
CREATE TRIGGER assessments_no_truncate_guard
    BEFORE TRUNCATE ON public.assessments
    FOR EACH STATEMENT EXECUTE FUNCTION public.assessments_no_truncate();
"""


def install_schema(admin_dsn: str, app_role: str | None = None) -> None:
    """Create the table and guard as the OWNER role, and optionally grant a least-privilege
    application role. Idempotent. Never run with the application's own credentials."""
    if app_role is not None and not _ROLE_NAME.match(app_role):
        raise ValueError("invalid application role name")
    try:
        with psycopg.connect(admin_dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            conn.execute(_SCHEMA)
            conn.execute("REVOKE ALL ON public.assessments FROM PUBLIC")
            if app_role is not None:
                role = sql.Identifier(app_role)
                conn.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE ON public.assessments TO {}").format(role)
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
                    "has_table_privilege('public.assessments', 'TRIGGER') "
                    "FROM pg_class c WHERE c.oid = 'public.assessments'::regclass"
                ).fetchone()
                assert row is not None
                names = (
                    "owns the table",
                    "is a superuser",
                    "can DELETE",
                    "can TRUNCATE",
                    "can alter triggers",
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
