"""`guarded()` after the knowledge migration (ADR 0022 decision 3): a later migration (version 5 and
up) may not name a knowledge object, rename or drop a knowledge table, change its owner or column
defaults, or grant or revoke on it. Other ways to change a knowledge table (ADD COLUMN, ADD
CONSTRAINT, SET SCHEMA, UNLOGGED, schema changes, triggers, policies, rules, inheritance, event
triggers, ...) are NOT caught; they are recorded as strict xfails in `KNOWN_GAPS`. Database-free.

Mutations patch the module attributes in the test (`monkeypatch`); `src` is never edited. Each
mutation runs the same check that passes on the real module and expects it to fail."""

from __future__ import annotations

import pytest

from divesafe.services import migrations as migrations_module
from divesafe.services.migrations import (
    KNOWLEDGE_NAMES,
    KNOWLEDGE_TABLES,
    MIGRATIONS,
    Migration,
    MigrationError,
)
from divesafe.services.migrations import (
    guarded as real_guarded,
)

pytestmark = pytest.mark.safety


def refuses(sql: str, version: int = 5) -> bool:
    # Looked up on the module at call time, so a monkeypatched `guarded` is really exercised.
    try:
        migrations_module.guarded((*MIGRATIONS, Migration(version, "later", sql)))
    except MigrationError:
        return True
    return False


TABLE_TEMPLATES = (
    "DROP TABLE {t}",
    "drop table if exists public.{t} cascade",
    "Drop   Table  Public.{T}",
    'DROP TABLE "public"."{t}"',
    "DROP\n\tTABLE\n  {t}",
    "ALTER TABLE {t} RENAME TO other",
    "alter table public.{t} rename to other",
    "ALTER  TABLE   public.{T}   RENAME   TO other",
    "ALTER TABLE public.{t} RENAME COLUMN a TO b",
    "ALTER TABLE ONLY {t} DROP CONSTRAINT c",
    "ALTER TABLE {t} DROP COLUMN section",
    "ALTER TABLE {t} ALTER COLUMN title DROP NOT NULL",
    "ALTER TABLE {t} OWNER TO someone",
    "ALTER TABLE {t} ALTER COLUMN title SET DEFAULT 'x'",
    "ALTER TABLE {t} ALTER COLUMN title TYPE text",
    "GRANT UPDATE ON {t} TO someone",
    "GRANT ALL ON TABLE public.{t} TO PUBLIC",
    "REVOKE SELECT ON public.{t} FROM someone",
    "revoke   all   on   {T}   from   someone",
    "ALTER TABLE {t} DISABLE TRIGGER ALL",
)

SEQUENCE_TEMPLATES = (
    "ALTER SEQUENCE public.kb_status_event_order_seq RESTART",
    "ALTER SEQUENCE kb_status_event_order_seq INCREMENT BY -1",
    "SELECT setval('public.kb_status_event_order_seq', 1)",
    "SELECT nextval('kb_status_event_order_seq')",
    "DROP SEQUENCE kb_status_event_order_seq",
    "GRANT USAGE ON SEQUENCE kb_status_event_order_seq TO someone",
    "REVOKE ALL ON SEQUENCE public.kb_status_event_order_seq FROM someone",
    "ALTER SEQUENCE KB_STATUS_EVENT_ORDER_SEQ OWNER TO someone",
)

# Statements that name a knowledge table without a keyword the rules look at, or that reach it
# through a wider object. Recorded so the gap is visible; strict, so closing it forces a review.
KNOWN_GAPS = (
    "ALTER TABLE kb_chunks SET SCHEMA other",
    "ALTER TABLE kb_chunks ADD COLUMN x int",
    "ALTER TABLE kb_documents ADD CONSTRAINT z CHECK (true)",
    "ALTER TABLE kb_chunks SET (fillfactor=50)",
    "ALTER TABLE kb_chunks NO FORCE ROW LEVEL SECURITY",
    "GRANT ALL ON ALL TABLES IN SCHEMA public TO someone",
    "DROP SCHEMA public CASCADE",
    # The patterns stop at ';', so one inside a comment or a string hides the harmful verb:
    "ALTER TABLE public.kb_chunks /* ; */ RENAME TO z",
    "ALTER TABLE public.kb_chunks -- ;\n RENAME TO z",
    "ALTER TABLE kb_chunks ADD COLUMN a text DEFAULT ';', DROP COLUMN b",
    # Objects that attach behaviour to a knowledge table without any of the checked verbs:
    "CREATE TRIGGER t BEFORE INSERT ON public.kb_chunks FOR EACH ROW EXECUTE FUNCTION f()",
    "DO $$ BEGIN EXECUTE 'DROP TABLE ' || 'kb_' || 'chunks'; END $$",
    "ALTER TABLE public.kb_chunks ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY p ON public.kb_chunks USING (false)",
    "CREATE RULE r AS ON INSERT TO public.kb_chunks DO INSTEAD NOTHING",
    "CREATE TABLE public.child () INHERITS (public.kb_chunks)",
    "REASSIGN OWNED BY divesafe_owner TO someone",
    "ALTER TABLE public.kb_status_events SET UNLOGGED",
    "ALTER SCHEMA public OWNER TO x",
    "ALTER SCHEMA public RENAME TO x",
    "CREATE EVENT TRIGGER t ON ddl_command_end EXECUTE FUNCTION f()",
)

# A statement list that merely names a table before a ';' is harmless and must stay accepted.
HARMLESS = (
    "ALTER TABLE kb_documents; ALTER TABLE y RENAME TO z",
    "CREATE TABLE public.harmless (x int);",
)


def check_every_name_is_refused() -> None:
    for name in KNOWLEDGE_NAMES:
        assert refuses(f"SELECT '{name}';"), name
        assert refuses(f"select '{name.upper()}';"), name


