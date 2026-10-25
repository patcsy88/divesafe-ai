"""Forward-only schema migrations for the assessments store (ADR 0015).

Applied by the owner role (`db_init`), never by the application. Each migration has a version,
a name and SQL; applied migrations are recorded with a SHA-256 checksum, so editing one that has
already run is detected and refused. There is no downgrade.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
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
# The function each guard trigger must call.
GUARD_FUNCTIONS = {
    "assessments_append_only_guard": "assessments_append_only",
    "assessments_insert_guard": "assessments_insert_guard",
    "assessments_no_truncate_guard": "assessments_no_truncate",
}

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
    "enable trigger",
    "enable replica",
    "enable always",
)
# The only later migrations that may name the guard, by version and checksum (ADR 0015). Editing
# one brings the word scan back, so its text cannot change without a new reviewed entry here.
REVIEWED_GUARD_MIGRATIONS: Mapping[int, str] = MappingProxyType(
    {
        3: "7588a25481499fa29d84a995fa9a851c8056690ec7b3591f6c3cd1f59b306297",
        4: "c95979fbf982c54ef0f05528d9549944d49a10c0210690854509c9042a36df8e",
    }
)
KNOWLEDGE_MIGRATION = 4
KNOWLEDGE_TABLES = ("kb_documents", "kb_chunks", "kb_status_events")
KNOWLEDGE_TRIGGERS = (
    "kb_documents_insert_guard",
    "kb_chunks_insert_guard",
    "kb_status_events_insert_guard",
    "kb_documents_append_only_guard",
    "kb_chunks_append_only_guard",
    "kb_status_events_append_only_guard",
    "kb_documents_no_truncate_guard",
    "kb_chunks_no_truncate_guard",
    "kb_status_events_no_truncate_guard",
)
# The function each knowledge trigger must call.
KNOWLEDGE_FUNCTIONS = {
    "kb_documents_insert_guard": "kb_documents_insert",
    "kb_chunks_insert_guard": "kb_chunks_insert",
    "kb_status_events_insert_guard": "kb_status_events_insert",
    "kb_documents_append_only_guard": "kb_no_update_delete",
    "kb_chunks_append_only_guard": "kb_no_update_delete",
    "kb_status_events_append_only_guard": "kb_no_update_delete",
    "kb_documents_no_truncate_guard": "kb_no_truncate",
    "kb_chunks_no_truncate_guard": "kb_no_truncate",
    "kb_status_events_no_truncate_guard": "kb_no_truncate",
}
# ADR 0022 condition 9: SHA-256 (lower-case hex) of pg_get_functiondef(oid) for each knowledge guard
# function, pinned per PostgreSQL MAJOR version (the text pg_get_functiondef returns can change
# between majors). Generated 2026-10-25 by `python -m divesafe.services.kb_pins` with
# DIVESAFE_PIN_APPLY=1 on fresh scratch databases on real PostgreSQL 16 (pgvector image) and 17;
# identical on both; generated on a fresh scratch database and confirmed by the live pin test.
# Never take values from the database being checked. Any other major has no entry and FAILS the
# check (fail closed). Regenerate when migration 4 changes.
KNOWLEDGE_FUNCTION_SHA256: Mapping[int, Mapping[str, str]] = MappingProxyType(
    {
        16: MappingProxyType(
            {
                "kb_chunks_insert": (
                    "1d93b260c9ea2bf9991f871982b44514061643b1bf3fa23b0b4068af7f3e5711"
                ),
                "kb_documents_insert": (
                    "5ed6eae06b5524a62e17fe97eae7f7bc7abb0b3cd104ffcb7e882259e68db985"
                ),
                "kb_no_truncate": (
                    "4894f69b62e3fe0eae57e53d76a816f8eafed92ffc0cd9812d46287d31105229"
                ),
                "kb_no_update_delete": (
                    "9d0491680703bc57775b64149f44e3de0da6bbfcf42b094170cff47b80f36a47"
                ),
                "kb_status_events_insert": (
                    "b2f5d9e7c4e1caf4b1cf90e2e0552998762c54d02349a631d4cb8e80674f278b"
                ),
            }
        ),
        17: MappingProxyType(
            {
                "kb_chunks_insert": (
                    "1d93b260c9ea2bf9991f871982b44514061643b1bf3fa23b0b4068af7f3e5711"
                ),
                "kb_documents_insert": (
                    "5ed6eae06b5524a62e17fe97eae7f7bc7abb0b3cd104ffcb7e882259e68db985"
                ),
                "kb_no_truncate": (
                    "4894f69b62e3fe0eae57e53d76a816f8eafed92ffc0cd9812d46287d31105229"
                ),
                "kb_no_update_delete": (
                    "9d0491680703bc57775b64149f44e3de0da6bbfcf42b094170cff47b80f36a47"
                ),
                "kb_status_events_insert": (
                    "b2f5d9e7c4e1caf4b1cf90e2e0552998762c54d02349a631d4cb8e80674f278b"
                ),
            }
        ),
    }
)
# Expected pg_trigger.tgtype per trigger. Same provenance as above (identical on 16 and 17). These
# are the PostgreSQL tgtype bit sums, as printed by the server, for BEFORE ROW INSERT (7), BEFORE
# ROW UPDATE OR DELETE (27) and BEFORE STATEMENT TRUNCATE (34); no bit constants are defined here.
# Derivation from the migration 4 CREATE TRIGGER text: *_insert_guard are BEFORE INSERT FOR EACH
# ROW; *_append_only_guard are BEFORE UPDATE OR DELETE FOR EACH ROW; *_no_truncate_guard are BEFORE
# TRUNCATE FOR EACH STATEMENT. A trigger without an entry FAILS the check.
KNOWLEDGE_TRIGGER_TGTYPE: Mapping[str, int] = MappingProxyType(
    {
        "kb_documents_insert_guard": 7,
        "kb_chunks_insert_guard": 7,
        "kb_status_events_insert_guard": 7,
        "kb_documents_append_only_guard": 27,
        "kb_chunks_append_only_guard": 27,
        "kb_status_events_append_only_guard": 27,
        "kb_documents_no_truncate_guard": 34,
        "kb_chunks_no_truncate_guard": 34,
        "kb_status_events_no_truncate_guard": 34,
    }
)
# Forbidden only in migrations after the one that creates them.
KNOWLEDGE_NAMES = (
    *KNOWLEDGE_TRIGGERS,
    *sorted(set(KNOWLEDGE_FUNCTIONS.values())),
    "kb_status_event_order_seq",
)
_KB_TABLE_DDL = re.compile(
    r"\b(drop|rename)\b[^;]*\bkb_(documents|chunks|status_events)\b"
    r"|\balter table\b[^;]*\bkb_[a-z_]*[^;]*\b(rename|drop|owner|alter column|set default)\b"
    r"|\b(grant|revoke)\b[^;]*\bkb_[a-z_]+"
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


_V2 = """
CREATE TABLE IF NOT EXISTS public.pending_runs (
    id               text PRIMARY KEY CHECK (id ~ '^[0-9A-Za-z._-]{1,64}$'),
    created_by       text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 200),
    assigned_decider text NOT NULL CHECK (length(assigned_decider) BETWEEN 1 AND 200),
    plan             jsonb NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
    created_at       timestamptz NOT NULL DEFAULT now(),
    state            text NOT NULL CHECK (state IN ('RUNNING', 'FAILED')),
    token_hash       text NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    lease_expires_at timestamptz NOT NULL,
    failed_at        timestamptz,
    failure_code     text CHECK (failure_code IN
        ('timeout', 'storage', 'internal', 'shutdown', 'start_passed')),
    CHECK ((state = 'FAILED') = (failed_at IS NOT NULL)),
    CHECK ((state = 'FAILED') = (failure_code IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS pending_runs_running_lease
    ON public.pending_runs (lease_expires_at) WHERE state = 'RUNNING';
CREATE INDEX IF NOT EXISTS pending_runs_failed_at
    ON public.pending_runs (failed_at) WHERE state = 'FAILED';

CREATE TABLE IF NOT EXISTS public.run_failure_counts (
    code text PRIMARY KEY CHECK (code IN
        ('timeout', 'storage', 'internal', 'shutdown', 'start_passed')),
    n    bigint NOT NULL CHECK (n >= 0)
);
"""


_V3 = """
ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER assessments_append_only_guard;
ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER assessments_insert_guard;
ALTER TABLE public.assessments ENABLE ALWAYS TRIGGER assessments_no_truncate_guard;
"""


_V4 = """
CREATE SEQUENCE public.kb_status_event_order_seq AS bigint;
REVOKE ALL ON SEQUENCE public.kb_status_event_order_seq FROM PUBLIC;

CREATE TABLE public.kb_documents (
    document_id            text NOT NULL
        CHECK (document_id ~ '^[a-z0-9]+(-[a-z0-9]+){0,2}$' AND length(document_id) <= 40),
    version                integer NOT NULL CHECK (version BETWEEN 1 AND 999999),
    title                  text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    origin                 text NOT NULL
        CHECK (origin IN ('dive_professional', 'published_document')),
    author_role            text NOT NULL
        CHECK (author_role IN ('dive_professional', 'published_source')),
    approver_role          text NOT NULL CHECK (approver_role = 'dive_professional'),
    effective_date         date NOT NULL,
    review_by_date         date NOT NULL,
    licence                text NOT NULL CHECK (length(licence) BETWEEN 1 AND 500),
    licence_reference_present boolean NOT NULL CHECK (licence_reference_present),
    attribution            text
        CHECK (attribution IS NULL OR length(attribution) BETWEEN 1 AND 300),
    site_id                text CHECK (site_id IS NULL OR site_id ~ '^[a-z0-9_-]{1,64}$'),
    all_sites              boolean NOT NULL,
    display_permitted      boolean NOT NULL CHECK (display_permitted),
    supersedes             integer CHECK (supersedes BETWEEN 1 AND 999999),
    manifest_sha256        text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (document_id, version),
    CHECK (all_sites = (site_id IS NULL)),
    CHECK (effective_date <= review_by_date),
    CHECK (review_by_date <= (effective_date + interval '12 months')::date),
    CHECK (supersedes IS NULL OR supersedes < version)
);

CREATE TABLE public.kb_chunks (
    document_id    text NOT NULL,
    version        integer NOT NULL CHECK (version BETWEEN 1 AND 999999),
    chunk          integer NOT NULL CHECK (chunk BETWEEN 1 AND 999999),
    section        text NOT NULL CHECK (length(section) BETWEEN 1 AND 200),
    chunk_text     text NOT NULL CHECK (length(chunk_text) BETWEEN 1 AND 1500),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (document_id, version, chunk),
    FOREIGN KEY (document_id, version) REFERENCES public.kb_documents (document_id, version)
);

CREATE TABLE public.kb_status_events (
    event_order   bigint NOT NULL UNIQUE,
    document_id   text NOT NULL,
    version       integer NOT NULL CHECK (version BETWEEN 1 AND 999999),
    status        text NOT NULL CHECK (status IN ('DRAFT', 'APPROVED', 'WITHDRAWN')),
    hash_checked  boolean NOT NULL,
    recorded_at   timestamptz NOT NULL,
    CHECK (status = 'APPROVED' OR hash_checked = false),
    FOREIGN KEY (document_id, version) REFERENCES public.kb_documents (document_id, version)
);

REVOKE ALL ON public.kb_documents, public.kb_chunks, public.kb_status_events FROM PUBLIC;

CREATE FUNCTION public.kb_no_update_delete() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'knowledge tables are append-only';
END;
$$;

CREATE FUNCTION public.kb_no_truncate() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'knowledge tables are never truncated';
END;
$$;

CREATE FUNCTION public.kb_documents_insert() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    newest integer;
    family_site text;
    family_all boolean;
    family_found boolean;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'knowledge writes need a READ COMMITTED transaction';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(1220001, pg_catalog.hashtext(NEW.document_id));
    SELECT max(d.version) INTO newest
        FROM public.kb_documents d WHERE d.document_id = NEW.document_id;
    IF newest IS NOT NULL AND NEW.version <= newest THEN
        RAISE EXCEPTION 'a new version must be greater than every existing version';
    END IF;
    SELECT d.site_id, d.all_sites, true INTO family_site, family_all, family_found
        FROM public.kb_documents d WHERE d.document_id = NEW.document_id LIMIT 1;
    IF family_found AND (family_site IS DISTINCT FROM NEW.site_id
                         OR family_all IS DISTINCT FROM NEW.all_sites) THEN
        RAISE EXCEPTION 'every version of a document must have the same site';
    END IF;
    IF NEW.supersedes IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.kb_documents d
        WHERE d.document_id = NEW.document_id AND d.version = NEW.supersedes
    ) THEN
        RAISE EXCEPTION 'supersedes must name an earlier version of the same document';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.kb_chunks_insert() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'knowledge writes need a READ COMMITTED transaction';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(1220001, pg_catalog.hashtext(NEW.document_id));
    IF EXISTS (
        SELECT 1 FROM public.kb_status_events e
        WHERE e.document_id = NEW.document_id AND e.version = NEW.version
    ) THEN
        RAISE EXCEPTION 'chunks cannot be added to a version that has a status event';
    END IF;
    IF NEW.content_sha256 IS DISTINCT FROM pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(NEW.chunk_text, 'UTF8')), 'hex'
    ) THEN
        RAISE EXCEPTION 'the chunk text does not match its SHA-256';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.kb_status_events_insert() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    previous text;
    effective date;
    review_by date;
    document_found boolean;
    stamp timestamptz;
    approved_on date;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'knowledge writes need a READ COMMITTED transaction';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(1220001, pg_catalog.hashtext(NEW.document_id));
    SELECT d.effective_date, d.review_by_date, true INTO effective, review_by, document_found
        FROM public.kb_documents d
        WHERE d.document_id = NEW.document_id AND d.version = NEW.version;
    IF document_found IS NOT TRUE THEN
        RAISE EXCEPTION 'a status event needs an existing document version';
    END IF;
    SELECT e.status INTO previous FROM public.kb_status_events e
        WHERE e.document_id = NEW.document_id AND e.version = NEW.version
        ORDER BY e.event_order DESC LIMIT 1;
    IF previous = 'WITHDRAWN' AND NEW.status = 'WITHDRAWN' THEN
        RAISE EXCEPTION 'this version is already withdrawn' USING ERRCODE = 'KB001';
    END IF;
    IF NOT COALESCE((
        (previous IS NULL AND NEW.status = 'DRAFT')
        OR (previous = 'DRAFT' AND NEW.status IN ('APPROVED', 'WITHDRAWN'))
        OR (previous = 'APPROVED' AND NEW.status = 'WITHDRAWN')
    ), false) THEN
        RAISE EXCEPTION 'this status change is not allowed';
    END IF;
    IF NEW.status = 'APPROVED' AND NOT COALESCE((
        SELECT true FROM public.kb_chunks c
        WHERE c.document_id = NEW.document_id AND c.version = NEW.version LIMIT 1
    ), false) THEN
        RAISE EXCEPTION 'a version without chunks cannot be approved';
    END IF;
    stamp := pg_catalog.clock_timestamp();
    IF NEW.status = 'APPROVED' THEN
        approved_on := (stamp AT TIME ZONE 'Asia/Kuala_Lumpur')::date;
        IF NOT COALESCE(effective <= (approved_on + interval '12 months')::date, false) THEN
            RAISE EXCEPTION 'the effective date is more than 12 months after approval';
        END IF;
        IF NOT COALESCE(review_by >= approved_on, false) THEN
            RAISE EXCEPTION 'the review-by date is before the approval date';
        END IF;
    END IF;
    NEW.event_order := pg_catalog.nextval('public.kb_status_event_order_seq');
    NEW.recorded_at := stamp;
    RETURN NEW;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.kb_no_update_delete() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.kb_no_truncate() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.kb_documents_insert() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.kb_chunks_insert() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.kb_status_events_insert() FROM PUBLIC;

