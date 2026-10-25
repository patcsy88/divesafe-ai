"""ADR 0022 condition 9 against a real PostgreSQL: `knowledge_problems`, `verify()` as the
application role, `migrate()`'s post-check and the sha256 pins of the five knowledge functions.

Needs DIVESAFE_TEST_DATABASE_URL (a database named *_test, whose role may create databases and roles
and is a SUPERUSER: the break cases use ALTER TABLE ... OWNER TO and ALTER DATABASE ... OWNER TO).
Skipped otherwise. Offline these only collect and skip, so they prove nothing until a live run.

Every test gets its OWN scratch database (named *_test, created and force-dropped here); the shared
database is never touched, because the knowledge tables can never be emptied. Scratch roles end in
_test and are dropped in a finally. The DSN is never printed or stored: results are assigned to
variables before they are asserted. The expected problem texts are the SAME functions the offline
file uses (tests/safety/kb_catalogue_fake.py), so live and offline agree.

SERVER ASSUMPTIONS a live run may refute (each one is listed in the report):
  - PostgreSQL 16 or 17 (the only pinned majors); another major fails closed with `no pinned ...`;
  - `public` starts owned by pg_database_owner (PostgreSQL 15+ template) or by the owner role;
  - ALTER TABLE ... SET UNLOGGED works on kb_chunks alone, and on chunks -> events -> documents in
    that order (the server refuses to unlog kb_documents while permanent tables reference it);
  - a table whose parent is added with ALTER TABLE ... INHERIT is accepted when the columns match;
  - ALTER SCHEMA public OWNER TO pg_database_owner is accepted;
  - a recreated guard trigger (DROP, CREATE, ENABLE ALWAYS) gives ONLY the type or shape problem
    (WHEN (false), BEFORE DELETE only, BEFORE UPDATE only, AFTER INSERT, UPDATE OF column list,
    an argument), because the function pin and the enablement are unchanged;
  - `BEFORE UPDATE OF title OR DELETE` keeps tgtype 27 (the column list lives in tgattr);
  - DEFERRABLE is not tested: only constraint triggers can be deferrable, and they are AFTER ROW,
    so the case would not isolate the shape check;
  - `python -m divesafe.services.kb_pins` runs as a subprocess from this interpreter with src on
    PYTHONPATH, read-only (no APPLY), against the migrated scratch database.

Also here: the nine tgtype pins equal what the server stores (section 2) and each tgtype / shape
break (section 3, group h) is seen by the owner path, verify() as the application role, and
migrate()'s refusal."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import psycopg
import psycopg.conninfo
import pytest
from tests.integration.test_knowledge_store import PASSWORD, _drop_role
from tests.safety import kb_catalogue_fake as fake
from tests.unit.test_repository_contract import TEST_DSN as DSN

from divesafe.services import migrations as migrations_module
from divesafe.services.migrations import (
    KNOWLEDGE_FUNCTION_SHA256,
    KNOWLEDGE_FUNCTIONS,
    KNOWLEDGE_TABLES,
    KNOWLEDGE_TRIGGER_TGTYPE,
    MIGRATIONS,
    MigrationError,
    knowledge_function_hashes,
    knowledge_problems,
    knowledge_trigger_types,
    migrate,
)
from divesafe.services.postgres import PostgresAssessmentRepository, install_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
]

APP = "divesafe_kb_catapp_test"
LOADER = "divesafe_kb_catloader_test"
OTHER = "divesafe_kb_catother_test"
KB_BASE = "the knowledge guard is not intact after migrating"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _ident(name: str) -> str:
    assert IDENTIFIER.fullmatch(name), "unexpected identifier"
    return f'"{name}"'


def _dsn_for(database: str, user: str | None = None) -> str:
    assert DSN is not None
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    parts["dbname"] = database
    if user is not None:
        parts.update(user=user, password=PASSWORD)
    return psycopg.conninfo.make_conninfo(**parts)


def _is_knowledge(problem: str) -> bool:
    return "knowledge" in problem or "kb_" in problem or "public schema" in problem


@dataclass
class Scratch:
    database: str
    admin_dsn: str
    app_dsn: str

    def rows(self, query: str, params: Any = None) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.admin_dsn) as conn:
            cur = conn.execute(query, params)  # type: ignore[call-overload]
            return cur.fetchall() if cur.description else []

    def owner_problems(self) -> list[str]:
        with psycopg.connect(self.admin_dsn) as conn:
            found = knowledge_problems(conn, owner_is_current_user=True)
        return found

    def verify_as_app(self) -> list[str]:
        problems = PostgresAssessmentRepository(self.app_dsn).verify(least_privilege=False)
        return [p for p in problems if _is_knowledge(p)]

    def apply(self, statements: tuple[str, ...]) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            me_row = conn.execute("SELECT current_user").fetchone()
            assert me_row is not None
            names = {
                "db": _ident(self.database),
                "me": _ident(str(me_row[0])),
                "other": _ident(OTHER),
            }
            for raw in statements:
                if raw.startswith("@comment:"):
                    function = raw.split(":", 1)[1]
                    row = conn.execute(
                        "SELECT pg_catalog.pg_get_functiondef(%s::pg_catalog.regprocedure)",
                        (f"public.{function}()",),
                    ).fetchone()
                    assert row is not None
                    text = str(row[0]).rstrip()
                    assert text.endswith("$function$")
                    conn.execute(text[: -len("$function$")] + "\n-- changed\n$function$")  # type: ignore[call-overload]
                    continue
                conn.execute(raw.format(**names))  # type: ignore[call-overload]


@pytest.fixture(scope="module")
def _roles() -> Iterator[None]:
    assert DSN is not None
    name = str(psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", ""))
    if not name.endswith("_test"):
        raise RuntimeError("DIVESAFE_TEST_DATABASE_URL must point at a database named *_test")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for role in (APP, LOADER, OTHER):
            _drop_role(conn, role)
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'")  # type: ignore[call-overload]
    try:
        yield
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for role in (APP, LOADER, OTHER):
                _drop_role(conn, role)


@pytest.fixture
def bare(_roles: None) -> Iterator[Scratch]:
    """A new, EMPTY scratch database (nothing migrated)."""
    assert DSN is not None
    database = f"divesafe_cat_{uuid.uuid4().hex[:10]}_test"
    assert database.endswith("_test")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database}"')  # type: ignore[call-overload]
    try:
        yield Scratch(database, _dsn_for(database), _dsn_for(database, APP))
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')  # type: ignore[call-overload]


@pytest.fixture
def scratch(bare: Scratch) -> Scratch:
    """A scratch database migrated to 4 with the REAL pins, plus the app and loader roles."""
    install_schema(bare.admin_dsn, APP, loader_role=LOADER)
    return bare


def _major(scratch: Scratch) -> int:
    row = scratch.rows("SELECT current_setting('server_version_num')::int")
    return int(row[0][0]) // 10000


# --- 1. intact ------------------------------------------------------------------------------------


def test_an_intact_catalogue_has_no_problems_for_the_owner(scratch: Scratch) -> None:
    problems = scratch.owner_problems()
    assert problems == []


def test_verify_as_the_application_role_reports_no_knowledge_problem(scratch: Scratch) -> None:
    """Proves the application role can read the catalogues and the pg_database_owner lookup. If it
    cannot, the problem list holds the fixed 'cannot read the knowledge table definitions' text."""
    problems = scratch.verify_as_app()
    assert problems == []


@pytest.mark.parametrize(
    "catalogue",
    [
        "pg_trigger",
        "pg_proc",
        "pg_policy",
        "pg_rewrite",
        "pg_inherits",
        "pg_roles",
        "pg_database",
        "pg_namespace",
        "pg_class",
    ],
)
def test_the_application_role_can_read_each_catalogue_the_check_uses(
    scratch: Scratch, catalogue: str
) -> None:
    assert IDENTIFIER.fullmatch(catalogue)
    with psycopg.connect(scratch.app_dsn) as conn:
        row = conn.execute(f"SELECT pg_catalog.count(*) FROM pg_catalog.{catalogue}").fetchone()  # type: ignore[call-overload]
    assert row is not None
    assert isinstance(row[0], int)


def test_the_application_role_can_read_the_function_definitions_and_the_database_owner_role(
    scratch: Scratch,
) -> None:
    with psycopg.connect(scratch.app_dsn) as conn:
        hashes = knowledge_function_hashes(conn)
        owner_role = conn.execute(
            "SELECT oid FROM pg_catalog.pg_roles WHERE rolname = 'pg_database_owner'"
        ).fetchone()
    assert set(hashes[1]) == set(KNOWLEDGE_FUNCTIONS.values())
    assert owner_role is not None  # PostgreSQL 15+ has the built-in role


def test_what_owns_public_on_the_scratch_database(scratch: Scratch) -> None:
    """A FACT test: on the server it runs on, public is owned by pg_database_owner (PostgreSQL 15+
    template) or by the owner role itself, and the database owner is the owner role."""
    rows = scratch.rows(
        "SELECT (SELECT rolname FROM pg_catalog.pg_roles WHERE oid = n.nspowner), "
        "(SELECT rolname FROM pg_catalog.pg_roles WHERE oid = d.datdba), current_user "
        "FROM pg_catalog.pg_namespace n, pg_catalog.pg_database d "
        "WHERE n.nspname = 'public' AND d.datname = pg_catalog.current_database()"
    )
    schema_owner, database_owner, me = rows[0]
    assert schema_owner in ("pg_database_owner", me)
    assert database_owner == me


# --- 2. the pins match the real functions ---------------------------------------------------------


def test_the_pins_match_the_real_functions_on_the_running_major(scratch: Scratch) -> None:
    """A changed migration 4 text, or a PostgreSQL major with different pg_get_functiondef output,
    fails here: regenerate with `python -m divesafe.services.kb_pins` and review."""
    with psycopg.connect(scratch.admin_dsn) as conn:
        major, hashes = knowledge_function_hashes(conn)
    pinned = KNOWLEDGE_FUNCTION_SHA256.get(major)
    assert pinned is not None, f"no pins for PostgreSQL major {major}"
    assert hashes == dict(pinned)


def test_the_trigger_types_match_the_pins_on_the_running_major(scratch: Scratch) -> None:
    """The nine tgtype values pinned in migrations.py (7, 27, 34) are what the real CREATE TRIGGER
    text of migration 4 produces on this server."""
    with psycopg.connect(scratch.admin_dsn) as conn:
        found = knowledge_trigger_types(conn)
    expected = dict(KNOWLEDGE_TRIGGER_TGTYPE)
    assert found == expected
    assert set(found.values()) == {7, 27, 34}


def test_the_trigger_types_are_the_same_read_as_the_application_role(scratch: Scratch) -> None:
    with psycopg.connect(scratch.app_dsn) as conn:
        found = knowledge_trigger_types(conn)
    assert found == dict(KNOWLEDGE_TRIGGER_TGTYPE)


def test_kb_pins_lists_the_major_five_hashes_and_nine_tgtype_lines(scratch: Scratch) -> None:
    """Runs the dev tool as a subprocess in its read-only mode against the migrated scratch
    database. The DSN goes in the child's environment only; the output must not contain it."""
    env = {**os.environ, "DIVESAFE_PIN_DSN": scratch.admin_dsn}
    env.pop("DIVESAFE_PIN_APPLY", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[2] / "src"), env.get("PYTHONPATH", "")]
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "divesafe.services.kb_pins"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    code = done.returncode
    lines = done.stdout.splitlines()
    leaked = scratch.admin_dsn in done.stdout + done.stderr or PASSWORD in done.stdout + done.stderr
    type_lines = [line.split(" ") for line in lines if line.startswith("tgtype ")]
    function_lines = [line.split(" ") for line in lines[1:] if not line.startswith("tgtype ")]
    major = _major(scratch)
    assert code == 0
    assert lines[0] == f"postgresql_major {major}"
    assert {name: digest for name, digest in function_lines} == dict(
        KNOWLEDGE_FUNCTION_SHA256[major]
    )
    assert len(type_lines) == 9
    assert {name: int(value) for _, name, value in type_lines} == dict(KNOWLEDGE_TRIGGER_TGTYPE)
    assert leaked is False


