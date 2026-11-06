"""Forward-only schema migrations for the assessments store (ADR 0015).

Applied by the owner role (`db_init`), never by the application. Each migration has a version,
a name and SQL; applied migrations are recorded with a SHA-256 checksum, so editing one that has
already run is detected and refused. There is no downgrade.
"""

from __future__ import annotations

import hashlib
import json
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
        5: "76afa4028d3cd312dfd1b8a472b584b639077cd2a35a2e3b529d75c1ae685f40",
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
# ADR 0022 condition 11.1 and 11.2: SHA-256 per knowledge table of its columns, constraints and
# indexes as read by `_shape_digests` (see `_SHAPE_QUERIES`), pinned per PostgreSQL major. Same
# provenance rules as the function pins: generated by `kb_pins` with DIVESAFE_PIN_APPLY=1 on fresh
# scratch databases, confirmed by the live pin test, never taken from the database being checked.
# Regenerate when migration 4 changes. A missing major or key FAILS the check.
KNOWLEDGE_SHAPE_ASPECTS = ("columns", "constraints", "indexes")
_SHAPE_PINS_16_17 = MappingProxyType(
    {
        "kb_documents.columns": "0c3d5dcde3393419fb3bb315c3890cab8a219c90c27c40ca38189006da056677",
        "kb_chunks.columns": "cec00ad100dc7f4d8f5d6e17629238d84c69991550725b0407bfb8fc65684bd3",
        "kb_status_events.columns": (
            "916f509d5ce8c122086bb553c589a779fe2c670c74b1a6093d45ac1a012891ec"
        ),
        "kb_documents.constraints": (
            "b72c962c573684ba54cd57a81329d06a74a4c5df0df7fde5e6bb5519c79d92dc"
        ),
        "kb_chunks.constraints": "6e47749c28e30d5014118ce1d6d6c5c700b1cb4ba3ce74f9ff82b7764e951883",
        "kb_status_events.constraints": (
            "93161e56d214e6c82afebb81abb9b373a8108c3390c119ba5886685fc260f6df"
        ),
        "kb_documents.indexes": "0128aa05e0dfee325547ab4b5714976aa0eba7a301cc5bf71c8d6856c3685043",
        "kb_chunks.indexes": "89b6108613d353dd8d21295b87b7817dce0455d430bbc3924e7f1b5ee1263895",
        "kb_status_events.indexes": (
            "62167ad906ef5534652060d196885d1b12526718666262f0aa3028cf8c359ba7"
        ),
    }
)
# Generated separately on 16 and 17 (2026-11-06); the outputs were identical.
KNOWLEDGE_SHAPE_SHA256: Mapping[int, Mapping[str, str]] = MappingProxyType(
    {16: _SHAPE_PINS_16_17, 17: _SHAPE_PINS_16_17}
)
# ADR 0022 condition 11.3: the ordering sequence of kb_status_events and its parameters as created
# by migration 4 (`CREATE SEQUENCE ... AS bigint` with PostgreSQL's defaults): type, start,
# increment, maximum, minimum, cache, cycle, persistence.
KNOWLEDGE_SEQUENCE = "kb_status_event_order_seq"
KNOWLEDGE_SEQUENCE_PARAMETERS = ("bigint", 1, 1, 9223372036854775807, 1, 1, False, "p")
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


