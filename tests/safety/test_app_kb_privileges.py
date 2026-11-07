"""ADR 0022 conditions 11.4 and 11.5, application side: `verify(least_privilege=True)` refuses an
application role that can do more than SELECT the knowledge tables (`postgres._privilege_problems`
with `_knowledge_privilege_checks` and, from PostgreSQL 17, the MAINTAIN check).

Offline: a fake connection records the one SELECT of flags and answers it. That proves the wiring,
the labels and the SQL TEXT (which objects and privileges each check names); whether the SQL is
right on a real server is proved by tests/integration/test_postgres_guard.py."""

from __future__ import annotations

import re
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import postgres
from divesafe.services.migrations import KNOWLEDGE_FUNCTIONS, KNOWLEDGE_SEQUENCE, KNOWLEDGE_TABLES

pytestmark = pytest.mark.safety

PREFIX = "the application role "
KNOWLEDGE_LABELS = [
    "owns a knowledge table",
    "holds a privilege other than SELECT on a knowledge table",
    "holds a column privilege on a knowledge table",
    "holds a privilege on the knowledge ordering sequence",
    "can EXECUTE a knowledge function",
    "owns a knowledge function",
]
MAINTAIN_LABEL = "holds MAINTAIN on a knowledge table"


class _Row:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row

    def fetchone(self) -> tuple[Any, ...]:
        return self.row


def _split(select_list: str) -> list[str]:
    """The SELECT list split on top-level commas (not inside parentheses or quotes)."""
    parts, current, depth, quoted = [], "", 0, False
    for char in select_list:
        if char == "'":
            quoted = not quoted
        elif not quoted and char in "()":
            depth += 1 if char == "(" else -1
        if char == "," and depth == 0 and not quoted:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return parts


class FlagConnection:
    """Answers the version query, then the flags query: True for each expression whose text is in
    `raised` (or for every expression with `raise_all`). Records the expressions."""

    def __init__(self, version: int, raised: set[str] | None = None, raise_all: bool = False):
        self.version = version
        self.raised = raised or set()
        self.raise_all = raise_all
        self.expressions: list[str] = []

    def execute(self, query: Any, params: Any = None) -> _Row:
        text = str(query)
        if "server_version_num" in text:
            return _Row((self.version,))
        assert text.startswith("SELECT ") and params is None
        self.expressions = _split(text.removeprefix("SELECT "))
        return _Row(tuple(self.raise_all or e in self.raised for e in self.expressions))


def checks(module: Any, version: int) -> list[tuple[str, str]]:
    """(sql, label) per selected expression: every flag raised, so every label is reported."""
    conn = FlagConnection(version, raise_all=True)
    labels = module.PostgresAssessmentRepository._privilege_problems(conn)
    assert len(labels) == len(conn.expressions)
    return [
        (sql, label.removeprefix(PREFIX))
        for sql, label in zip(conn.expressions, labels, strict=True)
    ]


def report(module: Any, version: int, raised_labels: set[str]) -> list[str]:
    raised = {sql for sql, label in checks(module, version) if label in raised_labels}
    conn = FlagConnection(version, raised)
    return list(module.PostgresAssessmentRepository._privilege_problems(conn))


def sql_of(module: Any, label: str, version: int = 170000) -> str:
    found = [sql for sql, name in checks(module, version) if name == label]
    assert len(found) == 1, (label, len(found))
    return found[0]


def privileges(sql: str, function: str) -> set[str]:
    match = re.search(rf"{function}\(.*'([A-Z][A-Z, ]*)'\)", sql)
    assert match is not None, sql
    return {p.strip() for p in match.group(1).split(",")}


def listed(sql: str, column: str) -> set[str]:
    match = re.search(rf"{re.escape(column)} IN \(([^)]*)\)", sql)
    assert match is not None, sql
    return {n.strip().strip("'") for n in match.group(1).split(",")}


# --- the checks (each takes the module, so the mutation tests can reuse them) ---------------------