# --- 3. break one invariant at a time -------------------------------------------------------------


@dataclass(frozen=True)
class Break:
    id: str
    statements: tuple[str, ...]
    expected: list[str]
    migrate_detail: bool = True  # False: migrate()'s earlier trigger-enablement check fires first


NOOP = (
    "CREATE FUNCTION public.leaky_noop_test() RETURNS trigger LANGUAGE plpgsql "
    "AS $$ BEGIN RETURN NEW; END $$"
)
INSERT_GUARD = "kb_chunks_insert_guard"
REPLACE_INSERT_GUARD = (
    "DROP TRIGGER kb_chunks_insert_guard ON public.kb_chunks",
    "CREATE TRIGGER kb_chunks_insert_guard BEFORE INSERT ON public.kb_chunks FOR EACH ROW "
    "EXECUTE FUNCTION {function}()",
    "ALTER TABLE public.kb_chunks ENABLE ALWAYS TRIGGER kb_chunks_insert_guard",
)


def _replacing(function: str) -> tuple[str, ...]:
    return tuple(s.replace("{function}", function) for s in REPLACE_INSERT_GUARD)


def _truncate_function(body: str) -> str:
    return (
        "CREATE OR REPLACE FUNCTION public.kb_no_truncate() RETURNS trigger LANGUAGE plpgsql "
        f"VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$ {body} $$"
    )