_V5 = """
CREATE OR REPLACE FUNCTION public.assessments_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE
    json_null constant jsonb := 'null'::jsonb;
    old_decision jsonb := COALESCE(OLD.record->'human_decision', json_null);
    old_actual jsonb := COALESCE(OLD.record->'actual_conditions', json_null);
    new_decision jsonb := COALESCE(NEW.record->'human_decision', json_null);
    new_actual jsonb := COALESCE(NEW.record->'actual_conditions', json_null);
    old_moves jsonb := COALESCE(OLD.record->'reassignments', '[]'::jsonb);
    new_moves jsonb := COALESCE(NEW.record->'reassignments', '[]'::jsonb);
    is_move boolean := false;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'assessments are never deleted';
    END IF;
    -- A reassignment: while nothing is decided, exactly one move is appended to `reassignments`
    -- and `assigned_decider` becomes that move's target. Nothing else may change.
    IF old_decision = json_null AND new_decision = json_null
       AND old_actual = json_null AND new_actual = json_null
       AND jsonb_typeof(old_moves) = 'array' AND jsonb_typeof(new_moves) = 'array'
       AND jsonb_array_length(new_moves) = jsonb_array_length(old_moves) + 1
       AND jsonb_array_length(new_moves) <= 10
    THEN
        is_move := COALESCE(
            (NEW.record - 'assigned_decider' - 'reassignments')
                IS NOT DISTINCT FROM (OLD.record - 'assigned_decider' - 'reassignments')
            AND (new_moves - (jsonb_array_length(new_moves) - 1)) = old_moves
            AND (new_moves->-1)->>'from_decider' = OLD.record->>'assigned_decider'
            AND (new_moves->-1)->>'to_decider' = NEW.record->>'assigned_decider'
            AND lower(btrim((new_moves->-1)->>'from_decider'))
                <> lower(btrim((new_moves->-1)->>'to_decider')),
            false
        );
    END IF;
    IF NEW.id <> OLD.id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (NOT is_move AND (NEW.record - 'human_decision' - 'actual_conditions')
          IS DISTINCT FROM (OLD.record - 'human_decision' - 'actual_conditions'))
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
    """Inside the caller's transaction: built-ins only on the search path, identifiers not
    force-quoted (which would change pg_get_functiondef), and the interval, date and time zone
    output fixed (they change how constants in pg_get_constraintdef / pg_get_expr are written, so a
    role or database setting could otherwise change a shape digest). SET LOCAL lasts until the end
    of the enclosing top-level transaction: a released savepoint keeps it and a rolled-back one
    undoes it, so with `read_only` inside a savepoint the rest of that transaction is read-only."""
    conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")
    conn.execute("SET LOCAL quote_all_identifiers = off")
    conn.execute("SET LOCAL IntervalStyle = postgres")
    conn.execute("SET LOCAL DateStyle = 'ISO, MDY'")
    conn.execute("SET LOCAL TimeZone = 'UTC'")
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
    no inheritance, permanent tables, and the owner of the tables and of schema public. Condition
    11 adds: the columns, constraints and indexes against pinned digests (11.1 / 11.2); the
    ordering sequence's parameters, owner and ACL (11.3); the owner and EXECUTE ACL of every
    function a knowledge trigger calls (11.4); table and column ACLs, where a role other than the
    owner may hold only SELECT / INSERT, column INSERT, never PUBLIC or a grant option (11.5); and
    no other SECURITY DEFINER function, in any schema, run by a member of the owner (11.7).

    It does NOT detect: a renamed CHECK/FK constraint or a sequence RESTART/setval; wholesale
    re-owning of everything, which `verify()` cannot see because its expected owner is the owner of
    public.assessments; tampering that is undone before the next start-up; superuser edits of the
    catalogue; conditions 11.6, 11.8 and 11.9 (ADR 0022 Known gaps). Messages are fixed text.
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
    problems += _shape_problems(conn, int(major_row[0]) // 10000 if major_row else None)
    problems += _sequence_problems(conn, expected_owner)
    problems += _function_problems(conn, expected_owner, in_kb, tables)
    problems += _acl_problems(conn, tables)
    return problems


# Stands in for the digest of an aspect whose rows could not be serialised; not a hex digest, so
# it never equals a pin.
SHAPE_UNREADABLE = "unreadable"

# One query per aspect of ADR 0022 condition 11.1 / 11.2. The first column is the table name; the
# rest is serialised and hashed. Constraint and index rows carry no oid or name-only column, and
# the rows are sorted in Python, so the digest does not depend on catalogue order.
_SHAPE_QUERIES = {
    "columns": (
        "SELECT c.relname, a.attnum, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
        "a.attnotnull, a.attgenerated, a.attidentity, co.collname, "
        "pg_catalog.pg_get_expr(d.adbin, d.adrelid) "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
        "AND NOT a.attisdropped "
        "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
        "LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation "
        "WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[])"
    ),
    "constraints": (
        "SELECT c.relname, con.contype, con.convalidated, con.condeferrable, con.condeferred, "
        "con.connoinherit, con.conislocal, pg_catalog.pg_get_constraintdef(con.oid) "
        "FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
        "JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[])"
    ),
    "indexes": (
        "SELECT c.relname, i.indisvalid, i.indisready, i.indislive, i.indimmediate, "
        "pg_catalog.pg_get_indexdef(i.indexrelid) "
        "FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indrelid "
        "JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[])"
    ),
}


def _shape_digests(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """`<table>.<aspect>` -> SHA-256 hex of the sorted, JSON-serialised catalogue rows. A table
    with no rows for an aspect hashes the empty list. A row that cannot be serialised makes that
    key `SHAPE_UNREADABLE`, which is never compared with a pin. The caller has run `_harden`."""
    digests: dict[str, str] = {}
    unreadable: set[str] = set()
    for aspect in KNOWLEDGE_SHAPE_ASPECTS:
        grouped: dict[str, list[str]] = {t: [] for t in KNOWLEDGE_TABLES}
        for row in conn.execute(_SHAPE_QUERIES[aspect], (list(KNOWLEDGE_TABLES),)).fetchall():
            if str(row[0]) in grouped:
                try:
                    line = json.dumps(list(row[1:]), separators=(",", ":"))
                except (TypeError, ValueError):
                    unreadable.add(f"{row[0]}.{aspect}")
                    continue
                grouped[str(row[0])].append(line)
        for table, lines in grouped.items():
            joined = "\n".join(sorted(lines)).encode()
            digests[f"{table}.{aspect}"] = hashlib.sha256(joined).hexdigest()
    for key in unreadable:
        digests[key] = SHAPE_UNREADABLE
    return digests


def knowledge_shape_digests(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """The condition 11.1 / 11.2 digests of the live database. Read-only; for `kb_pins`."""
    _harden(conn)
    return _shape_digests(conn)


def _shape_problems(conn: psycopg.Connection[Any], major: int | None) -> list[str]:
    pins = KNOWLEDGE_SHAPE_SHA256.get(major, {}) if major is not None else {}
    problems: list[str] = []
    for key, digest in _shape_digests(conn).items():
        table, aspect = key.split(".")
        shape_pin = pins.get(key)
        if digest == SHAPE_UNREADABLE:
            problems.append(f"the {aspect} of {table} could not be read")
        elif shape_pin is None:
            problems.append(
                f"no pinned definition of the {aspect} of {table} on this PostgreSQL version"
            )
        elif digest != shape_pin:
            problems.append(f"the {aspect} of {table} do not match their pinned definition")
    return problems


def _sequence_problems(conn: psycopg.Connection[Any], expected_owner: str) -> list[str]:
    """Condition 11.3: parameters, owner and ACL of the ordering sequence (NULL relacl is the
    sequence default: owner only). The position of the sequence (RESTART, setval) is not read."""
    row = conn.execute(
        "SELECT pg_catalog.format_type(q.seqtypid, NULL), q.seqstart, q.seqincrement, q.seqmax, "
        "q.seqmin, q.seqcache, q.seqcycle, c.relpersistence, "
        f"c.relowner IS NOT DISTINCT FROM {expected_owner} AND {expected_owner} IS NOT NULL, "
        "NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(c.relacl) x "
        "WHERE x.grantee <> c.relowner) "
        "FROM pg_catalog.pg_sequence q JOIN pg_catalog.pg_class c ON c.oid = q.seqrelid "
        "JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "WHERE s.nspname = 'public' AND c.relname = %s",
        (KNOWLEDGE_SEQUENCE,),
    ).fetchone()
    if row is None:
        return ["the knowledge ordering sequence could not be read"]
    problems: list[str] = []
    if tuple(row[:8]) != KNOWLEDGE_SEQUENCE_PARAMETERS:
        problems.append("the knowledge ordering sequence does not have its expected parameters")
    if row[8] is not True:
        problems.append("the knowledge ordering sequence is not owned by the expected owner role")
    if row[9] is not True:
        problems.append("a role other than the owner holds a privilege on the ordering sequence")
    return problems


def _function_problems(
    conn: psycopg.Connection[Any], expected_owner: str, in_kb: str, tables: list[str]
) -> list[str]:
    """Condition 11.4: every function a knowledge trigger calls is owned by the expected owner and
    only the owner may EXECUTE it (a NULL proacl means PUBLIC may). Which function each trigger
    calls is the pairing check's job. Condition 11.7: no other SECURITY DEFINER function, in any
    schema, runs as a role that is a member of the owner (a superuser is a member of every role,
    so a superuser-owned one counts too; unknown counts)."""
    called = (
        "(SELECT t.tgfoid FROM pg_catalog.pg_trigger t "
        f"WHERE NOT t.tgisinternal AND t.tgrelid IN {in_kb})"
    )
    rows = conn.execute(
        "SELECT p.proname, "
        f"p.proowner IS NOT DISTINCT FROM {expected_owner} AND {expected_owner} IS NOT NULL, "
        "p.proacl IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(p.proacl) x "
        "WHERE x.grantee <> p.proowner) "
        f"FROM pg_catalog.pg_proc p WHERE p.oid IN {called}",
        (tables,),
    ).fetchall()
    problems: list[str] = []
    for proname, owned, only_owner in sorted(rows, key=lambda r: str(r[0])):
        name = str(proname) if str(proname) in KNOWLEDGE_FUNCTIONS.values() else "a function"
        if owned is not True:
            problems.append(f"{name} is not owned by the expected owner role")
        if only_owner is not True:
            problems.append(f"a role other than the owner may EXECUTE {name}")
    wrappers = conn.execute(
        "SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p WHERE p.prosecdef "
        f"AND pg_catalog.pg_has_role(p.proowner, {expected_owner}, %s) IS NOT FALSE "
        f"AND p.oid NOT IN {called}",
        ("MEMBER", tables),
    ).fetchone()
    if wrappers is None or int(wrappers[0]) != 0:
        problems.append("a SECURITY DEFINER function other than the guards runs as the owner role")
    return problems


_KB_TABLE_GRANTS = ("SELECT", "INSERT")
_KB_COLUMN_GRANTS = ("INSERT",)
_KB_COLUMN_INSERT_ONLY = "kb_status_events"
_KB_INSERT = "INSERT"


def _acl_problems(conn: psycopg.Connection[Any], tables: list[str]) -> list[str]:
    """Condition 11.5, role-independent: on each knowledge table no role but the owner holds a
    privilege other than SELECT or INSERT, PUBLIC holds nothing, no grant option is held, and
    column privileges are INSERT only; INSERT on kb_status_events is column-level only, and at
    most one role other than the owner may INSERT at all (table or column, across the three
    tables). Per-role checks are in `verify()` and `kb_privileges`."""
    rows = conn.execute(
        "SELECT c.relname, NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(c.relacl) x "
        "WHERE x.grantee <> c.relowner AND (x.grantee = 0 OR x.is_grantable "
        "OR x.privilege_type <> ALL(%s::text[]))), "
        "NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a, pg_catalog.aclexplode(a.attacl) x "
        "WHERE a.attrelid = c.oid AND x.grantee <> c.relowner AND (x.grantee = 0 "
        "OR x.is_grantable OR x.privilege_type <> ALL(%s::text[]))) "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[])",
        (list(_KB_TABLE_GRANTS), list(_KB_COLUMN_GRANTS), tables),
    ).fetchall()
    problems: list[str] = []
    for relname, table_ok, columns_ok in sorted(rows, key=lambda r: str(r[0])):
        if str(relname) not in KNOWLEDGE_TABLES:
            continue
        if table_ok is not True:
            problems.append(
                f"a role other than the owner holds more than SELECT or INSERT on {relname}"
            )
        if columns_ok is not True:
            problems.append(f"a column privilege other than INSERT is granted on {relname}")
    # At most one non-owner role holds INSERT directly (this does not check that it is the loader,
    # nor count members of that role), and on kb_status_events only through column grants (the
    # event_order and recorded_at columns are the database's). Grantees are oids, never printed.
    inserts = conn.execute(
        "SELECT c.relname, x.grantee, false FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace, "
        "pg_catalog.aclexplode(c.relacl) x "
        "WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[]) "
        "AND x.grantee <> c.relowner AND x.privilege_type = %s "
        "UNION ALL "
        "SELECT c.relname, x.grantee, true FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace s ON s.oid = c.relnamespace "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid, "
        "pg_catalog.aclexplode(a.attacl) x "
        "WHERE s.nspname = 'public' AND c.relname = ANY(%s::text[]) "
        "AND x.grantee <> c.relowner AND x.privilege_type = %s",
        (tables, _KB_INSERT, tables, _KB_INSERT),
    ).fetchall()
    inserts = [r for r in inserts if str(r[0]) in KNOWLEDGE_TABLES]
    if any(str(r[0]) == _KB_COLUMN_INSERT_ONLY and r[2] is not True for r in inserts):
        problems.append(
            f"a role other than the owner holds table-level INSERT on {_KB_COLUMN_INSERT_ONLY}"
        )
    if len({r[1] for r in inserts}) > 1:
        problems.append(
            "more than one role other than the owner may INSERT into the knowledge tables"
        )
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
    Migration(5, "a pending assessment may be reassigned, append-only", _V5),
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
            # The post-checks below read the catalogue; the migrations above keep the session's
            # own settings (their checksums were taken under them).
            _harden(conn)
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
