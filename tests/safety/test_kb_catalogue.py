"""ADR 0022 condition 9: the database check that the knowledge tables are exactly as migration 4
made them (`migrations.knowledge_problems`), its callers (`verify`, `migrate`) and the sha256 pins.

Offline: a fake catalogue (tests/safety/kb_catalogue_fake.py) answers the queries. It does NOT parse
SQL, so these tests prove the Python logic and the shape of the SQL text; whether the SQL itself is
right on a real server is proved only by tests/integration/test_kb_catalogue_live.py (not run
offline). Mutations are in test_kb_catalogue_mutations.py."""

from __future__ import annotations

import re
from typing import Any

import psycopg.errors
import pytest
from tests.safety import kb_catalogue_fake as fake
from tests.safety.kb_catalogue_fake import (
    SCENARIOS,
    Catalog,
    CatalogConnection,
    MigrateConnection,
    Scenario,
    VerifyConnection,
)

from divesafe.services import migrations, postgres
from divesafe.services.migrations import (
    KNOWLEDGE_FUNCTION_SHA256,
    KNOWLEDGE_FUNCTIONS,
    KNOWLEDGE_TABLES,
    KNOWLEDGE_TRIGGERS,
    MigrationError,
    knowledge_problems,
)

pytestmark = pytest.mark.safety


# --- one failure case per check (a)-(g) -----------------------------------------------------------


def test_the_scenario_table_covers_every_check_and_has_unique_ids() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    for needed in (
        "trigger-missing",
        "trigger-extra",
        "trigger-duplicate",
        "trigger-on-the-wrong-table",
        "trigger-calls-the-wrong-function",
        "trigger-function-in-the-wrong-schema",
        "trigger-enabled-state-O",
        "trigger-sha256-mismatch",
        "no-pin-for-major-15",
        "no-pin-for-major-18",
        "function-missing-from-the-pin-table",
        "rewrite-rule",
        "row-security-enabled",
        "row-security-forced",
        "policy-row",
        "inheritance-parent",
        "inheritance-child",
        "unlogged-table",
        "temporary-table",
        "wrong-table-owner",
        "wrong-public-owner",
        "public-owned-by-the-database-owner",
        "tgtype-insert-guard-fires-on-update",
        "tgtype-truncate-guard-is-per-row",
        "when-clause-on-an-insert-guard",
        "update-of-column-list",
        "trigger-arguments",
        "deferrable-trigger",
        "constraint-trigger",
        "transition-table-old",
        "transition-table-new",
        "no-trigger-type-pins",
        "trigger-type-pin-missing-for-one-trigger",
    ):
        assert needed in ids, needed


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_each_scenario_gives_exactly_its_fixed_problems(scenario: Scenario) -> None:
    found = fake.run_scenario(migrations, scenario)
    assert sorted(found) == sorted(scenario.expected)


def test_a_pristine_catalogue_has_no_problems_on_both_owner_paths() -> None:
    for current in (True, False):
        found = knowledge_problems(CatalogConnection(Catalog()), owner_is_current_user=current)
        assert found == [], current


def test_the_internal_foreign_key_triggers_are_not_counted_as_extra_triggers() -> None:
    catalog = Catalog()
    assert catalog.internal  # the fake really holds internal triggers the query must filter out
    found = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=True)
    assert found == []


def test_the_expected_owner_is_the_current_user_or_the_assessments_owner() -> None:
    catalog = Catalog(current_user="runner_test", assessments_owner="other_test")
    for table in catalog.tables.values():
        table.owner = "runner_test"
    catalog.schema_owner = "runner_test"
    catalog.db_owner = "runner_test"
    by_current = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=True)
    by_assessments = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=False)
    assert by_current == []
    assert sorted(by_assessments) == sorted(fake._all_owner())  # noqa: SLF001


# --- the problem texts are fixed -----------------------------------------------------------------


