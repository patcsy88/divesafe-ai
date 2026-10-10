"""Forward-only schema migrations for the assessments store (ADR 0015).

Applied by the owner role (`db_init`), never by the application. Each migration has a version,
a name and SQL; applied migrations are recorded with a SHA-256 checksum, so editing one that has
already run is detected and refused. There is no downgrade.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from divesafe.services.repository import RepositoryUnavailableError

CONNECT_TIMEOUT_SECONDS = 5
LOCK_TIMEOUT_MS = 30_000

GUARD_TRIGGERS = (
    "assessments_append_only_guard",
    "assessments_insert_guard",
    "assessments_no_truncate_guard",
)

# A migration after the first may not touch the guard. Changing it needs a new reviewed decision
# (risk-reviewer) and an explicit edit of this test-visible list, not a quiet SQL change.
FORBIDDEN_IN_LATER_MIGRATIONS = (
    "assessments_append_only",
    "assessments_insert_guard",
    "assessments_no_truncate",
    "disable trigger",
    "drop trigger",
    "alter function",
    "drop function",
    "disable row level",
    "session_replication_role",
)
_LOCK_KEY = 7_281_903_114

_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    integer PRIMARY KEY CHECK (version > 0),
    name       text NOT NULL CHECK (length(btrim(name)) > 0),
    checksum   text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""

_V1 = """
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


class MigrationError(RuntimeError):
    """The migration history and the code disagree, or a migration list is malformed. Messages
    never contain connection details."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (Migration(1, "assessments table and guard triggers", _V1),)

Applied = tuple[int, str, str]  # version, name, checksum


def validate_known(known: Sequence[Migration]) -> None:
    for expected, m in enumerate(known, start=1):
        if m.version != expected:
            raise MigrationError("migration versions must be 1, 2, 3, ... with no gaps")
        if not m.name.strip() or not m.sql.strip():
            raise MigrationError("a migration needs a name and SQL")


def plan(applied: Sequence[Applied], known: Sequence[Migration] = MIGRATIONS) -> list[Migration]:
    """The migrations still to run. Raises if the database history is not a prefix of `known`
    (a version the code does not know, a gap, a renamed or edited migration)."""
    validate_known(known)
    rows = sorted(applied)
    if len(rows) > len(known):
        raise MigrationError("the database is ahead of this code; deploy a newer version")
    for row, m in zip(rows, known, strict=False):
        if row[0] != m.version:
            raise MigrationError("the applied migrations have a gap or an unknown version")
        if row[1] != m.name or row[2] != m.checksum:
            raise MigrationError(f"migration {m.version} was changed after it was applied")
    return list(known[len(rows) :])


def guarded(known: Sequence[Migration]) -> None:
    """Refuse a migration list in which a later migration mentions the audit guard."""
    for m in known[1:]:
        lowered = m.sql.lower()
        for word in FORBIDDEN_IN_LATER_MIGRATIONS:
            if word in lowered:
                raise MigrationError(f"migration {m.version} touches the audit guard")


def migrate(
    admin_dsn: str, known: Sequence[Migration] = MIGRATIONS, *, app_role: str | None = None
) -> list[int]:
    """Apply pending migrations in one transaction under an advisory lock. Returns the versions
    applied. Idempotent and safe to run concurrently. Refuses to run as the application role."""
    guarded(known)
    try:
        with psycopg.connect(
            admin_dsn,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            options=f"-c lock_timeout={LOCK_TIMEOUT_MS}",
        ) as conn:
            user = conn.execute("SELECT current_user").fetchone()
            if app_role is not None and user is not None and user[0] == app_role:
                raise MigrationError("migrations must run as the owner role, not the application")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            conn.execute(_TABLE)
            conn.execute("REVOKE ALL ON public.schema_migrations FROM PUBLIC")
            rows = conn.execute(
                "SELECT version, name, checksum FROM public.schema_migrations"
            ).fetchall()
            pending = plan([(int(r[0]), str(r[1]), str(r[2])) for r in rows], known)
            for m in pending:
                conn.execute(m.sql)
                conn.execute(
                    "INSERT INTO public.schema_migrations (version, name, checksum) "
                    "VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
            enabled = {
                r[0]
                for r in conn.execute(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'public.assessments'::regclass AND tgenabled = 'O'"
                )
            }
            if not set(GUARD_TRIGGERS) <= enabled:
                raise MigrationError("the audit guard is not intact after migrating")
            return [m.version for m in pending]
    except psycopg.Error:
        raise RepositoryUnavailableError("could not apply the schema migrations") from None


def schema_problems(
    conn: psycopg.Connection[Any], known: Sequence[Migration] = MIGRATIONS
) -> list[str]:
    """Why the database is not at the version this code needs. Empty means it is."""
    if conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone() == (None,):
        return ["the schema has no migration history (run db_init as the owner role)"]
    rows = conn.execute("SELECT version, name, checksum FROM public.schema_migrations").fetchall()
    try:
        pending = plan([(int(r[0]), str(r[1]), str(r[2])) for r in rows], known)
    except MigrationError as exc:
        return [str(exc)]
    if pending:
        return [
            f"the database is at schema version {len(rows)} but this code needs {len(known)} "
            "(run db_init to migrate)"
        ]
    return []
