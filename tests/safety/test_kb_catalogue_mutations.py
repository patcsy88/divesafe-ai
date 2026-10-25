"""Mutations of ADR 0022 condition 9 (`knowledge_problems`, `migrate`, `verify`). Each runs on a
COPY of the module source (`mutated_module`), never on `src`, and must make a test fail: either a
scenario of tests/safety/kb_catalogue_fake.py gets a wrong answer (or crashes: a crash is not a
refusal), or the migrate()/verify() behaviour differs.

EQUIVALENT MUTANTS (deliberately not tested; the offline fake cannot tell them apart, and on a real
server they behave the same, so there is nothing to catch):
  - dropping `AND {expected_owner} IS NOT NULL` from the table-owner expression, and dropping
    `{expected_owner} IS NOT NULL AND` from the schema expression: `relowner` and `nspowner` are
    never NULL, so a NULL expected owner already makes the comparison false / NULL (NULL is not
    True), which is refused either way.
  - `ORDER BY` / sorting in `knowledge_function_hashes` (a dev tool; the dict has no order).
  - `str(...)` conversions of catalogue values.
  - the `if major_row else {}` fallback: `current_setting` always returns a row.
The SQL TEXT itself (joins, the pg_database_owner clause) is proved only by the live file."""

from __future__ import annotations

from typing import Any

import pytest
from tests.safety import kb_catalogue_fake as fake
from tests.safety.kb_catalogue_fake import SCENARIOS, Catalog, MigrateConnection, VerifyConnection
from tests.safety.kb_fakes import mutated_module

from divesafe.services import migrations, postgres
from divesafe.services.migrations import MigrationError

pytestmark = pytest.mark.safety

TRIGGER_SET = "    if len(found) != len(set(found)) or set(found) != set(KNOWLEDGE_TRIGGERS):"
PAIRING = '        if str(proname) != function or str(nspname) != "public":'
ENABLED = '        if str(enabled) != "A":'
WRONG_TABLE = (
    '        if str(relname) != next((t for t in KNOWLEDGE_TABLES if name.startswith(f"{t}_")), '
    "None):"
)
PIN_MISSING = "        if pinned is None:"
PIN_COMPARE = "        elif str(digest) != pinned:"
PIN_LOOKUP = "        pinned = pins.get(function)"
MAJOR = (
    "    pins = KNOWLEDGE_FUNCTION_SHA256.get(int(major_row[0]) // 10000, {}) if major_row else {}"
)
RULES = "    if counts is None or int(counts[0]) != 0:"
POLICIES = "    if counts is None or int(counts[1]) != 0:"
INHERITS = "    if counts is None or int(counts[2]) != 0:"
INHERIT_SQL = '        f"WHERE inhparent IN {in_kb} OR inhrelid IN {in_kb})",'
ROW_SECURITY = "        if rls is not False or forced is not False:"
PERSISTENCE = '        if str(persistence) != "p":'
OWNER_OK = "        if owner_ok is not True:"
PUBLIC_CHECK = "    if schema_owner is None or schema_owner[0] is not True:"
DB_OWNER_ROLE = "WHERE rolname = 'pg_database_owner') "
DB_OWNER_EQ = 'f"WHERE datname = pg_catalog.current_database()) = {expected_owner})) "'
UNREADABLE = "    if {str(r[0]) for r in state} != set(KNOWLEDGE_TABLES):"
INTERNAL = 'f"WHERE NOT t.tgisinternal AND t.tgrelid IN {in_kb}",'
OWNER_SWITCH = "        if owner_is_current_user"
TYPE_MISSING = "        if expected_type is None:"
TYPE_COMPARE = "        elif int(tgtype) != expected_type:"
TYPE_LOOKUP = "        expected_type = KNOWLEDGE_TRIGGER_TGTYPE.get(name)"
SHAPE_CHECK = "        if plain is not True:"
SHAPE_QUAL = "        \"(t.tgqual IS NULL AND t.tgattr::pg_catalog.text = '' AND t.tgnargs = 0 \""
SHAPE_DEFER = '        "AND NOT t.tgdeferrable AND t.tgconstraint = 0 "'
SHAPE_TABLES = '        "AND t.tgoldtable IS NULL AND t.tgnewtable IS NULL) "'
TRIGGERS_LINE = '    "kb_chunks_no_truncate_guard",\n    "kb_status_events_no_truncate_guard",\n)'