def test_no_problem_text_echoes_a_catalogue_value() -> None:
    secret = "never_echoed_value_test"
    catalog = Catalog(current_user="runner_test")
    for trigger in catalog.triggers:
        trigger.schema = secret
    catalog.triggers.append(fake.Trig("kb_chunks", secret, secret, secret, "O"))
    catalog.digests = {k: secret.ljust(64, "0")[:64] for k in catalog.digests}
    catalog.schema_owner = secret
    for table in catalog.tables.values():
        table.owner = secret
        table.persistence = "u"
        table.rls = True
    found = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=True)
    assert found
    joined = "\n".join(found)
    assert secret not in joined
    assert catalog.digests["kb_chunks_insert"] not in joined


def test_every_problem_text_names_only_fixed_objects() -> None:
    allowed = set(KNOWLEDGE_TABLES) | set(KNOWLEDGE_TRIGGERS) | set(KNOWLEDGE_FUNCTIONS.values())
    word = re.compile(r"\b(?:kb_[a-z_]+)\b")
    for scenario in SCENARIOS:
        for text in fake.run_scenario(migrations, scenario):
            for name in word.findall(text):
                assert name in allowed, (scenario.id, name)


# --- the queries ----------------------------------------------------------------------------------

WRITE = re.compile(
    r"\b(insert|update|delete|truncate|create|alter|drop|grant|revoke|set|lock|copy|call|do)\b",
    re.IGNORECASE,
)
CATALOGUES = ("pg_class", "pg_trigger", "pg_proc", "pg_namespace", "pg_rewrite", "pg_policy")
CATALOGUES += ("pg_inherits", "pg_roles", "pg_database")


def all_queries() -> list[tuple[str, Any]]:
    catalog = Catalog()
    for current in (True, False):
        knowledge_problems(CatalogConnection(catalog), owner_is_current_user=current)
    return catalog.log


def test_the_queries_are_read_only_selects() -> None:
    queries = all_queries()
    assert len(queries) >= 8
    for text, _ in queries:
        assert text.lstrip().upper().startswith("SELECT"), text[:40]
        assert WRITE.search(text) is None, text[:60]


def test_every_catalogue_is_schema_qualified() -> None:
    for text, _ in all_queries():
        for name in CATALOGUES:
            for match in re.finditer(rf"(?<![\w.]){name}\b", text):
                raise AssertionError(f"unqualified {name} at {match.start()}")
        assert "pg_catalog.current_setting" in text or "pg_catalog." in text
    for text, _ in all_queries():
        assert "public.assessments" not in text or "pg_catalog.to_regclass" in text


def test_the_table_names_travel_as_parameters_not_in_the_query_text() -> None:
    parameterised = 0
    for text, params in all_queries():
        for table in KNOWLEDGE_TABLES:
            assert table not in text, table
        if "%s" in text:
            parameterised += 1
            assert params is not None
            flat = [item for p in params for item in (p if isinstance(p, list) else [p])]
            assert set(KNOWLEDGE_TABLES) <= set(flat)
    assert parameterised >= 3


FIXED_LITERALS = (
    "'public.assessments'",
    "'server_version_num'",
    "'pg_database_owner'",
    "'public'",
    "'UTF8'",
    "'hex'",
    "= ''",  # an empty column list: tgattr::text = ''
)


def test_no_value_is_formatted_into_a_query() -> None:
    for text, _ in all_queries():
        assert "{" not in text and "}" not in text
        assert "'kb_" not in text
        rest = text
        # the only literals the queries may hold are these fixed, value-free ones
        for literal in FIXED_LITERALS:
            rest = rest.replace(literal, "")
        assert "'" not in rest, text[:80]


# --- the pins -------------------------------------------------------------------------------------

FIVE = {
    "kb_chunks_insert",
    "kb_documents_insert",
    "kb_no_truncate",
    "kb_no_update_delete",
    "kb_status_events_insert",
}


