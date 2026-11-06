"""Mutations of ADR 0022 conditions 9 and 11 (`knowledge_problems`, `migrate`, `verify`). Each runs
on a COPY of the module source (`mutated_module`), never on `src`, and must make a test fail: either
a scenario of tests/safety/kb_catalogue_fake.py gets a wrong answer (or crashes: a crash is not a
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


# Same for condition 9: the exact scenarios that catch each narrow mutation.
KNOWLEDGE_CATCHERS: dict[str, list[str]] = {
    "a-trigger-set-check-removed": [
        "trigger-missing",
        "last-trigger-missing",
        "trigger-extra",
        "trigger-duplicate",
        "extra-trigger-shape-is-not-judged",
    ],
    "a-duplicates-allowed": [
        "trigger-duplicate",
    ],
    "a-extra-triggers-allowed": [
        "trigger-extra",
        "extra-trigger-shape-is-not-judged",
    ],
    "a-missing-triggers-allowed": [
        "trigger-missing",
        "last-trigger-missing",
    ],
    "a-function-pairing-skipped": [
        "trigger-calls-the-wrong-function",
        "trigger-function-in-the-wrong-schema",
    ],
    "a-function-name-not-compared": [
        "trigger-calls-the-wrong-function",
    ],
    "a-function-schema-not-compared": [
        "trigger-function-in-the-wrong-schema",
    ],
    "a-wrong-table-check-removed": [
        "trigger-on-the-wrong-table",
    ],
    "a-enable-always-check-removed": [
        "trigger-enabled-state-O",
        "trigger-enabled-state-D",
        "trigger-enabled-state-R",
    ],
    "a-origin-accepted": [
        "trigger-enabled-state-O",
    ],
    "a-only-disabled-refused": [
        "trigger-enabled-state-O",
        "trigger-enabled-state-R",
    ],
    "b-sha256-compare-removed": [
        "trigger-calls-the-wrong-function",
        "trigger-sha256-mismatch",
        "shared-function-sha256-mismatch",
    ],
    "b-missing-pin-is-a-pass": [
        "no-pin-for-major-15",
        "no-pin-for-major-18",
        "empty-pin-table",
        "function-missing-from-the-pin-table",
    ],
    "b-missing-pin-not-reported": [
        "no-pin-for-major-15",
        "no-pin-for-major-18",
        "empty-pin-table",
        "function-missing-from-the-pin-table",
    ],
    "b-unknown-major-uses-the-16-pins": [
        "no-pin-for-major-15",
        "no-pin-for-major-18",
        "empty-pin-table",
    ],
    "c-rewrite-rule-check-removed": [
        "rewrite-rule",
        "counts-unreadable-fails-closed",
    ],
    "c-unreadable-counts-pass": [
        "counts-unreadable-fails-closed",
    ],
    "d-policy-check-removed": [
        "policy-row",
        "counts-unreadable-fails-closed",
    ],
    "d-row-security-check-removed": [
        "row-security-enabled",
        "row-security-forced",
        "row-security-forced-and-enabled",
    ],
    "d-relforcerowsecurity-ignored": [
        "row-security-forced",
    ],
    "d-relrowsecurity-ignored": [
        "row-security-enabled",
    ],
    "d-both-flags-needed": [
        "row-security-enabled",
        "row-security-forced",
    ],
    "e-inheritance-check-removed": [
        "inheritance-parent",
        "inheritance-child",
        "counts-unreadable-fails-closed",
    ],
    "e-inherits-child-side-ignored": [
        "inheritance-child",
    ],
    "e-inherits-parent-side-ignored": [
        "inheritance-parent",
    ],
    "f-persistence-check-removed": [
        "unlogged-table",
        "temporary-table",
    ],
    "f-unlogged-accepted": [
        "unlogged-table",
    ],
    "f-temporary-accepted": [
        "temporary-table",
    ],
    "g-table-owner-check-removed": [
        "current-user-path-uses-current-user",
        "false-path-uses-the-assessments-owner",
        "false-path-without-assessments-owner",
        "wrong-table-owner",
    ],
    "g-public-owner-check-removed": [
        "current-user-path-uses-current-user",
        "false-path-uses-the-assessments-owner",
        "false-path-without-assessments-owner",
        "wrong-public-owner",
        "public-owned-by-a-database-owner-who-is-not-expected",
    ],
    "g-database-owner-form-not-accepted": [
        "public-owned-by-the-database-owner",
        "public-owned-by-the-database-owner-false-path",
    ],
    "g-any-database-owner-accepted": [
        "public-owned-by-a-database-owner-who-is-not-expected",
    ],
    "g-owner-paths-swapped": [
        "pristine-false-path",
        "current-user-path-uses-current-user",
        "false-path-uses-the-assessments-owner",
        "false-path-without-assessments-owner",
        "public-owned-by-the-database-owner-false-path",
    ],
    "g-unreadable-table-not-reported": [
        "table-unreadable",
    ],
    "t-type-bits-subset-accepted": [
        "tgtype-truncate-guard-is-per-row",
        "tgtype-instead-bit-set-only",
        "tgtype-extra-event-bit-only",
    ],
    "t-only-the-event-bits-compared": [
        "tgtype-insert-guard-is-after",
        "tgtype-truncate-guard-is-per-row",
        "tgtype-truncate-bit-missing-only",
        "tgtype-instead-bit-set-only",
        "tgtype-row-bit-missing-only",
        "tgtype-before-bit-missing-only",
    ],
    "t-truncate-bit-ignored": [
        "tgtype-truncate-bit-missing-only",
        "tgtype-instead-bit-set-only",
    ],
    "t-type-compared-by-size": [
        "tgtype-insert-guard-fires-on-update",
        "tgtype-truncate-guard-is-per-row",
        "tgtype-instead-bit-set-only",
        "tgtype-extra-event-bit-only",
        "wrong-type-and-wrong-shape-give-both-problems",
    ],
    "t-instead-bit-ignored": [
        "tgtype-instead-bit-set-only",
    ],
    "t-missing-type-pin-not-reported": [
        "no-trigger-type-pins",
        "trigger-type-pin-missing-for-one-trigger",
        "missing-type-pin-fails-closed-even-for-a-wrong-type",
    ],
    "t-missing-type-pin-is-a-pass": [
        "no-trigger-type-pins",
        "trigger-type-pin-missing-for-one-trigger",
        "missing-type-pin-fails-closed-even-for-a-wrong-type",
    ],
    "t-missing-type-pin-defaults-to-insert": [
        "no-trigger-type-pins",
        "trigger-type-pin-missing-for-one-trigger",
        "missing-type-pin-fails-closed-even-for-a-wrong-type",
    ],
    "s-when-clause-not-checked": [
        "when-clause-on-an-insert-guard",
        "when-clause-on-a-truncate-guard",
        "wrong-type-and-wrong-shape-give-both-problems",
    ],
    "s-column-list-not-checked": [
        "update-of-column-list",
    ],
    "s-arguments-not-checked": [
        "trigger-arguments",
    ],
    "s-deferrable-not-checked": [
        "deferrable-trigger",
    ],
    "s-constraint-trigger-not-checked": [
        "constraint-trigger",
    ],
    "s-old-transition-table-not-checked": [
        "transition-table-old",
    ],
    "s-new-transition-table-not-checked": [
        "transition-table-new",
    ],
    "s-both-transition-tables-not-checked": [
        "transition-table-old",
        "transition-table-new",
    ],
    "s-two-shape-checks-not-made": [
        "when-clause-on-an-insert-guard",
        "when-clause-on-a-truncate-guard",
        "update-of-column-list",
        "wrong-type-and-wrong-shape-give-both-problems",
    ],
}


# condition 11 (shape pins, sequence, functions, SECURITY DEFINER wrappers, table and column ACLs)
SEQ_ACL_SQL = '        "WHERE x.grantee <> c.relowner) "'
SEQ_OWNER_SQL = (
    '        f"c.relowner IS NOT DISTINCT FROM {expected_owner} AND {expected_owner} IS NOT NULL, "'
)
FN_OWNER_SQL = (
    '        f"p.proowner IS NOT DISTINCT FROM {expected_owner} AND {expected_owner} IS NOT NULL, "'
)
FN_ACL_NULL = (
    '        "p.proacl IS NOT NULL AND NOT EXISTS '
    '(SELECT 1 FROM pg_catalog.aclexplode(p.proacl) x "'
)
FN_ACL_SQL = '        "WHERE x.grantee <> p.proowner) "'
DEFINER_MEMBER = (
    '        f"AND pg_catalog.pg_has_role(p.proowner, {expected_owner}, %s) IS NOT FALSE "'
)
DEFINER_GUARDS = '        f"AND p.oid NOT IN {called}",'
DEFINER_CHECK = "    if wrappers is None or int(wrappers[0]) != 0:"
TABLE_ACL_SQL = '        "WHERE x.grantee <> c.relowner AND (x.grantee = 0 OR x.is_grantable "'
COLUMN_ACL_SQL = (
    '        "WHERE a.attrelid = c.oid AND x.grantee <> c.relowner AND (x.grantee = 0 "'
)
COLUMN_ACL_TAIL = '        "OR x.is_grantable OR x.privilege_type <> ALL(%s::text[]))) "'
ACL_PARAMS = "        (list(_KB_TABLE_GRANTS), list(_KB_COLUMN_GRANTS), tables),"

CONDITION_11_MUTATIONS = [
    pytest.param(
        "    problems += _shape_problems(", "    _ = _shape_problems(", id="k-shape-check-unwired"
    ),
    pytest.param(
        "    problems += _sequence_problems(",
        "    _ = _sequence_problems(",
        id="k-sequence-check-unwired",
    ),
    pytest.param(
        "    problems += _function_problems(",
        "    _ = _function_problems(",
        id="k-function-check-unwired",
    ),
    pytest.param(
        "    problems += _acl_problems(", "    _ = _acl_problems(", id="k-acl-check-unwired"
    ),
    # 11.1 / 11.2 shape digests
    pytest.param(
        '            joined = "\\n".join(sorted(lines)).encode()',
        '            joined = "\\n".join(lines).encode()',
        id="k-rows-not-sorted",
    ),
    pytest.param(
        "json.dumps(list(row[1:]),", "json.dumps(list(row[2:]),", id="k-first-column-not-hashed"
    ),
    pytest.param(
        "json.dumps(list(row[1:]),", "json.dumps(list(row[1:-1]),", id="k-last-column-not-hashed"
    ),
    pytest.param(
        "            if str(row[0]) in grouped:",
        "            if str(row[0]) in grouped or True:",
        id="k-other-tables-not-filtered",
    ),
    pytest.param(
        "    pins = KNOWLEDGE_SHAPE_SHA256.get(major, {}) if major is not None else {}",
        "    pins = KNOWLEDGE_SHAPE_SHA256.get(major, KNOWLEDGE_SHAPE_SHA256[16]) "
        "if major is not None else {}",
        id="k-unknown-major-uses-the-16-shape-pins",
    ),
    pytest.param(
        "        elif shape_pin is None:", "        elif False:", id="k-missing-shape-pin-quiet"
    ),
    # (f) a row that cannot be serialised
    pytest.param(
        "        if digest == SHAPE_UNREADABLE:",
        "        if False:",
        id="k-unreadable-shape-compared-as-a-digest",
    ),
    pytest.param(
        "                except (TypeError, ValueError):",
        "                except KeyError:",
        id="k-unserialisable-shape-row-crashes",
    ),
    pytest.param(
        '                    unreadable.add(f"{row[0]}.{aspect}")\n                    continue',
        "                    continue",
        id="k-unserialisable-shape-row-skipped",
    ),
    pytest.param(
        "        shape_pin = pins.get(key)",
        "        shape_pin = pins.get(key, digest)",
        id="k-missing-shape-pin-is-a-pass",
    ),
    pytest.param(
        "        elif digest != shape_pin:", "        elif False:", id="k-shape-compare-removed"
    ),
    pytest.param(
        "        elif digest != shape_pin:",
        "        elif digest == shape_pin:",
        id="k-shape-compare-inverted",
    ),
    # 11.3 sequence
    pytest.param(
        '    if row is None:\n        return ["the knowledge ordering sequence could not be read"]',
        "    if row is None:\n        return []",
        id="k-missing-sequence-is-a-pass",
    ),
    pytest.param(
        "    if tuple(row[:8]) != KNOWLEDGE_SEQUENCE_PARAMETERS:",
        "    if False:",
        id="k-sequence-parameters-not-compared",
    ),
    pytest.param(
        "    if tuple(row[:8]) != KNOWLEDGE_SEQUENCE_PARAMETERS:",
        "    if tuple(row[:7]) != KNOWLEDGE_SEQUENCE_PARAMETERS[:7]:",
        id="k-sequence-persistence-not-compared",
    ),
    pytest.param(
        "    if tuple(row[:8]) != KNOWLEDGE_SEQUENCE_PARAMETERS:",
        "    if row[2] < 0:",
        id="k-sequence-only-negative-increment-refused",
    ),
    pytest.param("    if row[8] is not True:", "    if False:", id="k-sequence-owner-not-checked"),
    pytest.param("    if row[9] is not True:", "    if False:", id="k-sequence-acl-not-checked"),
    pytest.param(SEQ_ACL_SQL, '        "WHERE true) "', id="k-sequence-owner-entry-counted"),
    pytest.param(SEQ_OWNER_SQL, '        "true, "', id="k-sequence-owner-sql-removed"),
    # 11.4 functions
    pytest.param("        if owned is not True:", "        if False:", id="k-function-owner-quiet"),
    pytest.param(
        "        if only_owner is not True:", "        if False:", id="k-function-acl-quiet"
    ),
    pytest.param(FN_OWNER_SQL, '        "true, "', id="k-function-owner-sql-removed"),
    pytest.param(
        FN_ACL_NULL,
        '        "NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(p.proacl) x "',
        id="k-null-proacl-accepted",
    ),
    pytest.param(FN_ACL_SQL, '        "WHERE true) "', id="k-function-owner-entry-counted"),
    # 11.7 SECURITY DEFINER wrappers
    pytest.param(DEFINER_CHECK, "    if False:", id="k-definer-check-removed"),
    pytest.param(
        DEFINER_CHECK,
        "    if wrappers is not None and int(wrappers[0]) != 0:",
        id="k-unreadable-definer-count-passes",
    ),
    pytest.param(
        DEFINER_MEMBER,
        '        f"AND pg_catalog.pg_has_role(p.proowner, {expected_owner}, %s) "',
        id="k-unknown-membership-not-counted",
    ),
    pytest.param(DEFINER_MEMBER, '        "AND %s IS NOT NULL "', id="k-every-definer-counted"),
    pytest.param(DEFINER_GUARDS, '        "AND %s IS NOT NULL",', id="k-guards-counted"),
    # 11.5 table and column ACLs
    pytest.param(
        '_KB_TABLE_GRANTS = ("SELECT", "INSERT")',
        '_KB_TABLE_GRANTS = ("SELECT", "INSERT", "UPDATE")',
        id="k-table-update-allowed",
    ),
    pytest.param(
        '_KB_COLUMN_GRANTS = ("INSERT",)',
        '_KB_COLUMN_GRANTS = ("INSERT", "UPDATE")',
        id="k-column-update-allowed",
    ),
    pytest.param(
        '_KB_COLUMN_GRANTS = ("INSERT",)',
        '_KB_COLUMN_GRANTS = ("INSERT", "SELECT")',
        id="k-column-select-allowed",
    ),
    pytest.param(
        ACL_PARAMS,
        "        (list(_KB_COLUMN_GRANTS), list(_KB_TABLE_GRANTS), tables),",
        id="k-acl-lists-swapped",
    ),
    pytest.param(
        TABLE_ACL_SQL,
        '        "WHERE x.grantee <> c.relowner AND (x.is_grantable "',
        id="k-table-public-allowed",
    ),
    pytest.param(
        TABLE_ACL_SQL,
        '        "WHERE x.grantee <> c.relowner AND (x.grantee = 0 "',
        id="k-table-grant-option-allowed",
    ),
    pytest.param(
        TABLE_ACL_SQL,
        '        "WHERE (x.grantee = 0 OR x.is_grantable "',
        id="k-table-owner-entry-counted",
    ),
    pytest.param(
        COLUMN_ACL_SQL,
        '        "WHERE a.attrelid = c.oid AND x.grantee <> c.relowner AND (false "',
        id="k-column-public-allowed",
    ),
    pytest.param(
        COLUMN_ACL_TAIL,
        '        "OR x.privilege_type <> ALL(%s::text[]))) "',
        id="k-column-grant-option-allowed",
    ),
    pytest.param(
        COLUMN_ACL_TAIL, '        "OR x.is_grantable)) "', id="k-column-privilege-not-compared"
    ),
    pytest.param("        if table_ok is not True:", "        if False:", id="k-table-acl-quiet"),
    pytest.param(
        "        if columns_ok is not True:", "        if False:", id="k-column-acl-quiet"
    ),
    # 11.5 INSERT grantees: kb_status_events column-level only, one inserter at most
    pytest.param(
        "    if len({r[1] for r in inserts}) > 1:",
        "    if False:",
        id="k-second-inserter-allowed",
    ),
    pytest.param(
        "    if len({r[1] for r in inserts}) > 1:",
        "    if len({r[1] for r in inserts}) > 2:",
        id="k-two-other-inserters-allowed",
    ),
    pytest.param(
        "    if len({r[1] for r in inserts}) > 1:",
        "    if len({(r[0], r[1]) for r in inserts}) > 3:",
        id="k-inserters-counted-per-table",
    ),
    pytest.param(
        "    if any(str(r[0]) == _KB_COLUMN_INSERT_ONLY and r[2] is not True for r in inserts):",
        "    if False:",
        id="k-events-table-insert-allowed",
    ),
    pytest.param(
        '_KB_COLUMN_INSERT_ONLY = "kb_status_events"',
        '_KB_COLUMN_INSERT_ONLY = "kb_chunks"',
        id="k-column-only-rule-on-the-wrong-table",
    ),
    pytest.param(
        '        "pg_catalog.aclexplode(a.attacl) x "\n        "WHERE s.nspname',
        '        "pg_catalog.aclexplode(NULL::aclitem[]) x "\n        "WHERE s.nspname',
        id="k-column-inserters-not-read",
    ),
    pytest.param(
        '        "SELECT c.relname, x.grantee, true FROM pg_catalog.pg_class c "',
        '        "SELECT c.relname, x.grantee, false FROM pg_catalog.pg_class c "',
        id="k-column-inserts-read-as-table-level",
    ),
    pytest.param(
        '_KB_INSERT = "INSERT"', '_KB_INSERT = "SELECT"', id="k-inserters-read-as-selecters"
    ),
    pytest.param(
        '"AND x.grantee <> c.relowner AND x.privilege_type = %s "\n',
        '"AND x.privilege_type = %s "\n',
        id="k-inserters-table-half-counts-the-owner",
    ),
    pytest.param(
        '"AND x.grantee <> c.relowner AND x.privilege_type = %s",\n',
        '"AND x.privilege_type = %s",\n',
        id="k-inserters-column-half-counts-the-owner",
    ),
    pytest.param(
        '"AND x.grantee <> c.relowner AND x.privilege_type = %s "\n',
        '"AND x.grantee <> c.relowner "\n',
        id="k-inserters-table-half-counts-every-privilege",
    ),
    pytest.param(
        "    inserts = [r for r in inserts if str(r[0]) in KNOWLEDGE_TABLES]\n",
        "    inserts = [r for r in inserts if str(r[0]) == 'kb_chunks']\n",
        id="k-inserters-only-on-one-table",
    ),
]

# The exact scenarios that catch each narrow mutation: a narrow mutation must be caught by the
# scenarios written for it, not only as a side effect (a broad one breaks the pristine scenarios).
CONDITION_11_CATCHERS: dict[str, list[str]] = {
    "k-inserters-column-half-counts-the-owner": [
        "the-owner-holds-a-column-insert-grant",
    ],
    "k-other-tables-not-filtered": [
        "row-of-another-table-is-ignored",
    ],
    "k-unknown-major-uses-the-16-shape-pins": [
        "no-pin-for-major-15",
        "no-pin-for-major-18",
        "no-shape-pins",
    ],
    "k-missing-shape-pin-quiet": [
        "no-pin-for-major-15",
        "no-pin-for-major-18",
        "no-shape-pins",
        "shape-pin-missing-for-one-key",
    ],
    "k-unreadable-shape-compared-as-a-digest": [
        "shape-row-not-serialisable",
    ],
    "k-unserialisable-shape-row-crashes": [
        "shape-row-not-serialisable",
    ],
    "k-unserialisable-shape-row-skipped": [
        "shape-row-not-serialisable",
    ],
    "k-missing-shape-pin-is-a-pass": [
        "no-pin-for-major-15",
        "no-pin-for-major-18",
        "no-shape-pins",
        "shape-pin-missing-for-one-key",
    ],
    "k-missing-sequence-is-a-pass": [
        "sequence-missing",
    ],
    "k-sequence-parameters-not-compared": [
        "sequence-type",
        "sequence-start",
        "sequence-increment-negative",
        "sequence-maximum",
        "sequence-minimum",
        "sequence-cache",
        "sequence-cycle",
        "sequence-unlogged",
    ],
    "k-sequence-persistence-not-compared": [
        "sequence-unlogged",
    ],
    "k-sequence-only-negative-increment-refused": [
        "sequence-type",
        "sequence-start",
        "sequence-maximum",
        "sequence-minimum",
        "sequence-cache",
        "sequence-cycle",
        "sequence-unlogged",
    ],
    "k-sequence-owner-not-checked": [
        "current-user-path-uses-current-user",
        "false-path-uses-the-assessments-owner",
        "false-path-without-assessments-owner",
        "sequence-wrong-owner",
    ],
    "k-sequence-acl-not-checked": [
        "sequence-granted-to-another-role",
        "sequence-granted-to-public",
    ],
    "k-sequence-owner-sql-removed": [
        "current-user-path-uses-current-user",
        "false-path-uses-the-assessments-owner",
        "false-path-without-assessments-owner",
        "sequence-wrong-owner",
    ],
    "k-function-owner-quiet": [
        "current-user-path-uses-current-user",
        "false-path-uses-the-assessments-owner",
        "false-path-without-assessments-owner",
        "function-wrong-owner",
    ],
    "k-function-acl-quiet": [
        "function-executable-by-another-role",
        "function-executable-by-public-null-acl",
    ],
    "k-function-owner-sql-removed": [
        "current-user-path-uses-current-user",
        "false-path-uses-the-assessments-owner",
        "false-path-without-assessments-owner",
        "function-wrong-owner",
    ],
    "k-null-proacl-accepted": [
        "function-executable-by-public-null-acl",
    ],
    "k-definer-check-removed": [
        "security-definer-wrapper",
        "security-definer-owner-membership-unknown",
        "security-definer-count-unreadable",
    ],
    "k-unreadable-definer-count-passes": [
        "security-definer-count-unreadable",
    ],
    "k-unknown-membership-not-counted": [
        "security-definer-owner-membership-unknown",
    ],
    "k-table-update-allowed": [
        "table-privilege-beyond-select-insert",
    ],
    "k-column-update-allowed": [
        "column-privilege-beyond-insert",
    ],
    "k-column-select-allowed": [
        "column-select-privilege",
    ],
    "k-table-public-allowed": [
        "table-privilege-to-public",
    ],
    "k-table-grant-option-allowed": [
        "table-privilege-with-grant-option",
    ],
    "k-column-public-allowed": [
        "column-privilege-to-public",
    ],
    "k-column-grant-option-allowed": [
        "column-privilege-with-grant-option",
    ],
    "k-column-privilege-not-compared": [
        "column-privilege-beyond-insert",
        "column-select-privilege",
    ],
    "k-table-acl-quiet": [
        "table-privilege-beyond-select-insert",
        "table-privilege-to-public",
        "table-privilege-with-grant-option",
    ],
    "k-column-acl-quiet": [
        "column-privilege-beyond-insert",
        "column-privilege-to-public",
        "column-privilege-with-grant-option",
        "column-select-privilege",
    ],
    "k-second-inserter-allowed": [
        "column-privilege-to-public",
        "status-events-table-level-insert-by-the-app",
        "second-table-inserter",
        "second-column-inserter",
        "second-inserter-on-a-column-of-another-table",
        "two-roles-split-the-insert-grants",
    ],
    "k-two-other-inserters-allowed": [
        "column-privilege-to-public",
        "status-events-table-level-insert-by-the-app",
        "second-table-inserter",
        "second-column-inserter",
        "second-inserter-on-a-column-of-another-table",
        "two-roles-split-the-insert-grants",
    ],
    "k-inserters-counted-per-table": [
        "two-roles-split-the-insert-grants",
    ],
    "k-events-table-insert-allowed": [
        "status-events-table-level-insert",
        "status-events-table-level-insert-by-the-app",
    ],
    "k-column-inserters-not-read": [
        "column-privilege-to-public",
        "second-column-inserter",
        "second-inserter-on-a-column-of-another-table",
    ],
    "k-inserters-only-on-one-table": [
        "status-events-table-level-insert",
        "status-events-table-level-insert-by-the-app",
        "second-column-inserter",
        "second-inserter-on-a-column-of-another-table",
        "two-roles-split-the-insert-grants",
    ],
}


def test_every_catcher_entry_names_a_real_mutation() -> None:
    """A renamed mutation id would otherwise silently drop its exact-catcher check."""
    assert set(CONDITION_11_CATCHERS) <= {p.id for p in CONDITION_11_MUTATIONS}
    assert set(KNOWLEDGE_CATCHERS) <= {p.id for p in KNOWLEDGE_MUTATIONS}
    assert all(CONDITION_11_CATCHERS.values()) and all(KNOWLEDGE_CATCHERS.values())


@pytest.mark.parametrize(("old", "new"), CONDITION_11_MUTATIONS)
def test_mutation_of_the_condition_11_checks_is_caught_by_a_scenario(
    request: pytest.FixtureRequest, old: str, new: str
) -> None:
    real = fake.failing_scenarios(migrations)
    mutant = mutated_module(migrations, (old, new))
    caught = fake.failing_scenarios(mutant)
    assert real == []
    assert caught != [], "no scenario noticed the mutation"
    mutation_id = request.node.callspec.id
    if mutation_id in CONDITION_11_CATCHERS:
        assert sorted(caught) == sorted(CONDITION_11_CATCHERS[mutation_id]), caught


@pytest.mark.parametrize(("old", "new"), KNOWLEDGE_MUTATIONS)
def test_mutation_of_knowledge_problems_is_caught_by_a_scenario(
    request: pytest.FixtureRequest, old: str, new: str
) -> None:
    real = fake.failing_scenarios(migrations)
    mutant = mutated_module(migrations, (old, new))
    caught = fake.failing_scenarios(mutant)
    assert real == []
    assert caught != [], "no scenario noticed the mutation"
    mutation_id = request.node.callspec.id
    if mutation_id in KNOWLEDGE_CATCHERS:
        assert sorted(caught) == sorted(KNOWLEDGE_CATCHERS[mutation_id]), caught


def test_mutation_the_wrong_table_check_is_noticed_only_by_its_own_scenario() -> None:
    mutant = mutated_module(migrations, (WRONG_TABLE, "        if False:"))
    caught = fake.failing_scenarios(mutant)
    assert caught == ["trigger-on-the-wrong-table"]


# --- migrate() and verify() -----------------------------------------------------------------------


def migrate_misses(module: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Scenarios in which `module.migrate` does not raise (or does not roll back) when it must, or
    raises when the catalogue is intact."""
    bad = []
    # the real pins, read once: `module` may be `migrations` itself, which the loop patches
    real_pins = migrations.KNOWLEDGE_FUNCTION_SHA256
    real_types = migrations.KNOWLEDGE_TRIGGER_TGTYPE
    real_shapes = migrations.KNOWLEDGE_SHAPE_SHA256
    for scenario in [s for s in SCENARIOS if s.current]:
        catalog = Catalog()
        scenario.mutate(catalog)
        conn = MigrateConnection(catalog)
        monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, _c=conn, **k: _c)
        monkeypatch.setattr(
            module,
            "KNOWLEDGE_FUNCTION_SHA256",
            scenario.pins if scenario.pins is not None else real_pins,
        )
        monkeypatch.setattr(
            module,
            "KNOWLEDGE_TRIGGER_TGTYPE",
            scenario.type_pins if scenario.type_pins is not None else real_types,
        )
        monkeypatch.setattr(
            module,
            "KNOWLEDGE_SHAPE_SHA256",
            scenario.shape_pins if scenario.shape_pins is not None else real_shapes,
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