def _recreate(
    trigger: str, table: str, event: str, function: str, *, level: str = "ROW", extra: str = ""
) -> tuple[str, ...]:
    """Drop a guard trigger and create it again with a changed shape, then ENABLE ALWAYS again so
    that ONLY the type or shape differs (not the enablement, which has its own problem text)."""
    return (
        f"DROP TRIGGER {trigger} ON public.{table}",
        f"CREATE TRIGGER {trigger} {event} ON public.{table} FOR EACH {level} {extra} "
        f"EXECUTE FUNCTION public.{function}",
        f"ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER {trigger}",
    )


APPEND_ONLY = "kb_chunks_append_only_guard"
DOCUMENTS_APPEND_ONLY = "kb_documents_append_only_guard"
TRUNCATE_GUARD = "kb_chunks_no_truncate_guard"

BREAKS = [
    # (h) trigger type (tgtype) and plain shape. DEFERRABLE is not tested: only a CONSTRAINT trigger
    # can be deferrable, and those are AFTER ROW only, so a recreated guard would also have the
    # wrong tgtype and a different function-call shape (that case would not isolate the check).
    Break(
        "insert-guard-with-a-when-false-clause",
        _recreate(
            INSERT_GUARD, "kb_chunks", "BEFORE INSERT", "kb_chunks_insert()", extra="WHEN (false)"
        ),
        [fake.not_plain(INSERT_GUARD)],
    ),
    Break(
        "truncate-guard-with-a-when-false-clause",
        _recreate(
            TRUNCATE_GUARD,
            "kb_chunks",
            "BEFORE TRUNCATE",
            "kb_no_truncate()",
            level="STATEMENT",
            extra="WHEN (false)",
        ),
        [fake.not_plain(TRUNCATE_GUARD)],
    ),
    Break(
        "append-only-guard-fires-on-delete-only",
        _recreate(APPEND_ONLY, "kb_chunks", "BEFORE DELETE", "kb_no_update_delete()"),
        [fake.not_type(APPEND_ONLY)],  # tgtype 11, not 27
    ),
    Break(
        "append-only-guard-fires-on-update-only",
        _recreate(APPEND_ONLY, "kb_chunks", "BEFORE UPDATE", "kb_no_update_delete()"),
        [fake.not_type(APPEND_ONLY)],  # tgtype 19, not 27
    ),
    Break(
        "insert-guard-fires-on-update-instead-of-insert",
        _recreate(INSERT_GUARD, "kb_chunks", "BEFORE UPDATE", "kb_chunks_insert()"),
        [fake.not_type(INSERT_GUARD)],  # tgtype 19, not 7
    ),
    Break(
        "insert-guard-is-after-not-before",
        _recreate(INSERT_GUARD, "kb_chunks", "AFTER INSERT", "kb_chunks_insert()"),
        [fake.not_type(INSERT_GUARD)],  # tgtype 5, not 7
    ),
    Break(
        "append-only-guard-with-a-column-list",
        # tgtype stays 27 (BEFORE ROW UPDATE|DELETE): only the plain-shape check can see tgattr
        _recreate(
            DOCUMENTS_APPEND_ONLY,
            "kb_documents",
            "BEFORE UPDATE OF title OR DELETE",
            "kb_no_update_delete()",
        ),
        [fake.not_plain(DOCUMENTS_APPEND_ONLY)],
    ),
    Break(
        "insert-guard-with-an-argument",
        _recreate(INSERT_GUARD, "kb_chunks", "BEFORE INSERT", "kb_chunks_insert('x')"),
        [fake.not_plain(INSERT_GUARD)],
    ),
    # (a) the trigger set
    Break(
        "extra-trigger",
        (
            NOOP,
            "CREATE TRIGGER leaky_trigger_test BEFORE INSERT ON public.kb_chunks FOR EACH ROW "
            "EXECUTE FUNCTION public.leaky_noop_test()",
        ),
        [fake.NINE],
    ),
    Break(
        "trigger-dropped",
        ("DROP TRIGGER kb_chunks_insert_guard ON public.kb_chunks",),
        [fake.NINE],
        migrate_detail=False,
    ),
    Break(
        "same-name-trigger-on-another-table",
        (
            "CREATE TRIGGER kb_chunks_insert_guard BEFORE INSERT ON public.kb_documents "
            "FOR EACH ROW EXECUTE FUNCTION public.kb_chunks_insert()",
            "ALTER TABLE public.kb_documents ENABLE ALWAYS TRIGGER kb_chunks_insert_guard",
        ),
        [fake.NINE, fake.wrong_table(INSERT_GUARD)],
    ),
    Break(
        "trigger-calls-the-wrong-function",
        _replacing("public.kb_no_truncate"),
        [fake.not_call(INSERT_GUARD), fake.mismatch("kb_chunks_insert")],
    ),
    Break(
        "trigger-calls-a-same-named-function-in-another-schema",
        (
            "CREATE SCHEMA leaky_schema_test",
            "CREATE FUNCTION leaky_schema_test.kb_chunks_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
            *_replacing("leaky_schema_test.kb_chunks_insert"),
        ),
        [fake.not_call(INSERT_GUARD), fake.mismatch("kb_chunks_insert")],
    ),
    Break(
        "trigger-disabled",
        ("ALTER TABLE public.kb_chunks DISABLE TRIGGER kb_chunks_append_only_guard",),
        [fake.not_always("kb_chunks_append_only_guard")],
        migrate_detail=False,
    ),
    Break(
        "trigger-enabled-for-replica-only",
        ("ALTER TABLE public.kb_chunks ENABLE REPLICA TRIGGER kb_chunks_append_only_guard",),
        [fake.not_always("kb_chunks_append_only_guard")],
        migrate_detail=False,
    ),
    Break(
        "trigger-enabled-for-origin-only",
        ("ALTER TABLE public.kb_chunks ENABLE TRIGGER kb_chunks_append_only_guard",),
        [fake.not_always("kb_chunks_append_only_guard")],
        migrate_detail=False,
    ),
    # (b) the function definitions
    Break(
        "function-body-changed",
        (_truncate_function("BEGIN RAISE EXCEPTION 'changed'; END"),),
        [fake.mismatch("kb_no_truncate")] * 3,
    ),
    Break(
        "function-comment-added",
        ("@comment:kb_no_update_delete",),
        [fake.mismatch("kb_no_update_delete")] * 3,
    ),
    Break(
        "function-made-security-invoker",
        ("ALTER FUNCTION public.kb_no_truncate() SECURITY INVOKER",),
        [fake.mismatch("kb_no_truncate")] * 3,
    ),
    Break(
        "function-search-path-changed",
        ("ALTER FUNCTION public.kb_no_truncate() SET search_path = public",),
        [fake.mismatch("kb_no_truncate")] * 3,
    ),
    # (c) rules
    Break(
        "rewrite-rule",
        ("CREATE RULE leaky_rule_test AS ON INSERT TO public.kb_chunks DO INSTEAD NOTHING",),
        [fake.RULE],
    ),
    # (d) row-level security and policies
    Break(
        "row-security-enabled",
        ("ALTER TABLE public.kb_documents ENABLE ROW LEVEL SECURITY",),
        [fake.rls_on("kb_documents")],
    ),
    Break(
        "row-security-forced",
        ("ALTER TABLE public.kb_status_events FORCE ROW LEVEL SECURITY",),
        [fake.rls_on("kb_status_events")],
    ),
    Break(
        "policy-without-row-security",
        ("CREATE POLICY leaky_policy_test ON public.kb_chunks USING (true)",),
        [fake.POLICY],
    ),
    Break(
        "row-security-and-a-policy",
        (
            "ALTER TABLE public.kb_chunks ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY leaky_policy_test ON public.kb_chunks USING (true)",
        ),
        [fake.rls_on("kb_chunks"), fake.POLICY],
    ),
    # (e) inheritance
    Break(
        "a-child-inherits-a-knowledge-table",
        ("CREATE TABLE public.leaky_child_test () INHERITS (public.kb_chunks)",),
        [fake.INHERIT],
    ),
    Break(
        "a-knowledge-table-inherits-a-parent",
        (
            "CREATE TABLE public.leaky_parent_test (LIKE public.kb_chunks)",
            "ALTER TABLE public.kb_chunks INHERIT public.leaky_parent_test",
        ),
        [fake.INHERIT],
    ),
    # (f) persistence (relpersistence 't' cannot be set on an existing table: offline only)
    Break(
        "unlogged-table",
        # kb_documents is referenced by permanent tables (PostgreSQL: could not change table
        # "kb_documents" to unlogged because it references logged table...), so unlog a CHILD.
        ("ALTER TABLE public.kb_chunks SET UNLOGGED",),
        [fake.not_permanent("kb_chunks")],
    ),
    Break(
        "all-three-tables-unlogged",
        # children first: kb_documents is referenced by the other two, so it can only become
        # unlogged after they are (an unlogged table may reference a logged one, not the reverse)
        tuple(
            f"ALTER TABLE public.{t} SET UNLOGGED"
            for t in ("kb_chunks", "kb_status_events", "kb_documents")
        ),
        [fake.not_permanent(t) for t in KNOWLEDGE_TABLES],
    ),
    # (g) owners
    Break(
        "table-owner-changed",
        ("ALTER TABLE public.kb_chunks OWNER TO {other}",),
        [fake.wrong_owner("kb_chunks")],
    ),
    Break(
        "another-table-owner-changed",
        ("ALTER TABLE public.kb_documents OWNER TO {other}",),
        [fake.wrong_owner("kb_documents")],
    ),
    Break(
        "public-schema-owner-changed",
        ("ALTER SCHEMA public OWNER TO {other}",),
        [fake.PUBLIC_OWNER],
    ),
    Break(
        "public-owned-by-pg_database_owner-but-the-database-owner-changed",
        ("ALTER SCHEMA public OWNER TO pg_database_owner", "ALTER DATABASE {db} OWNER TO {other}"),
        [fake.PUBLIC_OWNER],
    ),
]
BREAK_IDS = [b.id for b in BREAKS]