def test_the_pin_table_has_exactly_the_five_functions_for_exactly_majors_16_and_17() -> None:
    assert set(KNOWLEDGE_FUNCTION_SHA256) == {16, 17}
    assert set(KNOWLEDGE_FUNCTIONS.values()) == FIVE
    for major, pins in KNOWLEDGE_FUNCTION_SHA256.items():
        assert set(pins) == FIVE, major


def test_each_pinned_sha256_is_64_lowercase_hex_characters() -> None:
    for major, pins in KNOWLEDGE_FUNCTION_SHA256.items():
        for name, digest in pins.items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None, (major, name)


def test_the_five_functions_have_five_different_pins() -> None:
    for pins in KNOWLEDGE_FUNCTION_SHA256.values():
        assert len(set(pins.values())) == 5  # a pin copied from another function would repeat


def test_the_pin_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        KNOWLEDGE_FUNCTION_SHA256[18] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        KNOWLEDGE_FUNCTION_SHA256[16]["kb_no_truncate"] = "0" * 64  # type: ignore[index]


@pytest.mark.parametrize("major", [9, 10, 14, 15, 18, 19, 0])
def test_an_unpinned_major_fails_closed_with_one_problem_per_trigger(major: int) -> None:
    catalog = Catalog(major=major)
    found = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=True)
    assert sorted(found) == sorted(fake.no_pin(KNOWLEDGE_FUNCTIONS[n]) for n in KNOWLEDGE_TRIGGERS)


def test_an_empty_or_mutated_pin_table_fails_closed() -> None:
    for pins in ({}, {16: {}}, {16: {k: "0" * 64 for k in FIVE}}, {16: {"kb_no_truncate": "x"}}):
        scenario = Scenario("x", lambda c: None, [], pins=pins)
        found = fake.run_scenario(migrations, scenario)
        assert found, pins


def test_knowledge_function_hashes_is_read_only_and_parameterised() -> None:
    catalog = Catalog()
    major, hashes = migrations.knowledge_function_hashes(CatalogConnection(catalog))
    assert major == 16
    assert hashes == dict(KNOWLEDGE_FUNCTION_SHA256[16])
    for text, _params in catalog.log:
        assert text.lstrip().upper().startswith("SELECT")
        assert "%s" in text or "server_version_num" in text
    assert catalog.log[-1][1] == (sorted(FIVE),)


# --- verify() -------------------------------------------------------------------------------------


def repository(
    monkeypatch: pytest.MonkeyPatch, conn: VerifyConnection
) -> postgres.PostgresAssessmentRepository:
    repo = postgres.PostgresAssessmentRepository("postgresql://never-used/x_test")
    monkeypatch.setattr(repo, "_connect", lambda: conn)
    return repo


def test_verify_reports_nothing_for_an_intact_knowledge_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = VerifyConnection(Catalog())
    problems = repository(monkeypatch, conn).verify(least_privilege=False)
    assert problems == []
    assert conn.transaction_log.entered >= 2  # the knowledge detail runs in its own transaction


def test_verify_reports_a_broken_knowledge_guard_with_its_fixed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = Catalog(rules=1)
    problems = repository(monkeypatch, VerifyConnection(catalog)).verify(least_privilege=False)
    assert problems == [fake.RULE]


def test_verify_runs_the_expected_owner_as_the_assessments_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = Catalog(current_user="app_test")  # verify may run as another role: not the owner
    problems = repository(monkeypatch, VerifyConnection(catalog)).verify(least_privilege=False)
    assert problems == []


@pytest.mark.parametrize("missing", [("kb_chunks",), tuple(KNOWLEDGE_TABLES)])
def test_verify_with_a_missing_knowledge_table_gives_only_the_does_not_exist_problems(
    monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...]
) -> None:
    catalog = Catalog(rules=1, policies=1)  # would be reported if the detail were not skipped
    conn = VerifyConnection(catalog, missing=missing)
    problems = repository(monkeypatch, conn).verify(least_privilege=False)
    expected = [f"the {t} table does not exist (run db_init as the owner role)" for t in missing]
    assert [p for p in problems if "knowledge" in p or "kb_" in p] == expected
    assert catalog.log == []  # the detailed checks were skipped
    assert fake.RULE not in problems