CREATE TRIGGER kb_documents_insert_guard
    BEFORE INSERT ON public.kb_documents
    FOR EACH ROW EXECUTE FUNCTION public.kb_documents_insert();
CREATE TRIGGER kb_chunks_insert_guard
    BEFORE INSERT ON public.kb_chunks
    FOR EACH ROW EXECUTE FUNCTION public.kb_chunks_insert();
CREATE TRIGGER kb_status_events_insert_guard
    BEFORE INSERT ON public.kb_status_events
    FOR EACH ROW EXECUTE FUNCTION public.kb_status_events_insert();
CREATE TRIGGER kb_documents_append_only_guard
    BEFORE UPDATE OR DELETE ON public.kb_documents
    FOR EACH ROW EXECUTE FUNCTION public.kb_no_update_delete();
CREATE TRIGGER kb_chunks_append_only_guard
    BEFORE UPDATE OR DELETE ON public.kb_chunks
    FOR EACH ROW EXECUTE FUNCTION public.kb_no_update_delete();
CREATE TRIGGER kb_status_events_append_only_guard
    BEFORE UPDATE OR DELETE ON public.kb_status_events
    FOR EACH ROW EXECUTE FUNCTION public.kb_no_update_delete();
CREATE TRIGGER kb_documents_no_truncate_guard
    BEFORE TRUNCATE ON public.kb_documents
    FOR EACH STATEMENT EXECUTE FUNCTION public.kb_no_truncate();