@pytest.mark.parametrize("statement", HARMLESS)
def test_a_statement_that_only_separates_a_knowledge_table_name_from_other_ddl_is_accepted(
    statement: str,
) -> None:
    assert not refuses(statement)


def test_a_harmless_later_migration_is_accepted() -> None:
    assert not refuses("CREATE TABLE public.harmless (x int);")
    assert not refuses("ALTER TABLE public.assessments_extra ADD COLUMN y int;")


def test_the_knowledge_names_include_the_sequence_every_table_trigger_and_function() -> None:
    assert "kb_status_event_order_seq" in KNOWLEDGE_NAMES
    assert len(KNOWLEDGE_NAMES) == len(set(KNOWLEDGE_NAMES)) >= 14
    for table in KNOWLEDGE_TABLES:
        assert any(name.startswith(table) for name in KNOWLEDGE_NAMES)


@pytest.mark.parametrize("name", KNOWLEDGE_NAMES)
def test_a_version_five_that_names_a_knowledge_object_is_refused(name: str) -> None:
    assert refuses(f"SELECT '{name}';")
    assert refuses(f"select '{name.upper()}';")
    assert refuses(f"  COMMENT ON FUNCTION   {name}  IS 'x';")


def test_the_same_name_in_the_knowledge_migration_itself_is_not_refused() -> None:
    # Version 4 creates the objects, so it is exempt; only later versions are held to the rule.
    real_guarded(MIGRATIONS)
    assert not refuses("SELECT 'kb_chunks_insert_guard';", version=4)


@pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
@pytest.mark.parametrize("template", TABLE_TEMPLATES)
def test_ddl_and_grants_on_a_knowledge_table_are_refused(table: str, template: str) -> None:
    statement = template.format(t=table, T=table.upper())
    assert refuses(statement), statement
    assert refuses(f"SELECT 1;\n{statement};"), statement


@pytest.mark.parametrize("statement", SEQUENCE_TEMPLATES)
def test_anything_that_touches_the_ordering_sequence_is_refused(statement: str) -> None:
    assert refuses(statement), statement


@pytest.mark.parametrize("version", [5, 6, 20])
def test_the_rule_holds_for_every_later_version(version: int) -> None:
    assert refuses("DROP TABLE kb_chunks", version=version)


@pytest.mark.parametrize("statement", KNOWN_GAPS)
@pytest.mark.xfail(strict=True, reason="known gap in guarded(): reported, not fixed here")
def test_known_gap_guarded_does_not_yet_refuse(statement: str) -> None:
    assert refuses(statement), statement


# --- mutations: break the rule once and the same checks must fail --------------------------------


def refuses_with_names(names: tuple[str, ...], word: str) -> bool:
    """`guarded` with only `names` in force, for a statement that names `word`."""
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(migrations_module, "KNOWLEDGE_NAMES", names)
        return refuses(f"SELECT '{word}';")
    finally:
        monkey.undo()


def check_names_are_refused_under(names: tuple[str, ...]) -> None:
    """Every REAL name must still be refused when only `names` are in force (a mutation)."""
    for word in KNOWLEDGE_NAMES:
        assert refuses_with_names(names, word), word


def run_name_check_with(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> None:
    monkeypatch.setattr(migrations_module, "KNOWLEDGE_NAMES", names)
    for word in KNOWLEDGE_NAMES:
        assert refuses(f"SELECT '{word}';"), word


def test_the_name_check_passes_on_the_real_module() -> None:
    check_every_name_is_refused()
    check_names_are_refused_under(KNOWLEDGE_NAMES)


def test_mutation_an_empty_names_tuple_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError):
        run_name_check_with(monkeypatch, ())


@pytest.mark.parametrize("index", range(len(KNOWLEDGE_NAMES)))
def test_mutation_removing_any_one_name_is_caught(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    names = KNOWLEDGE_NAMES[:index] + KNOWLEDGE_NAMES[index + 1 :]
    removed = KNOWLEDGE_NAMES[index]
    if any(other in removed for other in names):
        # A trigger name that contains a function name is refused by that function's entry too
        # (e.g. kb_chunks_insert_guard contains kb_chunks_insert): removing it changes nothing.
        run_name_check_with(monkeypatch, names)
        return
    with pytest.raises(AssertionError):
        run_name_check_with(monkeypatch, names)


def test_mutation_removing_the_sequence_name_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    names = tuple(n for n in KNOWLEDGE_NAMES if n != "kb_status_event_order_seq")
    with pytest.raises(AssertionError):
        run_name_check_with(monkeypatch, names)


def test_mutation_a_ddl_rule_that_never_matches_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    class Never:
        def search(self, text: str) -> None:
            return None

    monkeypatch.setattr(migrations_module, "_KB_TABLE_DDL", Never())
    caught = 0
    for template in TABLE_TEMPLATES:
        statement = template.format(t="kb_documents", T="KB_DOCUMENTS")
        try:
            assert refuses(statement), statement
        except AssertionError:
            caught += 1
    assert caught >= 10, "the table-DDL checks would not notice the rule being disabled"


def test_mutation_a_guard_that_never_refuses_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """`refuses` calls `migrations_module.guarded`, so patching it is seen by every check."""
    assert refuses("DROP TABLE kb_chunks")
    monkeypatch.setattr(migrations_module, "guarded", lambda known: None)
    assert not refuses("DROP TABLE kb_chunks")  # the patch really is in force
    with pytest.raises(AssertionError):
        check_every_name_is_refused()
    with pytest.raises(AssertionError):
        assert refuses("ALTER TABLE kb_documents OWNER TO someone")
    assert real_guarded is not migrations_module.guarded