def test_verify_reports_a_fixed_text_when_the_application_role_cannot_read_the_catalogues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = Catalog(deny=True)
    problems = repository(monkeypatch, VerifyConnection(catalog)).verify(least_privilege=False)
    assert problems == ["the application role cannot read the knowledge table definitions"]
    assert "denied" not in "".join(problems)


def test_verify_asks_the_knowledge_question_before_the_assessments_guard_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = VerifyConnection(Catalog())
    repository(monkeypatch, conn).verify(least_privilege=False)
    kb_at = next(i for i, c in enumerate(conn.calls) if "pg_catalog.pg_trigger" in c)
    assessments_at = next(i for i, c in enumerate(conn.calls) if "FROM pg_trigger t" in c)
    assert kb_at < assessments_at


# --- migrate() ------------------------------------------------------------------------------------


def install_migrate(monkeypatch: pytest.MonkeyPatch, catalog: Catalog) -> MigrateConnection:
    conn = MigrateConnection(catalog)
    monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, **k: conn)
    return conn


def test_migrate_passes_on_an_intact_catalogue_and_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = install_migrate(monkeypatch, Catalog())
    applied = migrations.migrate("postgresql://never-used/x_test")
    assert applied == []
    assert conn.exited is True
    assert conn.exit_type is None  # left normally: the transaction commits


@pytest.mark.parametrize(
    "scenario",
    [s for s in SCENARIOS if s.expected and s.current],
    ids=[s.id for s in SCENARIOS if s.expected and s.current],
)
def test_migrate_raises_on_any_problem_and_the_transaction_rolls_back(
    monkeypatch: pytest.MonkeyPatch, scenario: Scenario
) -> None:
    catalog = Catalog()
    scenario.mutate(catalog)
    conn = install_migrate(monkeypatch, catalog)
    if scenario.pins is not None:
        monkeypatch.setattr(migrations, "KNOWLEDGE_FUNCTION_SHA256", scenario.pins)
    if scenario.type_pins is not None:
        monkeypatch.setattr(migrations, "KNOWLEDGE_TRIGGER_TGTYPE", scenario.type_pins)
    with pytest.raises(MigrationError) as caught:
        migrations.migrate("postgresql://never-used/x_test")
    message = str(caught.value)
    assert message.startswith("the knowledge guard is not intact after migrating")
    for problem in scenario.expected:
        assert problem in message
    assert conn.exited is True
    assert (
        conn.exit_type is MigrationError
    )  # the exception passed through the transaction: ROLLBACK


def test_migrate_checks_the_owner_as_the_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = Catalog(assessments_owner="other_test")  # would fail the verify() path
    install_migrate(monkeypatch, catalog)
    assert migrations.migrate("postgresql://never-used/x_test") == []
    catalog.current_user = "someone_else_test"
    install_migrate(monkeypatch, catalog)
    with pytest.raises(MigrationError):
        migrations.migrate("postgresql://never-used/x_test")


def test_the_migrate_refusal_does_not_echo_the_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    install_migrate(monkeypatch, Catalog(rules=1))
    with pytest.raises(MigrationError) as caught:
        migrations.migrate("postgresql://u:never-echoed@host/x_test")
    assert "never-echoed" not in str(caught.value)
    assert fake.RULE in str(caught.value)


