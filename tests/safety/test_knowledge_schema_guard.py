"""Properties of migration 4 (the knowledge tables, ADR 0022 decisions 3, 10, 11), pinned on the SQL
text. Database-free. Every property is one assertion helper that takes the SQL; the real migration
must pass it, and a mutated copy of the SQL (built in the test, `src` is never edited) must fail it,
so each helper is shown to have teeth on every run."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from divesafe.services.migrations import (
    KNOWLEDGE_FUNCTIONS,
    KNOWLEDGE_MIGRATION,
    KNOWLEDGE_TABLES,
    KNOWLEDGE_TRIGGERS,
    MIGRATIONS,
    REVIEWED_GUARD_MIGRATIONS,
    Migration,
)

pytestmark = pytest.mark.safety

SQL = MIGRATIONS[KNOWLEDGE_MIGRATION - 1].sql
PINNED_CHECKSUM = "c95979fbf982c54ef0f05528d9549944d49a10c0210690854509c9042a36df8e"

INSERT_FUNCTIONS = {
    "kb_documents": "kb_documents_insert",
    "kb_chunks": "kb_chunks_insert",
    "kb_status_events": "kb_status_events_insert",
}
ALL_FUNCTIONS = ("kb_no_update_delete", "kb_no_truncate", *INSERT_FUNCTIONS.values())

# Independent of KNOWLEDGE_TRIGGERS: name -> (timing, level, table, function).
EXPECTED_TRIGGERS: dict[str, tuple[str, str, str, str]] = {}
for _t, _fn in INSERT_FUNCTIONS.items():
    EXPECTED_TRIGGERS[f"{_t}_insert_guard"] = ("BEFORE INSERT", "ROW", _t, _fn)
    EXPECTED_TRIGGERS[f"{_t}_append_only_guard"] = (
        "BEFORE UPDATE OR DELETE",
        "ROW",
        _t,
        "kb_no_update_delete",
    )
    EXPECTED_TRIGGERS[f"{_t}_no_truncate_guard"] = (
        "BEFORE TRUNCATE",
        "STATEMENT",
        _t,
        "kb_no_truncate",
    )

_FUNCTION = re.compile(
    r"CREATE FUNCTION public\.(\w+)\(\) RETURNS trigger\n(.*?)AS \$\$(.*?)\$\$;", re.DOTALL
)
_TRIGGER = re.compile(
    r"CREATE TRIGGER (\w+)\s+(BEFORE [A-Z ]+?) ON public\.(\w+)\s+FOR EACH (ROW|STATEMENT) "
    r"EXECUTE FUNCTION public\.(\w+)\(\);"
)
_BUILTINS = (
    "current_setting",
    "pg_advisory_xact_lock",
    "hashtext",
    "encode",
    "sha256",
    "convert_to",
    "clock_timestamp",
    "nextval",
)


def functions(sql: str) -> dict[str, tuple[str, str]]:
    found = {m.group(1): (m.group(2), m.group(3)) for m in _FUNCTION.finditer(sql)}
    assert sorted(found) == sorted(ALL_FUNCTIONS), "the five trigger functions, all in public"
    assert len(re.findall(r"\bCREATE (?:OR REPLACE )?FUNCTION\b", sql, re.I)) == 5
    return found


def table_block(sql: str, table: str) -> str:
    m = re.search(rf"CREATE TABLE public\.{table} \((.*?)\n\);", sql, re.DOTALL)
    assert m is not None, f"CREATE TABLE public.{table}"
    return m.group(1)


# --- assertion helpers (each is mutation-checked below) ----------------------------------------


def check_definer(sql: str) -> None:
    for name, (header, _) in functions(sql).items():
        assert header.count("SECURITY DEFINER") == 1, name
        assert "SECURITY INVOKER" not in header, name


def check_search_path(sql: str) -> None:
    for name, (header, _) in functions(sql).items():
        assert header.count("search_path") == 1, name
        assert re.search(r"SET search_path = pg_catalog, pg_temp\s*$", header), name


def check_volatile(sql: str) -> None:
    for name, (header, _) in functions(sql).items():
        assert re.search(r"\bVOLATILE\b", header), name
        assert not re.search(r"\b(STABLE|IMMUTABLE)\b", header, re.I), name


def check_qualified(sql: str) -> None:
    for kind in ("TABLE", "SEQUENCE", "FUNCTION"):
        assert not re.search(rf"CREATE (?:OR REPLACE )?{kind} (?!public\.)", sql), kind
    assert not re.search(r"REFERENCES (?!public\.)", sql)
    assert not re.search(r"(?:FROM|JOIN|INTO|UPDATE|ON|ON SEQUENCE|TABLE) (?!public\.)kb_", sql)
    assert not re.search(r"(?:FROM|INTO|JOIN) (?!public\.)\w*seq\b", sql)
    # a trigger name is the only legitimate unqualified kb_ word, and only after TRIGGER
    stripped = re.sub(r"(?:CREATE TRIGGER|ENABLE ALWAYS TRIGGER) kb_\w+", "TRIGGER x", sql)
    stripped = re.sub(r"CONSTRAINT kb_\w+", "", stripped)
    assert not re.search(r"(?<!public\.)\bkb_\w+", stripped), "unqualified kb_ name"
    for _, body in functions(sql).values():
        for fn in _BUILTINS:
            assert not re.search(rf"(?<!pg_catalog\.)\b{fn}\(", body), fn
        assert "public." in body or "RAISE" in body


def check_isolation(sql: str) -> None:
    refusal = re.compile(
        r"IF pg_catalog\.current_setting\('transaction_isolation'\) <> 'read committed' THEN\s+"
        r"RAISE EXCEPTION '[^']+';\s+END IF;"
    )
    for fn in INSERT_FUNCTIONS.values():
        body = functions(sql)[fn][1]
        m = refusal.search(body)
        assert m is not None, fn
        # first thing the function does, before it takes a lock or reads anything
        assert body[: m.start()].count("BEGIN") == 1 and body[: m.start()].strip().endswith(
            "BEGIN"
        ), fn
        assert m.start() < body.index("pg_advisory_xact_lock"), fn


def check_plain_guards_always_raise(sql: str) -> None:
    fns = functions(sql)
    for fn in ("kb_no_update_delete", "kb_no_truncate"):
        body = re.sub(r"\s+", " ", fns[fn][1]).strip()
        assert re.fullmatch(r"BEGIN RAISE EXCEPTION '[^']+'; END;", body), fn


def check_revokes(sql: str) -> None:
    for fn in ALL_FUNCTIONS:
        assert f"REVOKE EXECUTE ON FUNCTION public.{fn}() FROM PUBLIC;" in sql, fn
    assert not re.search(r"GRANT\b[^;]*\bON FUNCTION", sql, re.I)
    assert "REVOKE ALL ON public.kb_documents, public.kb_chunks, public.kb_status_events" in sql
    assert "REVOKE ALL ON SEQUENCE public.kb_status_event_order_seq FROM PUBLIC;" in sql
    assert not re.search(r"\bGRANT\b", sql, re.I), "migration 4 grants nothing; db_init does"


def check_triggers(sql: str) -> None:
    found = {
        m.group(1): (m.group(2), m.group(4), m.group(3), m.group(5)) for m in _TRIGGER.finditer(sql)
    }
    assert found == EXPECTED_TRIGGERS
    assert len(re.findall(r"CREATE TRIGGER", sql)) == len(EXPECTED_TRIGGERS) == 9
    assert set(found) == set(KNOWLEDGE_TRIGGERS)
    assert {k: v[3] for k, v in found.items()} == KNOWLEDGE_FUNCTIONS
    for table in KNOWLEDGE_TABLES:
        truncate = [n for n, v in found.items() if v[0] == "BEFORE TRUNCATE" and v[2] == table]
        assert len(truncate) == 1, table


def check_enable_always(sql: str) -> None:
    statements = re.findall(r"ALTER TABLE public\.(\w+) ENABLE ALWAYS TRIGGER (\w+);", sql)
    assert len(statements) == len(re.findall(r"CREATE TRIGGER", sql)) == 9
    assert sql.count("ENABLE ALWAYS") == 9
    assert {name: table for table, name in statements} == {
        name: spec[2] for name, spec in EXPECTED_TRIGGERS.items()
    }
    assert not re.search(r"\b(DISABLE|ENABLE REPLICA)\b", sql, re.I)
    assert not re.search(r"ENABLE TRIGGER", sql, re.I)


def check_no_drop_trigger_if_exists(sql: str) -> None:
    lowered = " ".join(sql.lower().split())
    assert "drop trigger" not in lowered
    assert "create or replace" not in lowered
    assert not re.search(r"\bdrop\b", lowered)


def check_no_shadowing(sql: str) -> None:
    assert not re.search(r"\bpg_temp\.", sql)
    assert not re.search(r"\bCREATE\s+(?:GLOBAL\s+|LOCAL\s+)?TEMP(?:ORARY)?\b", sql, re.I)
    assert sql.count("pg_temp") == 5, "pg_temp appears only in the five pinned search paths"
    assert not re.search(r"search_path\s*(?:=|TO)\s*[^\n]*\bpublic\b", sql)
    assert not re.search(r"set_config\(|SET LOCAL|SET SESSION", sql, re.I)


def check_time_zone(sql: str) -> None:
    assert "'+08'" not in sql and "+08" not in sql
    zones = re.findall(r"AT TIME ZONE (\S+)", sql)
    assert zones, "the approval date needs an explicit zone"
    assert all(z.startswith("'Asia/Kuala_Lumpur'") for z in zones)
    assert not re.search(r"\b(current_date|localtimestamp|now\(\))", sql, re.I)


def check_hash_checked(sql: str) -> None:
    block = table_block(sql, "kb_status_events")
    columns = [line.strip() for line in block.splitlines()]
    column = [c for c in columns if c.startswith("hash_checked")]
    assert len(column) == 1 and re.fullmatch(r"hash_checked\s+boolean NOT NULL,", column[0])
    assert "CHECK (status = 'APPROVED' OR hash_checked = false)," in columns
    assert not re.search(r"\bDEFAULT\b", block, re.I), "no default on any event column"
    assert not re.search(r"GENERATED|IDENTITY|SERIAL", block, re.I)
    assert "event_order   bigint NOT NULL UNIQUE," in columns
    assert "recorded_at   timestamptz NOT NULL," in columns


def check_distinct_sqlstate(sql: str) -> None:
    _, body = functions(sql)["kb_status_events_insert"]
    assert sql.count("ERRCODE") == 1
    assert re.search(
        r"IF previous = 'WITHDRAWN' AND NEW\.status = 'WITHDRAWN' THEN\s+"
        r"RAISE EXCEPTION '[^']+' USING ERRCODE = 'KB001';",
        body,
    )
    assert "KB001" not in sql.replace("ERRCODE = 'KB001'", "")


def check_status_trigger_order(sql: str) -> None:
    _, body = functions(sql)["kb_status_events_insert"]
    lock = body.index("pg_advisory_xact_lock")
    exists = body.index("a status event needs an existing document version")
    allowed = body.index("this status change is not allowed")
    stamp = body.index("clock_timestamp()")
    sequence = body.index("nextval(")
    assert "a version without chunks cannot be approved" in body, "approval check missing"
    empty = body.index("a version without chunks cannot be approved")
    assert lock < exists < allowed < empty < stamp < sequence
    assert body.count("nextval(") == 1 and body.count("clock_timestamp()") == 1
    assert "NEW.event_order := pg_catalog.nextval('public.kb_status_event_order_seq');" in body
    assert "NEW.recorded_at := stamp;" in body
    assert body.index("12 months") < sequence
    assert "(previous IS NULL AND NEW.status = 'DRAFT')" in body
    assert "(previous = 'DRAFT' AND NEW.status IN ('APPROVED', 'WITHDRAWN'))" in body
    assert "(previous = 'APPROVED' AND NEW.status = 'WITHDRAWN')" in body
    # NULL-safe: a NULL comparison must fail closed, never fall through to "allowed".
    assert re.search(r"IF NOT COALESCE\(\(\s*\(previous IS NULL", body)
    assert re.search(r"\)\s*,\s*false\)\s*THEN\s*RAISE EXCEPTION 'this status change", body)
    assert (
        "IF NOT COALESCE(effective <= (approved_on + interval '12 months')::date, false) THEN"
        in body
    )
    assert "IF NOT COALESCE(review_by >= approved_on, false) THEN" in body
    assert CHUNKS_REQUIRED.search(body), "the approval-needs-chunks check is missing or weakened"
    assert body.count("COALESCE(") == 4
    assert ", true)" not in body.replace("true INTO", "")


CHUNKS_REQUIRED = re.compile(
    r"IF NEW\.status = 'APPROVED' AND NOT COALESCE\(\(\s*"
    r"SELECT true FROM public\.kb_chunks c\s*"
    r"WHERE c\.document_id = NEW\.document_id AND c\.version = NEW\.version LIMIT 1\s*"
    r"\), false\) THEN\s*RAISE EXCEPTION 'a version without chunks cannot be approved';\s*END IF;"
)


def drop_chunks_required(segment: str) -> str:
    return CHUNKS_REQUIRED.sub("", segment)


LOCK_CALL = (
    "PERFORM pg_catalog.pg_advisory_xact_lock(1220001, pg_catalog.hashtext(NEW.document_id));"
)


def check_advisory_lock(sql: str) -> None:
    """All three insert triggers serialise per document on one transaction-scoped lock, taken
    before they read anything, so two sessions cannot both pass the same check."""
    assert sql.count("pg_advisory_") == 3 and sql.count(LOCK_CALL) == 3
    for fn in INSERT_FUNCTIONS.values():
        body = functions(sql)[fn][1]
        assert body.count(LOCK_CALL) == 1, fn
        reads = [body.index(w) for w in ("SELECT", "EXISTS") if w in body]
        assert body.index(LOCK_CALL) < min(reads), fn


CHUNK_FREEZE = re.compile(
    r"IF EXISTS \(\s*SELECT 1 FROM public\.kb_status_events e\s*"
    r"WHERE e\.document_id = NEW\.document_id AND e\.version = NEW\.version\s*\) THEN\s*"
    r"RAISE EXCEPTION 'chunks cannot be added to a version that has a status event';\s*END IF;"
)


def check_chunks_freeze_at_the_first_status_event(sql: str) -> None:
    body = functions(sql)["kb_chunks_insert"][1]
    found = CHUNK_FREEZE.search(body)
    assert found is not None, "the freeze check is missing or weakened"
    assert body.index("pg_advisory_xact_lock") < found.start() < body.index("IS DISTINCT FROM")
    assert "ERRCODE" not in body, "a plain P0001; KB001 belongs to the withdrawn refusal only"
    assert body.count("RAISE EXCEPTION") == 3  # isolation, freeze, hash


def check_review_by_bound(sql: str) -> None:
    block = table_block(sql, "kb_documents")
    assert "CHECK (effective_date <= review_by_date)," in block
    assert "CHECK (review_by_date <= (effective_date + interval '12 months')::date)," in block
    assert block.count("interval") == 1


def drop_chunk_freeze(segment: str) -> str:
    return CHUNK_FREEZE.sub("", segment)


def check_pinned(sql: str) -> None:
    migration = Migration(KNOWLEDGE_MIGRATION, MIGRATIONS[3].name, sql)
    assert migration.checksum == PINNED_CHECKSUM
    assert REVIEWED_GUARD_MIGRATIONS[KNOWLEDGE_MIGRATION] == migration.checksum


# --- the real migration passes every helper ------------------------------------------------------

HELPERS: dict[str, Callable[[str], None]] = {
    "definer": check_definer,
    "search_path": check_search_path,
    "volatile": check_volatile,
    "qualified": check_qualified,
    "isolation": check_isolation,
    "plain_guards": check_plain_guards_always_raise,
    "revokes": check_revokes,
    "triggers": check_triggers,
    "enable_always": check_enable_always,
    "no_drop_trigger_if_exists": check_no_drop_trigger_if_exists,
    "no_shadowing": check_no_shadowing,
    "time_zone": check_time_zone,
    "hash_checked": check_hash_checked,
    "sqlstate": check_distinct_sqlstate,
    "status_order": check_status_trigger_order,
    "advisory_lock": check_advisory_lock,
    "chunk_freeze": check_chunks_freeze_at_the_first_status_event,
    "review_by_bound": check_review_by_bound,
    "pinned": check_pinned,
}


@pytest.mark.parametrize("name", HELPERS)
def test_migration_four_has_the_property(name: str) -> None:
    HELPERS[name](SQL)


def test_migration_four_is_the_fourth_and_pinned() -> None:
    assert KNOWLEDGE_MIGRATION == 4 and MIGRATIONS[3].version == 4
    assert MIGRATIONS[3].checksum == PINNED_CHECKSUM


def test_exactly_the_three_knowledge_tables_are_created() -> None:
    assert re.findall(r"CREATE TABLE (\S+) \(", SQL) == [f"public.{t}" for t in KNOWLEDGE_TABLES]
    assert tuple(KNOWLEDGE_TABLES) == ("kb_documents", "kb_chunks", "kb_status_events")


def test_the_migration_does_not_touch_the_assessments_guard_or_replication_role() -> None:
    lowered = SQL.lower()
    for word in ("assessments", "session_replication_role", "disable trigger", "security invoker"):
        assert word not in lowered


# --- mutations: break a COPY of the SQL once; the named helper must fail --------------------------


def replace_once(old: str, new: str, nth: int = 0) -> Callable[[str], str]:
    def edit(sql: str) -> str:
        positions = [m.start() for m in re.finditer(re.escape(old), sql)]
        assert len(positions) > nth, f"mutation target not found: {old!r}"
        at = positions[nth]
        return sql[:at] + new + sql[at + len(old) :]

    return edit


def replace_all(old: str, new: str) -> Callable[[str], str]:
    def edit(sql: str) -> str:
        assert old in sql, f"mutation target not found: {old!r}"
        return sql.replace(old, new)

    return edit


def in_function(fn: str, edit: Callable[[str], str]) -> Callable[[str], str]:
    def apply(sql: str) -> str:
        start = sql.index(f"CREATE FUNCTION public.{fn}()")
        end = sql.index("$$;", sql.index("AS $$", start)) + 3
        segment = sql[start:end]
        mutated = edit(segment)
        assert mutated != segment, f"mutation changed nothing in {fn}"
        return sql[:start] + mutated + sql[end:]

    return apply


ISOLATION_BLOCK = re.compile(
    r"    IF pg_catalog\.current_setting\('transaction_isolation'\) <> 'read committed' THEN\n"
    r"        RAISE EXCEPTION '[^']+';\n    END IF;\n"
)


def drop_isolation(segment: str) -> str:
    return ISOLATION_BLOCK.sub("", segment)


def move_nextval_before_validation(segment: str) -> str:
    line = "    NEW.event_order := pg_catalog.nextval('public.kb_status_event_order_seq');\n"
    assert line in segment
    segment = segment.replace(line, "")
    lock = "pg_catalog.hashtext(NEW.document_id));\n"
    return segment.replace(lock, lock + line, 1)


def move_clock_before_validation(segment: str) -> str:
    line = "    stamp := pg_catalog.clock_timestamp();\n"
    assert line in segment
    segment = segment.replace(line, "")
    lock = "pg_catalog.hashtext(NEW.document_id));\n"
    return segment.replace(lock, lock + line, 1)


def remove_transition_coalesce(sql: str) -> str:
    sql = replace_once("IF NOT COALESCE((", "IF NOT ((")(sql)
    return replace_once(
        "    ), false) THEN\n        RAISE EXCEPTION 'this status",
        "    )) THEN\n        RAISE EXCEPTION 'this status",
    )(sql)


MUTATIONS: list[tuple[str, str, Callable[[str], str]]] = [
    # SECURITY DEFINER
    ("definer: drop it from the first function", "definer", replace_once("SECURITY DEFINER ", "")),
    (
        "definer: drop it from the last function",
        "definer",
        replace_once("SECURITY DEFINER ", "", nth=4),
    ),
    (
        "definer: SECURITY INVOKER",
        "definer",
        replace_once("SECURITY DEFINER", "SECURITY DEFINER SECURITY INVOKER"),
    ),
    # search_path
    (
        "search_path: pg_temp first",
        "search_path",
        replace_once("pg_catalog, pg_temp", "pg_temp, pg_catalog"),
    ),
    (
        "search_path: public added",
        "search_path",
        replace_once("pg_catalog, pg_temp", "pg_catalog, public, pg_temp", nth=2),
    ),
    (
        "search_path: pg_temp dropped",
        "search_path",
        replace_once("pg_catalog, pg_temp", "pg_catalog"),
    ),
    (
        "search_path: pg_temp dropped from the last function",
        "search_path",
        replace_once("pg_catalog, pg_temp", "pg_catalog", nth=4),
    ),
    (
        "search_path: public instead of pg_temp",
        "search_path",
        replace_once("pg_catalog, pg_temp", "pg_catalog, public"),
    ),
    (
        "search_path: no SET at all",
        "search_path",
        replace_once(" SET search_path = pg_catalog, pg_temp", ""),
    ),
    # VOLATILE
    ("volatile: STABLE", "volatile", replace_once("VOLATILE", "STABLE")),
    (
        "volatile: IMMUTABLE in the status function",
        "volatile",
        replace_once("VOLATILE", "IMMUTABLE", nth=4),
    ),
    ("volatile: keyword dropped", "volatile", replace_once("VOLATILE ", "")),
    # schema qualification
    (
        "qualified: table in the version trigger",
        "qualified",
        replace_once("FROM public.kb_documents d", "FROM kb_documents d"),
    ),
    (
        "qualified: sequence in nextval",
        "qualified",
        replace_once(
            "nextval('public.kb_status_event_order_seq')", "nextval('kb_status_event_order_seq')"
        ),
    ),
    (
        "qualified: nextval via search path",
        "qualified",
        replace_once("pg_catalog.nextval", "nextval"),
    ),
    (
        "qualified: hashtext via search path",
        "qualified",
        replace_once("pg_catalog.hashtext", "hashtext"),
    ),
    (
        "qualified: lock via search path",
        "qualified",
        replace_once("pg_catalog.pg_advisory_xact_lock", "pg_advisory_xact_lock"),
    ),
    ("qualified: sha256 via search path", "qualified", replace_once("pg_catalog.sha256", "sha256")),
    (
        "qualified: clock_timestamp via search path",
        "qualified",
        replace_once("pg_catalog.clock_timestamp", "clock_timestamp"),
    ),
    (
        "qualified: current_setting via search path",
        "qualified",
        replace_once("pg_catalog.current_setting", "current_setting"),
    ),
    (
        "qualified: status table in the status trigger",
        "qualified",
        replace_once("FROM public.kb_status_events e", "FROM kb_status_events e"),
    ),
    (
        "qualified: function in CREATE TRIGGER",
        "qualified",
        replace_once(
            "EXECUTE FUNCTION public.kb_no_truncate()", "EXECUTE FUNCTION kb_no_truncate()"
        ),
    ),
    (
        "qualified: foreign key target",
        "qualified",
        replace_once("REFERENCES public.kb_documents", "REFERENCES kb_documents"),
    ),
    (
        "qualified: table name in CREATE TABLE",
        "qualified",
        replace_once("CREATE TABLE public.kb_chunks", "CREATE TABLE kb_chunks"),
    ),
    (
        "qualified: trigger target table",
        "qualified",
        replace_once("ON public.kb_chunks\n    FOR EACH ROW", "ON kb_chunks\n    FOR EACH ROW"),
    ),
    # isolation refusal
    (
        "isolation: removed from kb_documents_insert",
        "isolation",
        in_function("kb_documents_insert", drop_isolation),
    ),
    (
        "isolation: removed from kb_chunks_insert",
        "isolation",
        in_function("kb_chunks_insert", drop_isolation),
    ),
    (
        "isolation: removed from kb_status_events_insert",
        "isolation",
        in_function("kb_status_events_insert", drop_isolation),
    ),
    (
        "isolation: only repeatable read refused",
        "isolation",
        replace_once("<> 'read committed'", "= 'repeatable read'"),
    ),
    (
        "isolation: check after the lock",
        "isolation",
        in_function(
            "kb_chunks_insert",
            lambda s: drop_isolation(s).replace(
                "    IF NEW.content_sha256",
                "    IF pg_catalog.current_setting('transaction_isolation')"
                " <> 'read committed' THEN\n"
                "        RAISE EXCEPTION 'x';\n    END IF;\n    IF NEW.content_sha256",
            ),
        ),
    ),
    # the unconditional guards
    (
        "plain_guards: truncate guard made conditional",
        "plain_guards",
        replace_once(
            "RAISE EXCEPTION 'knowledge tables are never truncated';",
            "IF current_user <> 'x' THEN RAISE EXCEPTION 'knowledge tables are never truncated'; "
            "END IF; RETURN NULL;",
        ),
    ),
    (
        "plain_guards: update/delete guard returns",
        "plain_guards",
        replace_once("RAISE EXCEPTION 'knowledge tables are append-only';", "RETURN OLD;"),
    ),
    # REVOKE EXECUTE
    *[
        (
            f"revokes: no REVOKE for {fn}",
            "revokes",
            replace_all(f"REVOKE EXECUTE ON FUNCTION public.{fn}() FROM PUBLIC;\n", ""),
        )
        for fn in ALL_FUNCTIONS
    ],
    (
        "revokes: a function granted to a role",
        "revokes",
        replace_once(
            "\nCREATE TRIGGER",
            "\nGRANT EXECUTE ON FUNCTION public.kb_no_truncate() TO PUBLIC;\nCREATE TRIGGER",
        ),
    ),
    (
        "revokes: table not revoked from PUBLIC",
        "revokes",
        replace_all(
            "REVOKE ALL ON public.kb_documents, public.kb_chunks, "
            "public.kb_status_events FROM PUBLIC;\n",
            "",
        ),
    ),
    (
        "revokes: sequence not revoked from PUBLIC",
        "revokes",
        replace_all("REVOKE ALL ON SEQUENCE public.kb_status_event_order_seq FROM PUBLIC;\n", ""),
    ),
    # triggers and TRUNCATE guards
    *[
        (
            f"triggers: {name} not created",
            "triggers",
            replace_all(
                re.search(rf"CREATE TRIGGER {name}\n.*?;\n", SQL, re.DOTALL).group(0),  # type: ignore[union-attr]
                "",
            ),
        )
        for name in KNOWLEDGE_TRIGGERS
    ],
    (
        "triggers: truncate guard fires per row",
        "triggers",
        replace_once("FOR EACH STATEMENT", "FOR EACH ROW"),
    ),
    (
        "triggers: truncate guard calls the wrong function",
        "triggers",
        replace_once(
            "EXECUTE FUNCTION public.kb_no_truncate()",
            "EXECUTE FUNCTION public.kb_no_update_delete()",
        ),
    ),
    (
        "triggers: guard fires AFTER",
        "triggers",
        replace_once("BEFORE UPDATE OR DELETE", "AFTER UPDATE OR DELETE"),
    ),
    (
        "triggers: update-only guard",
        "triggers",
        replace_once("BEFORE UPDATE OR DELETE", "BEFORE UPDATE"),
    ),
    (
        "triggers: insert guard on the wrong table",
        "triggers",
        replace_once("BEFORE INSERT ON public.kb_chunks", "BEFORE INSERT ON public.kb_documents"),
    ),
    # ENABLE ALWAYS
    *[
        (
            f"enable_always: {name} left at the default",
            "enable_always",
            replace_all(
                re.search(rf"ALTER TABLE public\.\w+ ENABLE ALWAYS TRIGGER {name};\n", SQL).group(
                    0
                ),  # type: ignore[union-attr]
                "",
            ),
        )
        for name in KNOWLEDGE_TRIGGERS
    ],
    (
        "enable_always: replica only",
        "enable_always",
        replace_once(
            "ENABLE ALWAYS TRIGGER kb_chunks_insert_guard",
            "ENABLE REPLICA TRIGGER kb_chunks_insert_guard",
        ),
    ),
    (
        "enable_always: a trigger disabled",
        "enable_always",
        lambda s: s + "ALTER TABLE public.kb_chunks DISABLE TRIGGER kb_chunks_insert_guard;\n",
    ),
    (
        "enable_always: duplicate statement hides a missing one",
        "enable_always",
        replace_once(
            "ALTER TABLE public.kb_chunks ENABLE ALWAYS TRIGGER kb_chunks_no_truncate_guard;",
            "ALTER TABLE public.kb_chunks ENABLE ALWAYS TRIGGER kb_chunks_insert_guard;",
        ),
    ),
    # DROP TRIGGER IF EXISTS
    (
        "no_drop_trigger_if_exists: appended",
        "no_drop_trigger_if_exists",
        lambda s: s + "DROP TRIGGER IF EXISTS kb_chunks_insert_guard ON public.kb_chunks;\n",
    ),
    (
        "no_drop_trigger_if_exists: before the create",
        "no_drop_trigger_if_exists",
        replace_once(
            "CREATE TRIGGER kb_documents_insert_guard",
            "DROP TRIGGER IF EXISTS kb_documents_insert_guard ON public.kb_documents;\n"
            "CREATE TRIGGER kb_documents_insert_guard",
        ),
    ),
    (
        "no_drop_trigger_if_exists: CREATE OR REPLACE",
        "no_drop_trigger_if_exists",
        replace_once("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"),
    ),
    # shadowing
    (
        "no_shadowing: pg_temp-qualified table",
        "no_shadowing",
        lambda s: s + "SELECT 1 FROM pg_temp.kb_documents;\n",
    ),
    (
        "no_shadowing: temp table created",
        "no_shadowing",
        lambda s: s + "CREATE TEMP TABLE kb_documents (x int);\n",
    ),
    (
        "no_shadowing: public on the path",
        "no_shadowing",
        replace_once(
            "SET search_path = pg_catalog, pg_temp", "SET search_path = pg_catalog, public, pg_temp"
        ),
    ),
    (
        "no_shadowing: set_config in a body",
        "no_shadowing",
        replace_once(
            "BEGIN\n    RAISE EXCEPTION 'knowledge tables are append-only';",
            "BEGIN\n    PERFORM set_config('search_path', 'public', true);\n"
            "    RAISE EXCEPTION 'knowledge tables are append-only';",
        ),
    ),
    # time zone
    ("time_zone: '+08' POSIX sign flip", "time_zone", replace_once("'Asia/Kuala_Lumpur'", "'+08'")),
    ("time_zone: UTC date", "time_zone", replace_once("'Asia/Kuala_Lumpur'", "'UTC'")),
    (
        "time_zone: zone removed",
        "time_zone",
        replace_once("(stamp AT TIME ZONE 'Asia/Kuala_Lumpur')::date", "(stamp)::date"),
    ),
    (
        "time_zone: database current_date",
        "time_zone",
        replace_once("(stamp AT TIME ZONE 'Asia/Kuala_Lumpur')::date", "current_date"),
    ),
    # hash_checked
    (
        "hash_checked: DEFAULT false",
        "hash_checked",
        replace_once(
            "hash_checked  boolean NOT NULL,", "hash_checked  boolean NOT NULL DEFAULT false,"
        ),
    ),
    (
        "hash_checked: nullable",
        "hash_checked",
        replace_once("hash_checked  boolean NOT NULL,", "hash_checked  boolean,"),
    ),
    (
        "hash_checked: CHECK dropped",
        "hash_checked",
        replace_once("    CHECK (status = 'APPROVED' OR hash_checked = false),\n", ""),
    ),
    (
        "hash_checked: CHECK weakened",
        "hash_checked",
        replace_once("hash_checked = false)", "hash_checked IS NOT NULL)"),
    ),
    (
        "hash_checked: CHECK inverted",
        "hash_checked",
        replace_once("hash_checked = false)", "hash_checked = true)"),
    ),
    (
        "hash_checked: recorded_at default",
        "hash_checked",
        replace_once(
            "recorded_at   timestamptz NOT NULL,",
            "recorded_at   timestamptz NOT NULL DEFAULT clock_timestamp(),",
        ),
    ),
    (
        "hash_checked: identity ordering key",
        "hash_checked",
        replace_once(
            "event_order   bigint NOT NULL UNIQUE,",
            "event_order   bigint GENERATED ALWAYS AS IDENTITY UNIQUE,",
        ),
    ),
    # distinct SQLSTATE
    ("sqlstate: removed", "sqlstate", replace_once(" USING ERRCODE = 'KB001'", "")),
    (
        "sqlstate: also used by another refusal",
        "sqlstate",
        replace_once(
            "RAISE EXCEPTION 'this status change is not allowed';",
            "RAISE EXCEPTION 'this status change is not allowed' USING ERRCODE = 'KB001';",
        ),
    ),
    (
        "sqlstate: default P0001 for the repeat",
        "sqlstate",
        replace_once("USING ERRCODE = 'KB001'", "USING ERRCODE = 'P0001'"),
    ),
    # status trigger ordering and rules
    (
        "status_order: nextval before validation",
        "status_order",
        in_function("kb_status_events_insert", move_nextval_before_validation),
    ),
    (
        "status_order: clock before validation",
        "status_order",
        in_function("kb_status_events_insert", move_clock_before_validation),
    ),
    (
        "status_order: first event may be APPROVED",
        "status_order",
        replace_once(
            "(previous IS NULL AND NEW.status = 'DRAFT')",
            "(previous IS NULL AND NEW.status IN ('DRAFT', 'APPROVED'))",
        ),
    ),
    (
        "status_order: withdrawn can return to approved",
        "status_order",
        replace_once(
            "(previous = 'APPROVED' AND NEW.status = 'WITHDRAWN')",
            "(previous = 'WITHDRAWN' AND NEW.status = 'APPROVED')",
        ),
    ),
    ("status_order: 24 months", "status_order", replace_all("'12 months'", "'24 months'")),
    (
        "status_order: review-by check dropped",
        "status_order",
        replace_once("IF NOT COALESCE(review_by >= approved_on, false) THEN", "IF false THEN"),
    ),
    (
        "status_order: transition check loses COALESCE (NULL-unsafe)",
        "status_order",
        remove_transition_coalesce,
    ),
    # an approval needs at least one chunk of that very version (owner decision 2026-10-22)
    (
        "status_order: approval without chunks allowed (check removed)",
        "status_order",
        in_function("kb_status_events_insert", drop_chunks_required),
    ),
    (
        "status_order: chunks check after the clock and nextval",
        "status_order",
        in_function(
            "kb_status_events_insert",
            lambda seg: (
                lambda block, rest: rest.replace(
                    "    NEW.event_order :=", "    " + block + "\n    NEW.event_order :=", 1
                )
            )(CHUNKS_REQUIRED.search(seg).group(0), drop_chunks_required(seg)),  # type: ignore[union-attr]
        ),
    ),
    (
        "status_order: chunks check after the clock read (before the approval date checks only)",
        "status_order",
        in_function(
            "kb_status_events_insert",
            lambda seg: (
                lambda block, rest: rest.replace(
                    "    IF NEW.status = 'APPROVED' THEN\n        approved_on",
                    "    " + block + "\n    IF NEW.status = 'APPROVED' THEN\n        approved_on",
                    1,
                )
            )(CHUNKS_REQUIRED.search(seg).group(0), drop_chunks_required(seg)),  # type: ignore[union-attr]
        ),
    ),
    (
        "status_order: chunks check before the transition check",
        "status_order",
        in_function(
            "kb_status_events_insert",
            lambda seg: (
                lambda block, rest: rest.replace(
                    "    IF NOT COALESCE((\n        (previous IS NULL",
                    "    " + block + "\n    IF NOT COALESCE((\n        (previous IS NULL",
                    1,
                )
            )(CHUNKS_REQUIRED.search(seg).group(0), drop_chunks_required(seg)),  # type: ignore[union-attr]
        ),
    ),
    (
        "status_order: chunks of any version count",
        "status_order",
        replace_once(
            "WHERE c.document_id = NEW.document_id AND c.version = NEW.version LIMIT 1",
            "WHERE c.document_id = NEW.document_id LIMIT 1",
        ),
    ),
    (
        "status_order: chunks check loses COALESCE (NULL-unsafe)",
        "status_order",
        replace_once(
            "LIMIT 1\n    ), false) THEN\n        RAISE EXCEPTION 'a version without",
            "LIMIT 1\n    )) THEN\n        RAISE EXCEPTION 'a version without",
        ),
    ),
    (
        "status_order: chunks COALESCE defaults to true",
        "status_order",
        replace_once(
            "LIMIT 1\n    ), false) THEN\n        RAISE EXCEPTION 'a version without",
            "LIMIT 1\n    ), true) THEN\n        RAISE EXCEPTION 'a version without",
        ),
    ),
    (
        "status_order: chunks check applies to every status",
        "status_order",
        replace_once("IF NEW.status = 'APPROVED' AND NOT COALESCE((", "IF NOT COALESCE(("),
    ),
    (
        "status_order: chunks refusal uses KB001",
        "status_order",
        replace_once(
            "'a version without chunks cannot be approved';",
            "'a version without chunks cannot be approved' USING ERRCODE = 'KB001';",
        ),
    ),
    (
        "status_order: transition COALESCE defaults to true",
        "status_order",
        replace_once(
            "    ), false) THEN\n        RAISE EXCEPTION 'this status",
            "    ), true) THEN\n        RAISE EXCEPTION 'this status",
        ),
    ),
    (
        "status_order: effective-date check loses COALESCE",
        "status_order",
        replace_once(
            "IF NOT COALESCE(effective <= (approved_on + interval '12 months')::date, false) THEN",
            "IF NOT (effective <= (approved_on + interval '12 months')::date) THEN",
        ),
    ),
    (
        "status_order: effective-date COALESCE fails open",
        "status_order",
        replace_once(
            "(approved_on + interval '12 months')::date, false) THEN",
            "(approved_on + interval '12 months')::date, true) THEN",
        ),
    ),
    (
        "status_order: review-by check loses COALESCE",
        "status_order",
        replace_once(
            "IF NOT COALESCE(review_by >= approved_on, false) THEN",
            "IF NOT (review_by >= approved_on) THEN",
        ),
    ),
    (
        "status_order: review-by COALESCE fails open",
        "status_order",
        replace_once("review_by >= approved_on, false)", "review_by >= approved_on, true)"),
    ),
    (
        "status_order: recorded_at left to the loader",
        "status_order",
        replace_once("    NEW.recorded_at := stamp;\n", ""),
    ),
    # one advisory lock, per document, before any read
    *[
        (
            f"advisory_lock: {fn} takes no lock",
            "advisory_lock",
            in_function(fn, lambda seg: seg.replace(LOCK_CALL + "\n", "")),
        )
        for fn in INSERT_FUNCTIONS.values()
    ],
    *[
        (
            f"advisory_lock: {fn} locks a different key",
            "advisory_lock",
            in_function(fn, lambda seg: seg.replace("1220001", "1220002")),
        )
        for fn in INSERT_FUNCTIONS.values()
    ],
    (
        "advisory_lock: chunks lock on the version, not the document",
        "advisory_lock",
        in_function(
            "kb_chunks_insert",
            lambda seg: seg.replace("hashtext(NEW.document_id)", "hashtext(NEW.version::text)"),
        ),
    ),
    (
        "advisory_lock: session lock that is never released",
        "advisory_lock",
        replace_once("pg_catalog.pg_advisory_xact_lock", "pg_catalog.pg_advisory_lock"),
    ),
    (
        "advisory_lock: documents lock taken after the read",
        "advisory_lock",
        in_function(
            "kb_documents_insert",
            lambda seg: seg.replace(LOCK_CALL + "\n", "").replace(
                "    IF newest IS NOT NULL", "    " + LOCK_CALL + "\n    IF newest IS NOT NULL"
            ),
        ),
    ),
    # chunks freeze at the first status event
    (
        "chunk_freeze: check removed",
        "chunk_freeze",
        in_function("kb_chunks_insert", drop_chunk_freeze),
    ),
    (
        "chunk_freeze: ignores the version",
        "chunk_freeze",
        replace_once(
            "WHERE e.document_id = NEW.document_id AND e.version = NEW.version\n    ) THEN\n"
            "        RAISE EXCEPTION 'chunks",
            "WHERE e.document_id = NEW.document_id\n    ) THEN\n        RAISE EXCEPTION 'chunks",
        ),
    ),
    (
        "chunk_freeze: only a DRAFT blocks it open",
        "chunk_freeze",
        replace_once(
            "WHERE e.document_id = NEW.document_id AND e.version = NEW.version\n    ) THEN\n"
            "        RAISE EXCEPTION 'chunks",
            "WHERE e.document_id = NEW.document_id AND e.version = NEW.version\n"
            "          AND e.status <> 'DRAFT'\n    ) THEN\n        RAISE EXCEPTION 'chunks",
        ),
    ),
    (
        "chunk_freeze: inverted",
        "chunk_freeze",
        replace_once(
            "    IF EXISTS (\n        SELECT 1 FROM public.kb_status_events e",
            "    IF NOT EXISTS (\n        SELECT 1 FROM public.kb_status_events e",
        ),
    ),
    (
        "chunk_freeze: reuses the withdrawn SQLSTATE",
        "chunk_freeze",
        replace_once(
            "'chunks cannot be added to a version that has a status event';",
            "'chunks cannot be added to a version that has a status event'"
            " USING ERRCODE = 'KB001';",
        ),
    ),
    (
        "chunk_freeze: check runs before the lock",
        "chunk_freeze",
        in_function(
            "kb_chunks_insert",
            lambda seg: (
                lambda block, rest: rest.replace("    PERFORM", "    " + block + "\n    PERFORM", 1)
            )(CHUNK_FREEZE.search(seg).group(0), drop_chunk_freeze(seg)),  # type: ignore[union-attr]
        ),
    ),
    # review-by is at most 12 months after the effective date
    (
        "review_by_bound: CHECK removed",
        "review_by_bound",
        replace_once(
            "    CHECK (review_by_date <= (effective_date + interval '12 months')::date),\n", ""
        ),
    ),
    (
        "review_by_bound: 24 months",
        "review_by_bound",
        replace_once(
            "interval '12 months')::date),\n    CHECK (supersedes",
            "interval '24 months')::date),\n    CHECK (supersedes",
        ),
    ),
    (
        "review_by_bound: comparison reversed",
        "review_by_bound",
        replace_once(
            "CHECK (review_by_date <= (effective_date", "CHECK (review_by_date >= (effective_date"
        ),
    ),
    (
        "review_by_bound: lower bound removed",
        "review_by_bound",
        replace_once("    CHECK (effective_date <= review_by_date),\n", ""),
    ),
    (
        "review_by_bound: measured from a fixed date",
        "review_by_bound",
        replace_once(
            "(effective_date + interval '12 months')::date)",
            "(date '2030-01-01' + interval '12 months')::date)",
        ),
    ),
    # pin
    ("pinned: a trailing comment", "pinned", lambda s: s + "\n-- edited"),
    ("pinned: one character", "pinned", replace_once("999999", "999998")),
]


@pytest.mark.parametrize(("label", "helper", "mutate"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_a_broken_copy_of_the_sql_fails_the_helper(
    label: str, helper: str, mutate: Callable[[str], str]
) -> None:
    mutated = mutate(SQL)
    assert mutated != SQL, label
    HELPERS[helper](SQL)  # the unbroken text passes, so the failure below is the mutation's
    with pytest.raises(AssertionError):
        HELPERS[helper](mutated)


def test_every_helper_is_exercised_by_at_least_one_mutation() -> None:
    assert {m[1] for m in MUTATIONS} == set(HELPERS)


# --- the loader and application role names ------------------------------------------------------

from divesafe.services.postgres import (  # noqa: E402
    LOADER_ROLE_MISSING,
    LoaderRoleError,
    check_role_name,
    install_schema,
)

DUMMY_DSN = (
    "postgresql://nobody:nothing@127.0.0.1:1/never_used"  # never reached: refusals come first
)


@pytest.mark.parametrize("role", ["divesafe_loader", "_x", "a", "loader1", "a" * 63])
def test_acceptable_role_names_pass(role: str) -> None:
    check_role_name(role, what="loader")


@pytest.mark.parametrize(
    "role",
    [
        "public",
        "pg_monitor",
        "pg_",
        "pg_write_server_files",
        "Divesafe",
        "DIVESAFE_LOADER",
        "",
        "a" * 64,
        "ok\n",
        "\nok",
        "ok ",
        "a b",
        "1abc",
        "a-b",
        'a"b',
        "a;b",
        "é",
        "loader\x00",
    ],
)
def test_unacceptable_role_names_are_refused(role: str) -> None:
    with pytest.raises(ValueError, match="invalid loader role name"):
        check_role_name(role, what="loader")


def test_a_role_name_with_a_trailing_newline_is_not_accepted_by_a_dollar_anchor() -> None:
    # `re.match(r"...$", "ok\n")` would accept it; the validator must use fullmatch.
    assert re.match(r"[a-z_][a-z0-9_]{0,62}$", "ok\n") is not None
    with pytest.raises(ValueError):
        check_role_name("ok\n", what="loader")


@pytest.mark.parametrize("bad", ["public", "pg_monitor", "Bad", "", "ok\n", "a" * 64])
def test_install_schema_refuses_a_bad_loader_name_before_any_connection(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid loader role name"):
        install_schema(DUMMY_DSN, None, loader_role=bad)


@pytest.mark.parametrize("reserved", ["divesafe_owner", "divesafe_bootstrap"])
def test_install_schema_refuses_the_owner_and_bootstrap_names_as_loader(reserved: str) -> None:
    with pytest.raises(ValueError, match="invalid loader role name"):
        install_schema(DUMMY_DSN, None, loader_role=reserved)


def test_install_schema_refuses_the_application_role_as_the_loader() -> None:
    with pytest.raises(ValueError, match="invalid loader role name"):
        install_schema(DUMMY_DSN, "divesafe_app", loader_role="divesafe_app")


def test_install_schema_still_refuses_a_bad_application_name() -> None:
    with pytest.raises(ValueError, match="invalid application role name"):
        install_schema(DUMMY_DSN, "public", loader_role="divesafe_loader")


def test_the_missing_loader_message_is_fixed_and_carries_no_detail() -> None:
    assert LOADER_ROLE_MISSING == (
        "the knowledge loader role does not exist; create it before running db_init"
    )
    assert issubclass(LoaderRoleError, RuntimeError)


def test_the_settings_loader_role_name_rejects_what_the_validator_rejects() -> None:
    from pydantic import ValidationError

    from divesafe.config import Settings

    assert Settings(loader_db_role="divesafe_loader").loader_db_role == "divesafe_loader"
    for bad in ("Loader", "a" * 64, "", "1x", "a b", "ok\n"):
        with pytest.raises(ValidationError):
            Settings(loader_db_role=bad)
    assert Settings().loader_db_role is None