KNOWLEDGE_MUTATIONS = [
    pytest.param(TRIGGER_SET, "    if False:", id="a-trigger-set-check-removed"),
    pytest.param(
        TRIGGER_SET,
        "    if not (len(found) != len(set(found)) or set(found) != set(KNOWLEDGE_TRIGGERS)):",
        id="a-trigger-set-check-inverted",
    ),
    pytest.param(
        TRIGGER_SET,
        "    if set(found) != set(KNOWLEDGE_TRIGGERS):",
        id="a-duplicates-allowed",
    ),
    pytest.param(
        TRIGGER_SET,
        "    if len(found) != len(set(found)) or not set(found) >= set(KNOWLEDGE_TRIGGERS):",
        id="a-extra-triggers-allowed",
    ),
    pytest.param(
        TRIGGER_SET,
        "    if len(found) != len(set(found)) or not set(found) <= set(KNOWLEDGE_TRIGGERS):",
        id="a-missing-triggers-allowed",
    ),
    pytest.param(
        TRIGGER_SET,
        "    if len(found) != len(set(found)) or set(found) != set(KNOWLEDGE_TRIGGERS[1:]):",
        id="a-one-trigger-dropped-from-the-expected-set",
    ),
    pytest.param(
        '    "kb_status_events_no_truncate_guard",\n)\n# The function',
        ")\n# The function",
        id="a-last-trigger-dropped-from-the-constant",
    ),
    pytest.param(PAIRING, "        if False:", id="a-function-pairing-skipped"),
    pytest.param(
        PAIRING, '        if str(nspname) != "public":', id="a-function-name-not-compared"
    ),
    pytest.param(
        PAIRING, "        if str(proname) != function:", id="a-function-schema-not-compared"
    ),
    pytest.param(WRONG_TABLE, "        if False:", id="a-wrong-table-check-removed"),
    pytest.param(ENABLED, "        if False:", id="a-enable-always-check-removed"),
    pytest.param(ENABLED, '        if str(enabled) not in ("A", "O"):', id="a-origin-accepted"),
    pytest.param(ENABLED, '        if str(enabled) == "D":', id="a-only-disabled-refused"),
    pytest.param(INTERNAL, 'f"WHERE t.tgrelid IN {in_kb}",', id="a-internal-triggers-counted"),
    pytest.param(PIN_COMPARE, "        elif False:", id="b-sha256-compare-removed"),
    pytest.param(
        PIN_COMPARE, "        elif str(digest) == pinned:", id="b-sha256-compare-inverted"
    ),
    pytest.param(
        PIN_LOOKUP,
        '        pinned = pins.get("kb_no_truncate")',
        id="b-sha256-of-the-wrong-function",
    ),
    pytest.param(
        PIN_LOOKUP, "        pinned = pins.get(function, str(digest))", id="b-missing-pin-is-a-pass"
    ),
    pytest.param(PIN_MISSING, "        if False:", id="b-missing-pin-not-reported"),
    pytest.param(
        MAJOR,
        "    pins = KNOWLEDGE_FUNCTION_SHA256.get(int(major_row[0]) // 10000, "
        "KNOWLEDGE_FUNCTION_SHA256[16]) if major_row else {}",
        id="b-unknown-major-uses-the-16-pins",
    ),
    pytest.param(
        MAJOR,
        "    pins = KNOWLEDGE_FUNCTION_SHA256.get(int(major_row[0]) // 100, {}) "
        "if major_row else {}",
        id="b-major-computed-wrongly",
    ),
    pytest.param(RULES, "    if False:", id="c-rewrite-rule-check-removed"),
    pytest.param(
        RULES, "    if counts is None or int(counts[0]) == 0:", id="c-rewrite-rule-inverted"
    ),
    pytest.param(
        RULES, "    if counts is not None and int(counts[0]) != 0:", id="c-unreadable-counts-pass"
    ),
    pytest.param(POLICIES, "    if False:", id="d-policy-check-removed"),
    pytest.param(
        POLICIES, "    if counts is None or int(counts[1]) == 0:", id="d-policy-check-inverted"
    ),
    pytest.param(ROW_SECURITY, "        if False:", id="d-row-security-check-removed"),
    pytest.param(ROW_SECURITY, "        if rls is not False:", id="d-relforcerowsecurity-ignored"),
    pytest.param(ROW_SECURITY, "        if forced is not False:", id="d-relrowsecurity-ignored"),
    pytest.param(
        ROW_SECURITY,
        "        if rls is not False and forced is not False:",
        id="d-both-flags-needed",
    ),
    pytest.param(INHERITS, "    if False:", id="e-inheritance-check-removed"),
    pytest.param(
        INHERIT_SQL, '        f"WHERE inhparent IN {in_kb})",', id="e-inherits-child-side-ignored"
    ),
    pytest.param(
        INHERIT_SQL, '        f"WHERE inhrelid IN {in_kb})",', id="e-inherits-parent-side-ignored"
    ),
    pytest.param(PERSISTENCE, "        if False:", id="f-persistence-check-removed"),
    pytest.param(
        PERSISTENCE, '        if str(persistence) not in ("p", "u"):', id="f-unlogged-accepted"
    ),
    pytest.param(
        PERSISTENCE, '        if str(persistence) not in ("p", "t"):', id="f-temporary-accepted"
    ),
    pytest.param(OWNER_OK, "        if False:", id="g-table-owner-check-removed"),
    pytest.param(OWNER_OK, "        if owner_ok is True:", id="g-table-owner-check-inverted"),
    pytest.param(PUBLIC_CHECK, "    if False:", id="g-public-owner-check-removed"),
    pytest.param(
        PUBLIC_CHECK,
        "    if schema_owner is None or schema_owner[0] is True:",
        id="g-public-owner-check-inverted",
    ),
    pytest.param(
        DB_OWNER_ROLE, "WHERE rolname = 'nobody_test') ", id="g-database-owner-form-not-accepted"
    ),
    pytest.param(
        DB_OWNER_EQ,
        'f"WHERE datname = pg_catalog.current_database()) IS NOT NULL)) "',
        id="g-any-database-owner-accepted",
    ),
    pytest.param(OWNER_SWITCH, "        if not owner_is_current_user", id="g-owner-paths-swapped"),
    pytest.param(UNREADABLE, "    if False:", id="g-unreadable-table-not-reported"),
    # trigger type (tgtype) and plain shape
    pytest.param(TYPE_COMPARE, "        elif False:", id="t-type-compare-removed"),
    pytest.param(
        TYPE_COMPARE, "        elif int(tgtype) == expected_type:", id="t-type-compare-inverted"
    ),
    pytest.param(
        TYPE_COMPARE,
        "        elif int(tgtype) & expected_type != expected_type:",
        id="t-type-bits-subset-accepted",
    ),
    pytest.param(
        TYPE_COMPARE,
        "        elif int(tgtype) & 1 != expected_type & 1:",
        id="t-only-the-row-bit-compared",
    ),
    pytest.param(
        TYPE_COMPARE,
        "        elif int(tgtype) & 2 != expected_type & 2:",
        id="t-only-the-timing-bit-compared",
    ),
    pytest.param(
        TYPE_COMPARE,
        "        elif int(tgtype) & 28 != expected_type & 28:",
        id="t-only-the-event-bits-compared",
    ),
    pytest.param(
        TYPE_COMPARE,
        "        elif int(tgtype) & 31 != expected_type & 31:",
        id="t-truncate-bit-ignored",
    ),
    pytest.param(
        TYPE_COMPARE, "        elif int(tgtype) < expected_type:", id="t-type-compared-by-size"
    ),
    pytest.param(
        TYPE_COMPARE,
        "        elif int(tgtype) & 63 != expected_type & 63:",
        id="t-instead-bit-ignored",
    ),
    pytest.param(TYPE_MISSING, "        if False:", id="t-missing-type-pin-not-reported"),
    pytest.param(
        TYPE_LOOKUP,
        "        expected_type = KNOWLEDGE_TRIGGER_TGTYPE.get(name, int(tgtype))",
        id="t-missing-type-pin-is-a-pass",
    ),
    pytest.param(
        TYPE_LOOKUP,
        "        expected_type = KNOWLEDGE_TRIGGER_TGTYPE.get(name, 7)",
        id="t-missing-type-pin-defaults-to-insert",
    ),
    pytest.param(
        TYPE_LOOKUP,
        "        expected_type = KNOWLEDGE_TRIGGER_TGTYPE.get(function)",
        id="t-type-pin-looked-up-by-function",
    ),
    pytest.param(SHAPE_CHECK, "        if False:", id="s-shape-check-removed"),
    pytest.param(SHAPE_CHECK, "        if plain is True:", id="s-shape-check-inverted"),
    pytest.param(SHAPE_CHECK, "        if plain is None:", id="s-shape-null-only-refused"),
    pytest.param(
        SHAPE_QUAL,
        "        \"(t.tgattr::pg_catalog.text = '' AND t.tgnargs = 0 \"",
        id="s-when-clause-not-checked",
    ),
    pytest.param(
        SHAPE_QUAL,
        '        "(t.tgqual IS NULL AND t.tgnargs = 0 "',
        id="s-column-list-not-checked",
    ),
    pytest.param(
        SHAPE_QUAL,
        "        \"(t.tgqual IS NULL AND t.tgattr::pg_catalog.text = '' \"",
        id="s-arguments-not-checked",
    ),
    pytest.param(
        SHAPE_DEFER,
        '        "AND t.tgconstraint = 0 "',
        id="s-deferrable-not-checked",
    ),
    pytest.param(
        SHAPE_DEFER,
        '        "AND NOT t.tgdeferrable "',
        id="s-constraint-trigger-not-checked",
    ),
    pytest.param(
        SHAPE_TABLES,
        '        "AND t.tgnewtable IS NULL) "',
        id="s-old-transition-table-not-checked",
    ),
    pytest.param(
        SHAPE_TABLES,
        '        "AND t.tgoldtable IS NULL) "',
        id="s-new-transition-table-not-checked",
    ),
    pytest.param(
        SHAPE_TABLES,
        '        "AND TRUE) "',
        id="s-both-transition-tables-not-checked",
    ),
    pytest.param(
        SHAPE_QUAL, '        "(TRUE AND t.tgnargs = 0 "', id="s-two-shape-checks-not-made"
    ),
]