def test_the_database_error_from_the_knowledge_check_is_not_a_migration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalogue read refused inside migrate() surfaces as the repository's fixed error, never as
    a pass. migrate() catches psycopg.Error (InsufficientPrivilege is one) and re-raises
    RepositoryUnavailableError with a fixed text, and the fake's `with` block sees that exception
    (a rollback), so the raw psycopg error never escapes."""
    from divesafe.services.repository import RepositoryUnavailableError

    conn = install_migrate(monkeypatch, Catalog(deny=True))
    with pytest.raises(RepositoryUnavailableError) as caught:
        migrations.migrate("postgresql://never-used/x_test")
    assert str(caught.value) == "could not apply the schema migrations"
    assert "denied" not in str(caught.value)
    assert conn.exit_type is psycopg.errors.InsufficientPrivilege  # rolled back, not committed


# --- the trigger-type pins and the trigger shape --------------------------------------------------

TYPES = migrations.KNOWLEDGE_TRIGGER_TGTYPE


def test_the_trigger_type_pins_are_exactly_the_nine_triggers_with_the_three_values() -> None:
    assert set(TYPES) == set(KNOWLEDGE_TRIGGERS)
    assert len(TYPES) == 9
    assert set(TYPES.values()) == {7, 27, 34}
    for name, value in TYPES.items():
        assert type(value) is int, name
        # written out again in the fake, from the CREATE TRIGGER text, not copied from the pin table
        assert value == fake.expected_tgtype(name), name


def test_each_kind_of_guard_has_its_own_trigger_type() -> None:
    kinds = {"_insert_guard": 7, "_append_only_guard": 27, "_no_truncate_guard": 34}
    for suffix, value in kinds.items():
        matching = [n for n in TYPES if n.endswith(suffix)]
        assert len(matching) == 3, suffix
        assert {TYPES[n] for n in matching} == {value}, suffix


def test_the_trigger_type_pin_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        TYPES["kb_chunks_insert_guard"] = 19  # type: ignore[index]
    with pytest.raises(TypeError):
        TYPES["extra"] = 7  # type: ignore[index]


def test_the_function_pins_are_identical_on_majors_16_and_17() -> None:
    assert dict(KNOWLEDGE_FUNCTION_SHA256[16]) == dict(KNOWLEDGE_FUNCTION_SHA256[17])


def test_knowledge_trigger_types_reads_the_non_internal_triggers_with_the_table_names() -> None:
    catalog = Catalog()
    found = migrations.knowledge_trigger_types(CatalogConnection(catalog))
    assert found == {n: fake.expected_tgtype(n) for n in KNOWLEDGE_TRIGGERS}
    assert catalog.log[-1][1] == (list(KNOWLEDGE_TABLES),)
    text = catalog.log[-1][0]
    assert text.lstrip().upper().startswith("SELECT")
    assert "NOT t.tgisinternal" in text


def test_the_shipped_trigger_types_equal_the_pin_table_on_a_pristine_catalogue() -> None:
    catalog = Catalog()
    found = migrations.knowledge_trigger_types(CatalogConnection(catalog))
    assert found == dict(TYPES)


def test_a_trigger_type_or_shape_problem_does_not_echo_the_catalogue_value() -> None:
    catalog = Catalog()
    for trigger in catalog.triggers:
        trigger.tgtype = 987654
        trigger.nargs = 424242
        trigger.constraint = 777777
        trigger.oldtable = "never_echoed_table_test"
    found = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=True)
    joined = "\n".join(found)
    assert found
    for leaked in ("987654", "424242", "777777", "never_echoed_table_test"):
        assert leaked not in joined


def test_every_trigger_gets_a_type_and_a_shape_check_even_when_the_function_pin_is_fine() -> None:
    for name in KNOWLEDGE_TRIGGERS:
        catalog = Catalog()
        trig = next(t for t in catalog.triggers if t.name == name)
        trig.tgtype = 1
        found = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=True)
        assert found == [fake.not_type(name)], name
        catalog = Catalog()
        trig = next(t for t in catalog.triggers if t.name == name)
        trig.qual = True
        found = knowledge_problems(CatalogConnection(catalog), owner_is_current_user=True)
        assert found == [fake.not_plain(name)], name