def check_labels(module: Any) -> None:
    assert [label for _, label in module._knowledge_privilege_checks()] == KNOWLEDGE_LABELS
    for version in (160000, 169999, 170000, 180000):
        labels = [label for _, label in checks(module, version)]
        for label in KNOWLEDGE_LABELS:
            assert labels.count(label) == 1, (version, label)
        assert labels.count(MAINTAIN_LABEL) == (1 if version >= 170000 else 0), version


def check_each_reported_alone(module: Any) -> None:
    for version in (160000, 170000):
        assert report(module, version, set()) == []
        for label in KNOWLEDGE_LABELS + ([MAINTAIN_LABEL] if version >= 170000 else []):
            assert report(module, version, {label}) == [PREFIX + label], (version, label)


def check_table_sql(module: Any) -> None:
    for label in [*KNOWLEDGE_LABELS[:3], MAINTAIN_LABEL]:
        sql = sql_of(module, label)
        assert "n.nspname = 'public'" in sql, label
        assert listed(sql, "c.relname") == set(KNOWLEDGE_TABLES), label
    assert "pg_has_role(current_user, c.relowner, 'MEMBER')" in sql_of(module, KNOWLEDGE_LABELS[0])
    assert privileges(sql_of(module, KNOWLEDGE_LABELS[1]), "has_table_privilege") == {
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    }
    assert privileges(sql_of(module, KNOWLEDGE_LABELS[2]), "has_any_column_privilege") == {
        "INSERT",
        "UPDATE",
        "REFERENCES",
    }
    assert privileges(sql_of(module, MAINTAIN_LABEL), "has_table_privilege") == {"MAINTAIN"}


def check_sequence_sql(module: Any) -> None:
    sql = sql_of(module, KNOWLEDGE_LABELS[3])
    assert f"to_regclass('public.{KNOWLEDGE_SEQUENCE}')" in sql
    assert privileges(sql, "has_sequence_privilege") == {"USAGE", "SELECT", "UPDATE"}


def check_function_sql(module: Any) -> None:
    for label in KNOWLEDGE_LABELS[4:]:
        sql = sql_of(module, label)
        assert "n.nspname = 'public'" in sql, label
        assert listed(sql, "p.proname") == set(KNOWLEDGE_FUNCTIONS.values()), label
    assert "has_function_privilege(p.oid, 'EXECUTE')" in sql_of(module, KNOWLEDGE_LABELS[4])
    assert MEMBER_PATH not in sql_of(module, KNOWLEDGE_LABELS[5])  # ownership: pg_has_role itself
    assert "pg_has_role(current_user, p.proowner, 'MEMBER')" in sql_of(module, KNOWLEDGE_LABELS[5])


MEMBER_PATH = "pg_has_role(current_user, r.oid, 'MEMBER')"


def check_member_paths(module: Any) -> None:
    """Each privilege counts for the role itself AND for any role it is a member of (a NOINHERIT
    member does not inherit, but may SET ROLE), with the same objects and privileges both times."""
    sequence = f"to_regclass('public.{KNOWLEDGE_SEQUENCE}')"
    expected = [
        (KNOWLEDGE_LABELS[1], "has_table_privilege", f"c.oid, {TABLE_WRITES}"),
        (KNOWLEDGE_LABELS[2], "has_any_column_privilege", "c.oid, 'INSERT, UPDATE, REFERENCES'"),
        (KNOWLEDGE_LABELS[3], "has_sequence_privilege", f"{sequence}, 'USAGE, SELECT, UPDATE'"),
        (KNOWLEDGE_LABELS[4], "has_function_privilege", "p.oid, 'EXECUTE'"),
        (MAINTAIN_LABEL, "has_table_privilege", "c.oid, 'MAINTAIN'"),
    ]
    for label, function, args in expected:
        sql = sql_of(module, label)
        assert f"{function}({args})" in sql, label
        assert f"{function}(r.oid, {args})" in sql, label
        assert MEMBER_PATH in sql, label
        assert "FROM pg_roles r" in sql, label