@pytest.mark.parametrize(("old", "new"), KNOWLEDGE_MUTATIONS)
def test_mutation_of_knowledge_problems_is_caught_by_a_scenario(old: str, new: str) -> None:
    real = fake.failing_scenarios(migrations)
    mutant = mutated_module(migrations, (old, new))
    caught = fake.failing_scenarios(mutant)
    assert real == []
    assert caught != [], "no scenario noticed the mutation"


def test_mutation_the_wrong_table_check_is_noticed_only_by_its_own_scenario() -> None:
    mutant = mutated_module(migrations, (WRONG_TABLE, "        if False:"))
    caught = fake.failing_scenarios(mutant)
    assert caught == ["trigger-on-the-wrong-table"]


# --- migrate() and verify() -----------------------------------------------------------------------


def migrate_misses(module: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Scenarios in which `module.migrate` does not raise (or does not roll back) when it must, or
    raises when the catalogue is intact."""
    bad = []
    for scenario in [s for s in SCENARIOS if s.current]:
        catalog = Catalog()
        scenario.mutate(catalog)
        conn = MigrateConnection(catalog)
        monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, _c=conn, **k: _c)
        monkeypatch.setattr(
            module,
            "KNOWLEDGE_FUNCTION_SHA256",
            scenario.pins if scenario.pins is not None else migrations.KNOWLEDGE_FUNCTION_SHA256,
        )
        monkeypatch.setattr(
            module,
            "KNOWLEDGE_TRIGGER_TGTYPE",
            scenario.type_pins
            if scenario.type_pins is not None
            else migrations.KNOWLEDGE_TRIGGER_TGTYPE,
        )
        try:
            module.migrate("postgresql://never-used/x_test")
            raised: type[BaseException] | None = None
        except MigrationError as exc:
            raised = type(exc)
            if not str(exc).startswith("the knowledge guard is not intact after migrating: "):
                bad.append(scenario.id)
                continue
        except Exception:  # noqa: BLE001
            bad.append(scenario.id)
            continue
        if scenario.expected and (raised is None or conn.exit_type is not MigrationError):
            bad.append(scenario.id)
        if not scenario.expected and (raised is not None or conn.exit_type is not None):
            bad.append(scenario.id)
    return bad


MIGRATE_MUTATIONS = [
    pytest.param(
        "                if kb_problems:", "                if False:", id="m-problems-ignored"
    ),
    pytest.param(
        "                if kb_problems:", "                if not kb_problems:", id="m-inverted"
    ),
    pytest.param(
        "                kb_problems = knowledge_problems(conn, owner_is_current_user=True)",
        "                kb_problems = knowledge_problems(conn, owner_is_current_user=False)",
        id="m-owner-is-assessments-owner",
    ),
    pytest.param(
        "                kb_problems = knowledge_problems(conn, owner_is_current_user=True)",
        "                kb_problems = []",
        id="m-check-not-called",
    ),
    pytest.param(
        "            if len(known) >= KNOWLEDGE_MIGRATION:", "            if False:", id="m-skipped"
    ),
    pytest.param(
        '                        "the knowledge guard is not intact after migrating: "\n'
        '                        + "; ".join(kb_problems)',
        '                        "the knowledge guard is not intact after migrating"',
        id="m-problems-not-listed",
    ),
]


def test_migrate_copy_without_a_mutation_passes_the_same_checker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert migrate_misses(migrations, monkeypatch) == []


@pytest.mark.parametrize(("old", "new"), MIGRATE_MUTATIONS)
def test_mutation_of_migrate_is_caught(monkeypatch: pytest.MonkeyPatch, old: str, new: str) -> None:
    mutant = mutated_module(migrations, (old, new))
    missed = migrate_misses(mutant, monkeypatch)
    assert missed != []


def verify_with(module: Any, catalog: Catalog, missing: tuple[str, ...] = ()) -> tuple[Any, Any]:
    conn = VerifyConnection(catalog, missing)
    repo = module.PostgresAssessmentRepository("postgresql://never-used/x_test")
    repo._connect = lambda: conn  # noqa: SLF001
    try:
        found: Any = repo.verify(least_privilege=False)
    except Exception as exc:  # noqa: BLE001
        found = type(exc).__name__
    return found, catalog


def verify_misses(module: Any) -> list[str]:
    bad = []
    found, _ = verify_with(module, Catalog())
    if found != []:
        bad.append("intact")
    found, _ = verify_with(module, Catalog(rules=1))
    if found != [fake.RULE]:
        bad.append("broken")
    found, _ = verify_with(module, Catalog(current_user="app_test"))
    if found != []:
        bad.append("other-role-is-judged-by-the-assessments-owner")
    found, catalog = verify_with(module, Catalog(rules=1), ("kb_chunks",))
    if catalog.log or fake.RULE in found:
        bad.append("missing-table-skips-detail")
    found, _ = verify_with(module, Catalog(deny=True))
    if found != ["the application role cannot read the knowledge table definitions"]:
        bad.append("insufficient-privilege")
    return bad


VERIFY_MUTATIONS = [
    pytest.param(
        "            problems += self._knowledge_problems(conn)\n",
        "            problems += []\n",
        id="v-not-called",
    ),
    pytest.param(
        "return knowledge_problems(conn, owner_is_current_user=False, read_only=True)",
        "return knowledge_problems(conn, owner_is_current_user=True, read_only=True)",
        id="v-owner-is-the-current-user",
    ),
    pytest.param(
        "        if missing:\n            return missing\n", "", id="v-missing-detail-not-skipped"
    ),
    pytest.param(
        "        except psycopg.errors.InsufficientPrivilege:\n"
        '            return ["the application role cannot read the knowledge table definitions"]',
        "        except psycopg.errors.InsufficientPrivilege:\n            return []",
        id="v-insufficient-privilege-passes",
    ),
    pytest.param(
        "        except psycopg.errors.InsufficientPrivilege:\n"
        '            return ["the application role cannot read the knowledge table definitions"]',
        "        except ZeroDivisionError:\n            return []",
        id="v-insufficient-privilege-crashes",
    ),
]


def test_verify_checker_passes_on_the_real_module() -> None:
    assert verify_misses(postgres) == []


@pytest.mark.parametrize(("old", "new"), VERIFY_MUTATIONS)
def test_mutation_of_verify_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(postgres, (old, new))
    assert verify_misses(mutant) != []


def knowledge_comes_first(module: Any) -> bool:
    conn = VerifyConnection(Catalog())
    repo = module.PostgresAssessmentRepository("postgresql://never-used/x_test")
    repo._connect = lambda: conn  # noqa: SLF001
    repo.verify(least_privilege=False)
    kb_at = next(i for i, c in enumerate(conn.calls) if "pg_catalog.pg_trigger" in c)
    assessments_at = next(i for i, c in enumerate(conn.calls) if "FROM pg_trigger t" in c)
    return kb_at < assessments_at


def test_mutation_verify_asks_the_knowledge_question_after_the_assessments_check() -> None:
    call = "            problems += self._knowledge_problems(conn)\n"
    later = (
        "            mode = conn.execute(\"SELECT current_setting('session_replication_role')\")"
    )
    mutant = mutated_module(postgres, (call, ""), (later, call + later))
    assert knowledge_comes_first(postgres) is True
    assert knowledge_comes_first(mutant) is False