# Accepted states: the invariant holds in another legitimate form.
ACCEPTED = [
    Break(
        "public-owned-by-pg_database_owner", ("ALTER SCHEMA public OWNER TO pg_database_owner",), []
    ),
    Break("public-owned-by-the-owner-directly", ("ALTER SCHEMA public OWNER TO {me}",), []),
]


@pytest.mark.parametrize("case", BREAKS, ids=BREAK_IDS)
def test_each_broken_invariant_is_reported_with_its_exact_fixed_problem(
    scratch: Scratch, case: Break
) -> None:
    before = scratch.owner_problems()
    assert before == []
    scratch.apply(case.statements)
    by_owner = scratch.owner_problems()
    by_app = scratch.verify_as_app()
    assert sorted(by_owner) == sorted(case.expected)
    assert sorted(by_app) == sorted(case.expected)  # verify() as the application role agrees


@pytest.mark.parametrize("case", BREAKS, ids=BREAK_IDS)
def test_migrate_refuses_a_database_with_each_broken_invariant(
    scratch: Scratch, case: Break
) -> None:
    scratch.apply(case.statements)
    with pytest.raises(MigrationError) as caught:
        migrate(scratch.admin_dsn)
    message = str(caught.value)
    assert message.startswith(KB_BASE)
    if case.migrate_detail:
        assert message.startswith(KB_BASE + ": ")
        for problem in case.expected:
            assert problem in message