CREATE TRIGGER kb_chunks_no_truncate_guard
    BEFORE TRUNCATE ON public.kb_chunks
    FOR EACH STATEMENT EXECUTE FUNCTION public.kb_no_truncate();
CREATE TRIGGER kb_status_events_no_truncate_guard
    BEFORE TRUNCATE ON public.kb_status_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.kb_no_truncate();

ALTER TABLE public.kb_documents ENABLE ALWAYS TRIGGER kb_documents_insert_guard;
ALTER TABLE public.kb_chunks ENABLE ALWAYS TRIGGER kb_chunks_insert_guard;
ALTER TABLE public.kb_status_events ENABLE ALWAYS TRIGGER kb_status_events_insert_guard;
ALTER TABLE public.kb_documents ENABLE ALWAYS TRIGGER kb_documents_append_only_guard;
ALTER TABLE public.kb_chunks ENABLE ALWAYS TRIGGER kb_chunks_append_only_guard;
ALTER TABLE public.kb_status_events ENABLE ALWAYS TRIGGER kb_status_events_append_only_guard;
ALTER TABLE public.kb_documents ENABLE ALWAYS TRIGGER kb_documents_no_truncate_guard;
ALTER TABLE public.kb_chunks ENABLE ALWAYS TRIGGER kb_chunks_no_truncate_guard;
ALTER TABLE public.kb_status_events ENABLE ALWAYS TRIGGER kb_status_events_no_truncate_guard;
"""


def _harden(conn: psycopg.Connection[Any], *, read_only: bool = False) -> None:
    """Inside the caller's transaction: built-ins only on the search path, and identifiers are not
    force-quoted (which would change pg_get_functiondef). `read_only` uses SET LOCAL
    transaction_read_only with SET LOCAL, so it also works inside a savepoint."""
    conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")
    conn.execute("SET LOCAL quote_all_identifiers = off")
    if read_only:
        conn.execute("SET LOCAL transaction_read_only = on")


def knowledge_function_hashes(conn: psycopg.Connection[Any]) -> tuple[int, dict[str, str]]:
    """(PostgreSQL major version, function name -> SHA-256 hex of its definition) for the five
    knowledge functions in `public`. Read-only; used by `kb_pins` to generate the pins."""
    _harden(conn)
    major_row = conn.execute("SELECT pg_catalog.current_setting('server_version_num')").fetchone()
    assert major_row is not None
    rows = conn.execute(
        "SELECT p.proname, pg_catalog.encode(pg_catalog.sha256("
        "pg_catalog.convert_to(pg_catalog.pg_get_functiondef(p.oid), 'UTF8')), 'hex') "
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = ANY(%s::text[]) ORDER BY p.proname",
        (sorted(set(KNOWLEDGE_FUNCTIONS.values())),),
    ).fetchall()
    return int(major_row[0]) // 10000, {str(r[0]): str(r[1]) for r in rows}


def knowledge_trigger_types(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """trigger name -> pg_trigger.tgtype for the knowledge tables. Read-only; for `kb_pins`."""
    _harden(conn)
    rows = conn.execute(
        "SELECT t.tgname, t.tgtype FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND s.nspname = 'public' AND c.relname = ANY(%s::text[]) "
        "ORDER BY t.tgname",
        (list(KNOWLEDGE_TABLES),),
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def knowledge_problems(
    conn: psycopg.Connection[Any], *, owner_is_current_user: bool, read_only: bool = False
) -> list[str]:
    """ADR 0022 condition 9: a set of read-only checks on the three knowledge tables. It does NOT
    prove they are exactly as migration 4 made them. It checks: the nine non-internal triggers
    (names, table, function in public, ENABLE ALWAYS, trigger type and plain shape), the pinned
    definition hash of each guard function, no rewrite rules, no row-level security or policies,
    no inheritance, permanent tables, and the owner of the tables and of schema public.

    It does NOT detect: column definitions or constraints; sequence parameters; function owner or
    ACL; the application role's privileges on kb_* (the loader checks its own privileges in
    kb_privileges; app-role privileges on kb_* are NOT checked by verify(), ADR 0022 condition
    11); wholesale re-owning of everything, which `verify()` cannot see because its
    expected owner is the owner of public.assessments; tampering that is undone before the next
    start-up; superuser edits of the catalogue; SECURITY DEFINER wrappers. Messages are fixed text.
    The expected owner is the role running the migration (`owner_is_current_user`, `migrate`) or
    else the owner of public.assessments (`verify`, which may run as another role). The tables
    must exist; the caller checks that first."""
    _harden(conn, read_only=read_only)
    problems: list[str] = []
    tables = list(KNOWLEDGE_TABLES)
    in_kb = (
        "(SELECT c.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace s "
        "ON s.oid = c.relnamespace WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[]))"
    )
    # (a) triggers: exactly the nine, each on its table, calling its function in public, enabled
    # ALWAYS, with the pinned tgtype and a plain shape (no WHEN, no column list, no arguments, not
    # deferrable, not a constraint trigger, no transition tables); and (b) the definition of the
    # function each one actually calls matches the pin. pg_get_triggerdef is not used: it depends
    # on search_path.
    rows = conn.execute(
        "SELECT c.relname, t.tgname, p.proname, n.nspname, t.tgenabled, "
        "pg_catalog.encode(pg_catalog.sha256("
        "pg_catalog.convert_to(pg_catalog.pg_get_functiondef(t.tgfoid), 'UTF8')), 'hex'), "
        "t.tgtype, "
        "(t.tgqual IS NULL AND t.tgattr::pg_catalog.text = '' AND t.tgnargs = 0 "
        "AND NOT t.tgdeferrable AND t.tgconstraint = 0 "
        "AND t.tgoldtable IS NULL AND t.tgnewtable IS NULL) "
        "FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
        f"WHERE NOT t.tgisinternal AND t.tgrelid IN {in_kb}",
        (tables,),
    ).fetchall()
    found = [str(r[1]) for r in rows]
    if len(found) != len(set(found)) or set(found) != set(KNOWLEDGE_TRIGGERS):
        problems.append("the knowledge tables do not carry exactly the nine guard triggers")
    major_row = conn.execute("SELECT pg_catalog.current_setting('server_version_num')").fetchone()
    pins = KNOWLEDGE_FUNCTION_SHA256.get(int(major_row[0]) // 10000, {}) if major_row else {}
    for relname, tgname, proname, nspname, enabled, digest, tgtype, plain in rows:
        name = str(tgname)
        if name not in KNOWLEDGE_FUNCTIONS:
            continue
        if str(relname) != next((t for t in KNOWLEDGE_TABLES if name.startswith(f"{t}_")), None):
            problems.append(f"knowledge trigger '{name}' is on the wrong table")
        function = KNOWLEDGE_FUNCTIONS[name]
        if str(proname) != function or str(nspname) != "public":
            problems.append(f"knowledge trigger '{name}' does not call {function} in public")
        if str(enabled) != "A":
            problems.append(f"knowledge trigger '{name}' is not ENABLE ALWAYS")
        expected_type = KNOWLEDGE_TRIGGER_TGTYPE.get(name)
        if expected_type is None:
            problems.append(f"no pinned trigger type for knowledge trigger '{name}'")
        elif int(tgtype) != expected_type:
            problems.append(f"knowledge trigger '{name}' does not have its expected trigger type")
        if plain is not True:
            problems.append(f"knowledge trigger '{name}' does not have the expected plain shape")
        pinned = pins.get(function)
        if pinned is None:
            problems.append(f"no pinned definition hash for {function} on this PostgreSQL version")
        elif str(digest) != pinned:
            problems.append(f"the definition of {function} does not match its pinned hash")
    # (c) rules, (d) row security and policies, (e) inheritance
    counts = conn.execute(
        "SELECT (SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite "
        f"WHERE ev_class IN {in_kb}), "
        f"(SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy WHERE polrelid IN {in_kb}), "
        f"(SELECT pg_catalog.count(*) FROM pg_catalog.pg_inherits "
        f"WHERE inhparent IN {in_kb} OR inhrelid IN {in_kb})",
        (tables, tables, tables, tables),
    ).fetchone()
    if counts is None or int(counts[0]) != 0:
        problems.append("a rewrite rule exists on a knowledge table")
    if counts is None or int(counts[1]) != 0:
        problems.append("a row-level security policy exists on a knowledge table")
    if counts is None or int(counts[2]) != 0:
        problems.append("a knowledge table takes part in table inheritance")
    # (d) row security flags, (f) persistence, (g) owners
    expected_owner = (
        "(SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)"
        if owner_is_current_user
        else "(SELECT relowner FROM pg_catalog.pg_class "
        "WHERE oid = pg_catalog.to_regclass('public.assessments'))"
    )
    state = conn.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, c.relpersistence, "
        f"c.relowner IS NOT DISTINCT FROM {expected_owner} AND {expected_owner} IS NOT NULL "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[])",
        (tables,),
    ).fetchall()
    if {str(r[0]) for r in state} != set(KNOWLEDGE_TABLES):
        problems.append("a knowledge table could not be read")
    for relname, rls, forced, persistence, owner_ok in state:
        if rls is not False or forced is not False:
            problems.append(f"row-level security is on for {relname}")
        if str(persistence) != "p":
            problems.append(f"{relname} is not a permanent table")
        if owner_ok is not True:
            problems.append(f"{relname} is not owned by the expected owner role")
    schema_owner = conn.execute(
        f"SELECT {expected_owner} IS NOT NULL AND (nspowner = {expected_owner} "
        # PostgreSQL 15+ owns `public` by the built-in pg_database_owner, i.e. the database owner.
        "OR (nspowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = 'pg_database_owner') "
        "AND (SELECT datdba FROM pg_catalog.pg_database "
        f"WHERE datname = pg_catalog.current_database()) = {expected_owner})) "
        "FROM pg_catalog.pg_namespace WHERE nspname = 'public'"
    ).fetchone()
    if schema_owner is None or schema_owner[0] is not True:
        problems.append("the public schema is not owned by the expected owner role")
    return problems


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


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "assessments table and guard triggers", _V1),
    Migration(2, "pending runs and failure counts", _V2),
    Migration(3, "guard triggers fire in every replication role", _V3),
    Migration(4, "knowledge store tables and guard triggers", _V4),
)

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
    """Refuse a migration list in which a later migration mentions the audit guard, or one after
    the knowledge migration touches the knowledge guard or drops or renames a knowledge table."""
    for m in known[1:]:
        lowered = " ".join(m.sql.lower().split())
        if m.version > KNOWLEDGE_MIGRATION:
            for word in KNOWLEDGE_NAMES:
                if word in lowered:
                    raise MigrationError(f"migration {m.version} touches the knowledge guard")
            if _KB_TABLE_DDL.search(lowered):
                raise MigrationError(f"migration {m.version} touches the knowledge guard")
        if REVIEWED_GUARD_MIGRATIONS.get(m.version) == m.checksum:
            continue
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
                    "WHERE tgrelid = 'public.assessments'::regclass AND tgenabled = 'A'"
                )
            }
            if not set(GUARD_TRIGGERS) <= enabled:
                raise MigrationError("the audit guard is not intact after migrating")
            if len(known) >= KNOWLEDGE_MIGRATION:
                kb_enabled = {
                    r[0]
                    for r in conn.execute(
                        "SELECT tgname FROM pg_trigger WHERE tgenabled = 'A' AND tgrelid IN "
                        "('public.kb_documents'::regclass, 'public.kb_chunks'::regclass, "
                        "'public.kb_status_events'::regclass)"
                    )
                }
                if not set(KNOWLEDGE_TRIGGERS) <= kb_enabled:
                    raise MigrationError("the knowledge guard is not intact after migrating")
                kb_problems = knowledge_problems(conn, owner_is_current_user=True)
                if kb_problems:
                    raise MigrationError(
                        "the knowledge guard is not intact after migrating: "
                        + "; ".join(kb_problems)
                    )
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