APP_MEMBER_CHECKS = [
    ("can DELETE", "has_table_privilege", "to_regclass('public.assessments'), 'DELETE'"),
    ("can TRUNCATE", "has_table_privilege", "to_regclass('public.assessments'), 'TRUNCATE'"),
    ("can create triggers", "has_table_privilege", "to_regclass('public.assessments'), 'TRIGGER'"),
    (
        "can write the migration history",
        "has_table_privilege",
        "to_regclass('public.schema_migrations'), 'INSERT, UPDATE, DELETE, TRUNCATE'",
    ),
    (
        "can TRUNCATE or create triggers on the pending runs table",
        "has_table_privilege",
        "to_regclass('public.pending_runs'), 'TRUNCATE, TRIGGER'",
    ),
    (
        "can delete the failure counts",
        "has_table_privilege",
        "to_regclass('public.run_failure_counts'), 'DELETE, TRUNCATE'",
    ),
    ("can create objects in the public schema", "has_schema_privilege", "'public', 'CREATE'"),
    (
        "can create schemas in the database",
        "has_database_privilege",
        "current_database(), 'CREATE'",
    ),
    (
        "may set session_replication_role (replica mode skips ordinary triggers)",
        "has_parameter_privilege",
        "'session_replication_role', 'SET'",
    ),
]
SESSION_LABEL = "runs as a different role from its login role"


def check_app_member_paths(module: Any) -> None:
    """The audit-table, schema, database and parameter checks count SET ROLE paths too, and the
    session must run as its login role (a stored `role` setting would change whose grants these
    checks judge)."""
    for label, function, args in APP_MEMBER_CHECKS:
        sql = sql_of(module, label)
        assert f"{function}({args})" in sql, label
        assert f"{function}(r.oid, {args})" in sql, label
        assert MEMBER_PATH in sql, label
    assert sql_of(module, SESSION_LABEL) == "current_user <> session_user"
    assert report(module, 170000, {SESSION_LABEL}) == [PREFIX + SESSION_LABEL]


CHECKS = [
    check_labels,
    check_member_paths,
    check_app_member_paths,
    check_each_reported_alone,
    check_table_sql,
    check_sequence_sql,
    check_function_sql,
]


@pytest.mark.parametrize("check", CHECKS, ids=[c.__name__ for c in CHECKS])
def test_the_real_module(check: Any) -> None:
    check(postgres)


def test_the_maintain_gate_is_postgresql_17() -> None:
    assert postgres._MAINTAIN_PRIVILEGE_FROM == 170000


# --- mutations (copies of the module source) ------------------------------------------------------


def misses(module: Any) -> list[str]:
    bad = []
    for check in CHECKS:
        try:
            check(module)
        except AssertionError:
            bad.append(check.__name__)
    return bad


TABLE_LIST = '_KB_TABLE_LIST = ", ".join(f"\'{t}\'" for t in KNOWLEDGE_TABLES)'
FUNCTION_LIST = (
    '_KB_FUNCTION_LIST = ", ".join(f"\'{f}\'" for f in sorted(set(KNOWLEDGE_FUNCTIONS.values())))'
)
TABLE_WRITES = "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'"
TABLE_PRIVILEGES = f'_KB_TABLE_WRITES = "{TABLE_WRITES}"'