@pytest.mark.parametrize("case", ACCEPTED, ids=[c.id for c in ACCEPTED])
def test_the_other_legitimate_public_owner_forms_are_accepted(
    scratch: Scratch, case: Break
) -> None:
    scratch.apply(case.statements)
    by_owner = scratch.owner_problems()
    by_app = scratch.verify_as_app()
    applied = migrate(scratch.admin_dsn)
    assert by_owner == []
    assert by_app == []
    assert applied == []


def test_each_break_message_is_a_fixed_text_naming_no_scratch_object(scratch: Scratch) -> None:
    scratch.apply(BREAKS[0].statements + ("ALTER TABLE public.kb_chunks OWNER TO {other}",))
    found = scratch.owner_problems()
    joined = "\n".join(found)
    assert found
    for leaked in ("leaky_trigger_test", "leaky_noop_test", OTHER):
        assert leaked not in joined


# --- 4. a database without the knowledge tables ---------------------------------------------------


def test_verify_on_a_database_at_migration_3_gives_the_fixed_does_not_exist_problems(
    bare: Scratch,
) -> None:
    migrate(bare.admin_dsn, MIGRATIONS[:3])
    problems = PostgresAssessmentRepository(bare.admin_dsn).verify(least_privilege=False)
    knowledge = [p for p in problems if _is_knowledge(p)]
    expected = [
        f"the {t} table does not exist (run db_init as the owner role)" for t in KNOWLEDGE_TABLES
    ]
    assert knowledge == expected  # and the detailed checks were skipped (no other knowledge text)
    assert any("schema version 3" in p for p in problems)


def test_verify_on_an_empty_database_reports_the_missing_assessments_table_only(
    bare: Scratch,
) -> None:
    problems = PostgresAssessmentRepository(bare.admin_dsn).verify(least_privilege=False)
    assert problems == ["the assessments table does not exist (run db_init as the owner role)"]


# --- 5. migrate() refuses a wrong pin and rolls back ----------------------------------------------


def _pins(major: int, digest: str) -> Any:
    wrong = MappingProxyType({f: digest for f in set(KNOWLEDGE_FUNCTIONS.values())})
    return MappingProxyType({major: wrong})


def _existing(scratch: Scratch) -> tuple[Any, ...]:
    row = scratch.rows(
        "SELECT to_regclass('public.kb_chunks'), to_regclass('public.schema_migrations'), "
        "to_regclass('public.assessments')"
    )
    return row[0]


def test_a_wrong_pin_makes_migrate_refuse_and_roll_back_a_fresh_database(
    bare: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    major = _major(bare)
    monkeypatch.setattr(migrations_module, "KNOWLEDGE_FUNCTION_SHA256", _pins(major, "0" * 64))
    with pytest.raises(MigrationError) as caught:
        migrate(bare.admin_dsn)
    message = str(caught.value)
    existing = _existing(bare)
    assert message.startswith(KB_BASE + ": ")
    assert fake.mismatch("kb_chunks_insert") in message
    assert fake.mismatch("kb_no_truncate") in message
    assert existing == (None, None, None)  # nothing survived: the one transaction rolled back


def test_a_wrong_pin_leaves_the_schema_version_unchanged_and_a_retry_with_the_real_pins_works(
    bare: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate(bare.admin_dsn, MIGRATIONS[:3])
    major = _major(bare)
    monkeypatch.setattr(migrations_module, "KNOWLEDGE_FUNCTION_SHA256", _pins(major, "f" * 64))
    with pytest.raises(MigrationError):
        migrate(bare.admin_dsn)
    versions = [int(r[0]) for r in bare.rows("SELECT version FROM public.schema_migrations")]
    existing = _existing(bare)
    assert sorted(versions) == [1, 2, 3]
    assert existing[0] is None  # no kb table
    monkeypatch.undo()
    applied = migrate(bare.admin_dsn)
    after = [int(r[0]) for r in bare.rows("SELECT version FROM public.schema_migrations")]
    assert applied == [4]
    assert sorted(after) == [1, 2, 3, 4]


def test_a_missing_pin_for_the_running_major_makes_migrate_refuse_and_roll_back(
    bare: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations_module, "KNOWLEDGE_FUNCTION_SHA256", MappingProxyType({}))
    with pytest.raises(MigrationError) as caught:
        migrate(bare.admin_dsn)
    message = str(caught.value)
    existing = _existing(bare)
    assert fake.no_pin("kb_chunks_insert") in message
    assert existing == (None, None, None)


def test_a_pin_for_only_another_major_makes_migrate_refuse(
    bare: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_major = 15 if _major(bare) != 15 else 18
    monkeypatch.setattr(
        migrations_module, "KNOWLEDGE_FUNCTION_SHA256", _pins(other_major, "0" * 64)
    )
    with pytest.raises(MigrationError) as caught:
        migrate(bare.admin_dsn)
    message = str(caught.value)
    assert fake.no_pin("kb_no_truncate") in message