MUTATIONS = [
    pytest.param("            *_knowledge_privilege_checks(),\n", "", id="checks-not-spliced"),
    pytest.param(
        "        if version is not None and version[0] >= _MAINTAIN_PRIVILEGE_FROM:",
        "        if False:",
        id="maintain-never-checked",
    ),
    pytest.param(
        "_MAINTAIN_PRIVILEGE_FROM = 170000",
        "_MAINTAIN_PRIVILEGE_FROM = 180000",
        id="maintain-gate-too-late",
    ),
    pytest.param(
        "_MAINTAIN_PRIVILEGE_FROM = 170000",
        "_MAINTAIN_PRIVILEGE_FROM = 160000",
        id="maintain-asked-before-17",
    ),
    pytest.param('_MAINTAIN = "MAINTAIN"', '_MAINTAIN = "SELECT"', id="maintain-wrong-privilege"),
    pytest.param(
        TABLE_LIST, TABLE_LIST.replace("KNOWLEDGE_TABLES", "KNOWLEDGE_TABLES[:2]"), id="table-gone"
    ),
    pytest.param(
        FUNCTION_LIST,
        FUNCTION_LIST.replace("values())))", "values()))[1:])"),
        id="function-gone",
    ),
    pytest.param(
        TABLE_PRIVILEGES,
        TABLE_PRIVILEGES.replace("'INSERT, ", "'"),
        id="table-insert",
    ),
    pytest.param(
        TABLE_PRIVILEGES,
        TABLE_PRIVILEGES.replace(", TRIGGER'", "'"),
        id="table-trigger",
    ),
    pytest.param(
        "_KB_COLUMN_WRITES = \"'INSERT, UPDATE, REFERENCES'\"",
        "_KB_COLUMN_WRITES = \"'UPDATE, REFERENCES'\"",
        id="column-insert",
    ),
    pytest.param(
        "_KB_SEQUENCE_USE = \"'USAGE, SELECT, UPDATE'\"",
        "_KB_SEQUENCE_USE = \"'UPDATE'\"",
        id="seq-usage",
    ),
    pytest.param(
        '        f"AND {function}(r.oid, {args})))"',
        '        "AND false))"',
        id="member-path-dropped",
    ),
    pytest.param(
        '        f"({function}({args}) OR EXISTS (SELECT 1 FROM pg_roles r "',
        '        "(false OR EXISTS (SELECT 1 FROM pg_roles r "',
        id="own-privilege-dropped",
    ),
    pytest.param(
        "AND pg_has_role(current_user, r.oid, 'MEMBER') ",
        "",
        id="member-path-not-limited-to-memberships",
    ),
    pytest.param(
        '_self_or_member("has_table_privilege", f"c.oid, \'{_MAINTAIN}\'")',
        "f\"has_table_privilege(c.oid, '{_MAINTAIN}')\"",
        id="maintain-without-member-path",
    ),
    pytest.param(
        '_self_or_member("has_sequence_privilege", f"{sequence}, {_KB_SEQUENCE_USE}")',
        'f"has_sequence_privilege({sequence}, {_KB_SEQUENCE_USE})"',
        id="sequence-without-member-path",
    ),
    pytest.param(
        "pg_has_role(current_user, c.relowner, 'MEMBER')",
        "c.relowner = 0",
        id="table-ownership-not-through-membership",
    ),
    pytest.param(
        '            held = _self_or_member("has_table_privilege", '
        "f\"to_regclass('{table}'), '{privileges}'\")",
        "            held = f\"has_table_privilege(to_regclass('{table}'), '{privileges}')\"",
        id="app-table-checks-without-member-path",
    ),
    pytest.param(
        "_self_or_member(\"has_schema_privilege\", \"'public', 'CREATE'\")",
        "\"has_schema_privilege('public', 'CREATE')\"",
        id="schema-create-without-member-path",
    ),
    pytest.param(
        '_self_or_member("has_database_privilege", "current_database(), \'CREATE\'")',
        "\"has_database_privilege(current_database(), 'CREATE')\"",
        id="database-create-without-member-path",
    ),
    pytest.param(
        '_self_or_member("has_parameter_privilege", _REPLICATION_ROLE_SET)',
        'f"has_parameter_privilege({_REPLICATION_ROLE_SET})"',
        id="parameter-without-member-path",
    ),
    pytest.param(
        '            ("current_user <> session_user", ',
        '            ("false", ',
        id="session-role-never-checked",
    ),
    pytest.param(
        '_self_or_member("has_function_privilege", "p.oid, \'EXECUTE\'")',
        '"false"',
        id="execute-never-checked",
    ),
    pytest.param(
        "kb_function.format(\"pg_has_role(current_user, p.proowner, 'MEMBER')\")",
        'kb_function.format("false")',
        id="function-ownership-never-checked",
    ),
    pytest.param(
        "f\"WHERE n.nspname = 'public' AND c.relname IN ({_KB_TABLE_LIST}) AND {condition})\"",
        'f"WHERE c.relname IN ({_KB_TABLE_LIST}) AND {condition})"',
        id="table-schema-not-pinned",
    ),
]


def test_the_checker_passes_on_the_real_module() -> None:
    assert misses(postgres) == []


@pytest.mark.parametrize(("old", "new"), MUTATIONS)
def test_mutation_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(postgres, (old, new))
    assert misses(mutant) != [], "the mutation was not noticed"
