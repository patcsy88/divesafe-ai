"""A fake PostgreSQL catalogue for ADR 0022 condition 9 (`knowledge_problems`). Not a test file.

It answers the queries of `migrations.knowledge_problems` from a small model of the catalogue. It
READS the query text for a few fragments (the internal-trigger filter, the owner expression, the
inheritance sides, the `pg_database_owner` clause), so a mutation of those fragments changes its
answers. It does NOT parse SQL: only a live run proves the SQL itself. Every query is logged."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg.errors
from tests.safety.kb_catalogue_rows import SHAPE_ROWS

from divesafe.services.migrations import (
    KNOWLEDGE_FUNCTION_SHA256,
    KNOWLEDGE_FUNCTIONS,
    KNOWLEDGE_SEQUENCE_PARAMETERS,
    KNOWLEDGE_SHAPE_ASPECTS,
    KNOWLEDGE_SHAPE_SHA256,
    KNOWLEDGE_TABLES,
    KNOWLEDGE_TRIGGERS,
)

OWNER = "owner_test"
NIL = "0" * 64


class Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


def expected_tgtype(name: str) -> int:
    """pg_trigger.tgtype from the migration 4 CREATE TRIGGER text, written out here independently of
    the pin table in src: BEFORE ROW INSERT 7, BEFORE ROW UPDATE OR DELETE 27, BEFORE STATEMENT
    TRUNCATE 34."""
    if name.endswith("_insert_guard"):
        return 7
    if name.endswith("_append_only_guard"):
        return 27
    return 34


@dataclass
class Trig:
    rel: str
    name: str
    func: str
    schema: str = "public"
    enabled: str = "A"
    tgtype: int = -1  # -1: the expected type for the name
    qual: bool = False  # a WHEN clause
    attr: str = ""  # an UPDATE OF column list
    nargs: int = 0
    deferrable: bool = False
    constraint: int = 0
    oldtable: str | None = None
    newtable: str | None = None

    def __post_init__(self) -> None:
        if self.tgtype == -1:
            self.tgtype = expected_tgtype(self.name)


# The seven conditions of the "plain shape" expression in knowledge_problems. The fake reads the
# query text for each fragment and applies the check only if the fragment is there, so a mutation
# of one fragment changes its answer. It does not parse the SQL around them.
SHAPE_FRAGMENTS = (
    "t.tgqual IS NULL",
    "t.tgattr::pg_catalog.text = ''",
    "t.tgnargs = 0",
    "NOT t.tgdeferrable",
    "t.tgconstraint = 0",
    "t.tgoldtable IS NULL",
    "t.tgnewtable IS NULL",
)


SHAPE_FRAGMENTS_BY_ASPECT = {
    "columns": "JOIN pg_catalog.pg_attribute a ON",
    "constraints": "FROM pg_catalog.pg_constraint con",
    "indexes": "FROM pg_catalog.pg_index i",
}


APP = "app_test"
LOADER = "loader_test"
PUBLIC = "PUBLIC"
# the object's owner, whoever that is: ALTER ... OWNER TO rewrites the ACL to the new owner
SELF = "<owner>"
OWNER_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@dataclass(frozen=True)
class Grant:
    grantee: str
    privilege: str
    grantable: bool = False


def _loader(*privileges: str) -> list[Grant]:
    return [Grant(LOADER, p) for p in privileges]


def acl_ok(grants: list[Grant], text: str, allowed: list[str]) -> bool:
    """One NOT EXISTS subquery of `_acl_problems`: each condition applies only if its fragment is
    in that subquery's text; the allowed privileges come from the parameters."""
    for g in grants:
        if "x.grantee <> c.relowner" in text and g.grantee == SELF:
            continue
        if (
            (g.grantee == PUBLIC and "x.grantee = 0" in text)
            or (g.grantable and "x.is_grantable" in text)
            or ("x.privilege_type <> ALL(%s::text[])" in text and g.privilege not in allowed)
        ):
            return False
    return True


def insert_rows(
    table_acl: dict[str, list[Grant]],
    column_acl: dict[str, list[Grant]],
    text: str,
    params: Any,
) -> list[tuple[str, str, bool]]:
    """The INSERT-grantee query of `_acl_problems`: (table, grantee, column-level?) per entry.
    Each half of the UNION is read on its own: its owner filter and its privilege filter apply
    only if that half's text holds the fragment, so dropping a filter from one half is noticed."""
    table_text, _, column_text = text.partition("UNION ALL")
    # the privilege is a parameter: params is (tables, privilege, tables, privilege)
    halves = [(table_acl, False, table_text, params[1])]
    # the column half is there only if it reads the column ACLs, and marks its rows as columns
    # only if it says so
    if "aclexplode(a.attacl) x WHERE s.nspname" in column_text:
        halves.append((column_acl, "x.grantee, true" in column_text, column_text, params[-1]))
    rows = []
    for acl, is_column, half, privilege in halves:
        others = "x.grantee <> c.relowner" in half
        wanted = privilege if "x.privilege_type = %s" in half else None
        rows += [
            (table, g.grantee, is_column)
            for table, grants in acl.items()
            for g in grants
            if not (others and g.grantee == SELF) and (wanted is None or g.privilege == wanted)
        ]
    return rows


def plain_shape(t: Trig, text: str) -> bool:
    checks = (
        not t.qual,
        t.attr == "",
        t.nargs == 0,
        not t.deferrable,
        t.constraint == 0,
        t.oldtable is None,
        t.newtable is None,
    )
    return all(ok for fragment, ok in zip(SHAPE_FRAGMENTS, checks, strict=True) if fragment in text)


@dataclass
class Table:
    owner: str = OWNER
    rls: bool = False
    forced: bool = False
    persistence: str = "p"
    hidden: bool = False


def table_of(trigger: str) -> str:
    return next(t for t in KNOWLEDGE_TABLES if trigger.startswith(f"{t}_"))


@dataclass
class Catalog:
    major: int = 16
    current_user: str = OWNER
    assessments_owner: str | None = OWNER
    schema_owner: str = OWNER
    db_owner: str = OWNER
    rules: int = 0
    policies: int = 0
    inherit_parent: int = 0
    inherit_child: int = 0
    counts_unreadable: bool = False
    deny: bool = False
    log: list[tuple[str, Any]] = field(default_factory=list)  # the SELECTs (text, params)
    sequence: list[str] = field(default_factory=list)  # every statement, SET LOCAL included
    triggers: list[Trig] = field(default_factory=list)
    internal: list[Trig] = field(default_factory=list)
    digests: dict[str, str] = field(default_factory=dict)
    tables: dict[str, Table] = field(default_factory=dict)
    # condition 11: the recorded real rows (kb_catalogue_rows), the sequence and the functions
    shape: dict[str, list[tuple[Any, ...]]] = field(default_factory=dict)
    sequence_row: tuple[Any, ...] | None = KNOWLEDGE_SEQUENCE_PARAMETERS
    sequence_owner: str = OWNER
    sequence_acl: list[str] = field(default_factory=lambda: [SELF])  # grantees
    function_owners: dict[str, str] = field(default_factory=dict)
    function_acl: dict[str, list[str] | None] = field(default_factory=dict)  # None: NULL proacl
    # SECURITY DEFINER functions outside the guards: is the owner a member of the expected owner
    # (True, False or unknown None)? None for the whole list: the count cannot be read.
    definers: list[bool | None] | None = field(default_factory=lambda: [False])
    table_acl: dict[str, list[Grant]] = field(default_factory=dict)
    column_acl: dict[str, list[Grant]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.shape = {aspect: list(rows) for aspect, rows in SHAPE_ROWS.items()}
        functions = sorted(set(KNOWLEDGE_FUNCTIONS.values()))
        self.function_owners = dict.fromkeys(functions, OWNER)
        self.function_acl = {f: [SELF] for f in functions}
        # as install_schema grants them: the owner holds everything, the app SELECT, the loader
        # SELECT and INSERT (kb_status_events: SELECT plus column INSERT)
        owner = [Grant(SELF, p) for p in OWNER_PRIVILEGES]
        self.table_acl = {
            "kb_documents": [*owner, Grant(APP, "SELECT"), *_loader("SELECT", "INSERT")],
            "kb_chunks": [*owner, Grant(APP, "SELECT"), *_loader("SELECT", "INSERT")],
            "kb_status_events": [*owner, Grant(APP, "SELECT"), *_loader("SELECT")],
        }
        self.column_acl = {t: [] for t in KNOWLEDGE_TABLES}
        self.column_acl["kb_status_events"] = [Grant(LOADER, "INSERT")]
        self.triggers = [Trig(table_of(n), n, KNOWLEDGE_FUNCTIONS[n]) for n in KNOWLEDGE_TRIGGERS]
        # the foreign keys of kb_chunks / kb_status_events: internal triggers, filtered by the query
        self.internal = [
            Trig(
                "kb_chunks", "RI_ConstraintTrigger_c_1", "RI_FKey_check_ins", "pg_catalog", "O", 29
            ),
            Trig("kb_documents", "RI_ConstraintTrigger_a_2", "RI_FKey_del", "pg_catalog", "O", 9),
        ]
        self.digests = dict(KNOWLEDGE_FUNCTION_SHA256[16])
        self.tables = {t: Table() for t in KNOWLEDGE_TABLES}

    def _execute_ok(self, name: str, text: str) -> bool:
        acl = self.function_acl[name]
        if acl is None:  # NULL proacl: PUBLIC may EXECUTE; aclexplode(NULL) has no rows
            return "p.proacl IS NOT NULL" not in text
        others = "x.grantee <> p.proowner" in text
        return all(others and g == SELF for g in acl)

    def _definer_count(self, text: str) -> int:
        assert self.definers is not None
        guards = 0 if "AND p.oid NOT IN" in text else len(set(KNOWLEDGE_FUNCTIONS.values()))
        if "pg_catalog.pg_has_role(p.proowner" not in text:
            return guards + len(self.definers)
        unknown_counts = "IS NOT FALSE" in text
        return guards + sum(1 for m in self.definers if m is True or (m is None and unknown_counts))

    def expected_owner(self, text: str) -> str | None:
        return self.current_user if "rolname = current_user" in text else self.assessments_owner

    def answer(self, text: str, params: Any = None) -> Rows | None:
        self.sequence.append(text)
        if text.startswith("SET "):
            return Rows([])  # no model of settings: only the order and the text are recorded
        self.log.append((text, params))
        if self.deny:
            raise psycopg.errors.InsufficientPrivilege("denied")
        if "server_version_num" in text:
            return Rows([(self.major * 10000,)])
        if "p.proacl" in text:
            expected = self.expected_owner(text)
            return Rows(
                [
                    (
                        name,
                        "p.proowner IS NOT DISTINCT FROM" not in text
                        or (expected is not None and owner == expected),
                        self._execute_ok(name, text),
                    )
                    for name, owner in self.function_owners.items()
                    if name in {t.func for t in self.triggers}
                ]
            )
        if "p.prosecdef" in text:
            return Rows([] if self.definers is None else [(self._definer_count(text),)])
        if "FROM pg_catalog.pg_proc p JOIN" in text:
            return Rows(sorted(self.digests.items()))
        if "x.grantee, false" in text:  # before the shape queries: it joins pg_attribute too
            return Rows(list(insert_rows(self.table_acl, self.column_acl, text, params)))
        for aspect, fragment in SHAPE_FRAGMENTS_BY_ASPECT.items():
            if fragment in text:
                return Rows(list(self.shape[aspect]))
        if "pg_catalog.pg_sequence" in text:
            if self.sequence_row is None:
                return Rows([])
            expected = self.expected_owner(text)
            owner_ok = expected is not None and self.sequence_owner == expected
            if "c.relowner IS NOT DISTINCT FROM" not in text:
                owner_ok = True
            others = "x.grantee <> c.relowner" in text
            only_owner = all(others and g == SELF for g in self.sequence_acl)
            return Rows([(*self.sequence_row, owner_ok, only_owner)])
        if "aclexplode(c.relacl)" in text:
            parts = text.split("NOT EXISTS")
            assert len(parts) == 3, "the ACL query has one table and one column subquery"
            return Rows(
                [
                    (
                        n,
                        acl_ok(self.table_acl[n], parts[1], params[0]),
                        acl_ok(self.column_acl[n], parts[2], params[1]),
                    )
                    for n, t in self.tables.items()
                    if not t.hidden
                ]
            )
        if "SELECT t.tgname, t.tgtype" in text:  # knowledge_trigger_types
            rows = list(self.triggers)
            if "NOT t.tgisinternal" not in text:
                rows += self.internal
            return Rows([(t.name, t.tgtype) for t in rows])
        if "pg_catalog.pg_trigger" in text:
            rows = list(self.triggers)
            if "NOT t.tgisinternal" not in text:
                rows += self.internal
            return Rows(
                [
                    (
                        t.rel,
                        t.name,
                        t.func,
                        t.schema,
                        t.enabled,
                        self.digests.get(t.func, "f" * 64),
                        t.tgtype,
                        plain_shape(t, text),
                    )
                    for t in rows
                ]
            )
        if "pg_rewrite" in text:
            if self.counts_unreadable:
                return Rows([])
            parent = self.inherit_parent if "inhparent IN" in text else 0
            child = self.inherit_child if "inhrelid IN" in text else 0
            return Rows([(self.rules, self.policies, parent + child)])
        if "c.relrowsecurity" in text:
            expected = self.expected_owner(text)
            return Rows(
                [
                    (
                        n,
                        t.rls,
                        t.forced,
                        t.persistence,
                        expected is not None and t.owner == expected,
                    )
                    for n, t in self.tables.items()
                    if not t.hidden
                ]
            )
        if "nspowner" in text:
            expected = self.expected_owner(text)
            via_database_owner = (
                self.schema_owner == "pg_database_owner"
                and "rolname = 'pg_database_owner'" in text
                and ("current_database()) = " not in text or self.db_owner == expected)
            )
            ok = expected is not None and (self.schema_owner == expected or via_database_owner)
            return Rows([(ok,)])
        return None


class CatalogConnection:
    """Answers only the condition 9 queries; anything else is a test error."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.events: list[str] = []

    def execute(self, query: Any, params: Any = None) -> Rows:
        answer = self.catalog.answer(str(query), params)
        if answer is None:
            raise AssertionError("an unexpected query reached the catalogue fake")
        return answer


# --- the fixed problem texts (migrations.knowledge_problems) -------------------------------------

NINE = "the knowledge tables do not carry exactly the nine guard triggers"
RULE = "a rewrite rule exists on a knowledge table"
POLICY = "a row-level security policy exists on a knowledge table"
INHERIT = "a knowledge table takes part in table inheritance"
UNREAD = "a knowledge table could not be read"
PUBLIC_OWNER = "the public schema is not owned by the expected owner role"


def wrong_table(name: str) -> str:
    return f"knowledge trigger '{name}' is on the wrong table"


def not_call(name: str) -> str:
    return f"knowledge trigger '{name}' does not call {KNOWLEDGE_FUNCTIONS[name]} in public"


def not_always(name: str) -> str:
    return f"knowledge trigger '{name}' is not ENABLE ALWAYS"


def not_type(name: str) -> str:
    return f"knowledge trigger '{name}' does not have its expected trigger type"


def not_plain(name: str) -> str:
    return f"knowledge trigger '{name}' does not have the expected plain shape"


def no_type_pin(name: str) -> str:
    return f"no pinned trigger type for knowledge trigger '{name}'"


def no_pin(function: str) -> str:
    return f"no pinned definition hash for {function} on this PostgreSQL version"


def mismatch(function: str) -> str:
    return f"the definition of {function} does not match its pinned hash"


def rls_on(table: str) -> str:
    return f"row-level security is on for {table}"


def not_permanent(table: str) -> str:
    return f"{table} is not a permanent table"


def wrong_owner(table: str) -> str:
    return f"{table} is not owned by the expected owner role"


# condition 11
SEQUENCE_UNREAD = "the knowledge ordering sequence could not be read"
SEQUENCE_PARAMETERS = "the knowledge ordering sequence does not have its expected parameters"
SEQUENCE_OWNER = "the knowledge ordering sequence is not owned by the expected owner role"
SEQUENCE_ACL = "a role other than the owner holds a privilege on the ordering sequence"
DEFINER = "a SECURITY DEFINER function other than the guards runs as the owner role"


def no_shape_pin(table: str, aspect: str) -> str:
    return f"no pinned definition of the {aspect} of {table} on this PostgreSQL version"


def shape_mismatch(table: str, aspect: str) -> str:
    return f"the {aspect} of {table} do not match their pinned definition"


def function_owner(function: str) -> str:
    return f"{function} is not owned by the expected owner role"


def function_acl(function: str) -> str:
    return f"a role other than the owner may EXECUTE {function}"


def table_acl(table: str) -> str:
    return f"a role other than the owner holds more than SELECT or INSERT on {table}"


def column_acl(table: str) -> str:
    return f"a column privilege other than INSERT is granted on {table}"


EVENTS_TABLE_INSERT = "a role other than the owner holds table-level INSERT on kb_status_events"
MANY_INSERTERS = "more than one role other than the owner may INSERT into the knowledge tables"


def shape_unreadable(table: str, aspect: str) -> str:
    return f"the {aspect} of {table} could not be read"


# --- one table of scenarios, used by the tests AND by every mutation -----------------------------


@dataclass(frozen=True)
class Scenario:
    id: str
    mutate: Callable[[Catalog], None]
    expected: list[str]
    current: bool = True
    pins: Mapping[int, Mapping[str, str]] | None = None
    type_pins: Mapping[str, int] | None = None
    shape_pins: Mapping[int, Mapping[str, str]] | None = None


def _trig(cat: Catalog, name: str) -> Trig:
    return next(t for t in cat.triggers if t.name == name)


def _all_owner() -> list[str]:
    functions = sorted(set(KNOWLEDGE_FUNCTIONS.values()))
    return (
        [wrong_owner(t) for t in KNOWLEDGE_TABLES]
        + [PUBLIC_OWNER, SEQUENCE_OWNER]
        + [function_owner(f) for f in functions]
    )


def own_everything(cat: Catalog, role: str) -> None:
    for table in cat.tables.values():
        table.owner = role
    cat.schema_owner = role
    cat.db_owner = role
    cat.sequence_owner = role
    cat.function_owners = dict.fromkeys(cat.function_owners, role)


def _no_shape_pins() -> list[str]:
    return [no_shape_pin(t, a) for a in KNOWLEDGE_SHAPE_ASPECTS for t in KNOWLEDGE_TABLES]


def _major(cat: Catalog, major: int) -> None:
    cat.major = major


def _set(cat: Catalog, **fields: Any) -> None:
    for key, value in fields.items():
        setattr(cat, key, value)


def _hide(cat: Catalog) -> None:
    cat.tables["kb_chunks"].hidden = True


def _swap_always(cat: Catalog, state: str) -> None:
    _trig(cat, "kb_chunks_append_only_guard").enabled = state


def _pins_without(function: str) -> Mapping[int, Mapping[str, str]]:
    return {16: {k: v for k, v in KNOWLEDGE_FUNCTION_SHA256[16].items() if k != function}}


def _types_without(name: str) -> Mapping[str, int]:
    return {n: expected_tgtype(n) for n in KNOWLEDGE_TRIGGERS if n != name}


def _fault(name: str, **fields: Any) -> Callable[[Catalog], None]:
    def mutate(cat: Catalog) -> None:
        for key, value in fields.items():
            setattr(_trig(cat, name), key, value)

    return mutate


def _edit_row(aspect: str, index: int, column: int, value: Any) -> Callable[[Catalog], None]:
    def mutate(cat: Catalog) -> None:
        row = list(cat.shape[aspect][index])
        row[column] = value
        cat.shape[aspect][index] = tuple(row)

    return mutate


def _row_index(aspect: str, table: str, needle: str) -> int:
    return next(
        i for i, r in enumerate(SHAPE_ROWS[aspect]) if r[0] == table and needle in str(r[-1])
    )


def _column_index(table: str, column: str) -> int:
    return next(i for i, r in enumerate(SHAPE_ROWS["columns"]) if r[0] == table and r[2] == column)


def _drop_row(aspect: str, index: int) -> Callable[[Catalog], None]:
    return lambda cat: cat.shape[aspect].pop(index)


def _add_row(aspect: str, row: tuple[Any, ...]) -> Callable[[Catalog], None]:
    return lambda cat: cat.shape[aspect].append(row)


def _reverse_rows(cat: Catalog) -> None:
    for rows in cat.shape.values():
        rows.reverse()


def _sequence(position: int, value: Any) -> Callable[[Catalog], None]:
    def mutate(cat: Catalog) -> None:
        assert cat.sequence_row is not None
        row = list(cat.sequence_row)
        row[position] = value
        cat.sequence_row = tuple(row)

    return mutate


def _replace_loader(cat: Catalog) -> None:
    for acl in (*cat.table_acl.values(), *cat.column_acl.values()):
        acl[:] = [
            Grant("other_loader_test", g.privilege) if g.grantee == LOADER else g for g in acl
        ]


def _split_inserts(cat: Catalog) -> None:
    cat.table_acl["kb_chunks"] = [
        g for g in cat.table_acl["kb_chunks"] if g != Grant(LOADER, "INSERT")
    ]
    cat.table_acl["kb_chunks"].append(Grant(APP, "INSERT"))


def _condition_11_scenarios() -> list[Scenario]:
    display = _row_index("constraints", "kb_documents", "CHECK (display_permitted)")
    unique = _row_index("constraints", "kb_status_events", "UNIQUE (event_order)")
    status_fk = _row_index("constraints", "kb_status_events", "FOREIGN KEY")
    title = _column_index("kb_documents", "title")
    section = _column_index("kb_chunks", "section")
    order = _column_index("kb_status_events", "event_order")
    events_index = _row_index("indexes", "kb_status_events", "event_order")
    sequence = [
        ("sequence-type", 0, "integer"),
        ("sequence-start", 1, 100),
        ("sequence-increment-negative", 2, -1),
        ("sequence-maximum", 3, 10),
        ("sequence-minimum", 4, -100),
        ("sequence-cache", 5, 20),
        ("sequence-cycle", 6, True),
        ("sequence-unlogged", 7, "u"),
    ]
    out = [
        Scenario("shape-rows-in-another-order", _reverse_rows, []),
        Scenario(
            "constraint-dropped",
            _drop_row("constraints", display),
            [shape_mismatch("kb_documents", "constraints")],
        ),
        Scenario(
            "constraint-not-valid",
            _edit_row("constraints", display, 2, False),
            [shape_mismatch("kb_documents", "constraints")],
        ),
        Scenario(
            "constraint-deferrable",
            _edit_row("constraints", unique, 3, True),
            [shape_mismatch("kb_status_events", "constraints")],
        ),
        Scenario(
            "constraint-check-changed",
            _edit_row("constraints", display, 7, "CHECK (true)"),
            [shape_mismatch("kb_documents", "constraints")],
        ),
        Scenario(
            "foreign-key-dropped",
            _drop_row("constraints", status_fk),
            [shape_mismatch("kb_status_events", "constraints")],
        ),
        Scenario(
            "constraint-added",
            _add_row(
                "constraints", ("kb_chunks", "c", True, False, False, False, True, "CHECK (true)")
            ),
            [shape_mismatch("kb_chunks", "constraints")],
        ),
        Scenario(
            "column-not-null-dropped",
            _edit_row("columns", title, 4, False),
            [shape_mismatch("kb_documents", "columns")],
        ),
        Scenario(
            "column-type-changed",
            _edit_row("columns", section, 3, "character varying(300)"),
            [shape_mismatch("kb_chunks", "columns")],
        ),
        Scenario(
            "column-default-added",
            _edit_row("columns", title, 8, "'x'::text"),
            [shape_mismatch("kb_documents", "columns")],
        ),
        Scenario(
            "column-generated",
            _edit_row("columns", title, 5, "s"),
            [shape_mismatch("kb_documents", "columns")],
        ),
        Scenario(
            "column-identity",
            _edit_row("columns", order, 6, "a"),
            [shape_mismatch("kb_status_events", "columns")],
        ),
        Scenario(
            "column-collation",
            _edit_row("columns", title, 7, "C"),
            [shape_mismatch("kb_documents", "columns")],
        ),
        Scenario(
            "column-added",
            _add_row("columns", ("kb_chunks", 7, "extra", "text", False, "", "", "default", None)),
            [shape_mismatch("kb_chunks", "columns")],
        ),
        Scenario(
            "column-renamed",
            _edit_row("columns", title, 2, "heading"),
            [shape_mismatch("kb_documents", "columns")],
        ),
        Scenario(
            "index-invalid",
            _edit_row("indexes", events_index, 1, False),
            [shape_mismatch("kb_status_events", "indexes")],
        ),
        Scenario(
            "index-added",
            _add_row(
                "indexes",
                (
                    "kb_chunks",
                    True,
                    True,
                    True,
                    True,
                    "CREATE INDEX extra_idx ON public.kb_chunks USING btree (section)",
                ),
            ),
            [shape_mismatch("kb_chunks", "indexes")],
        ),
        Scenario(
            "row-of-another-table-is-ignored",
            _add_row("columns", ("assessments", 1, "id", "text", True, "", "", "default", None)),
            [],
        ),
        Scenario("no-shape-pins", lambda c: None, _no_shape_pins(), shape_pins={}),
        Scenario(
            "shape-pin-missing-for-one-key",
            lambda c: None,
            [no_shape_pin("kb_chunks", "indexes")],
            shape_pins={
                16: {
                    k: v for k, v in KNOWLEDGE_SHAPE_SHA256[16].items() if k != "kb_chunks.indexes"
                }
            },
        ),
        Scenario(
            "sequence-missing",
            lambda c: _set(c, sequence_row=None),
            [SEQUENCE_UNREAD],
        ),
        Scenario(
            "sequence-wrong-owner",
            lambda c: _set(c, sequence_owner="leaky_owner_test"),
            [SEQUENCE_OWNER],
        ),
        Scenario(
            "sequence-granted-to-another-role",
            lambda c: c.sequence_acl.append(APP),
            [SEQUENCE_ACL],
        ),
        Scenario(
            "sequence-granted-to-public",
            lambda c: c.sequence_acl.append(PUBLIC),
            [SEQUENCE_ACL],
        ),
        Scenario(
            "function-wrong-owner",
            lambda c: c.function_owners.update(kb_no_truncate="leaky_owner_test"),
            [function_owner("kb_no_truncate")],
        ),
        Scenario(
            "function-executable-by-another-role",
            lambda c: c.function_acl["kb_chunks_insert"].append(APP),  # type: ignore[union-attr]
            [function_acl("kb_chunks_insert")],
        ),
        Scenario(
            "function-executable-by-public-null-acl",
            lambda c: c.function_acl.update(kb_no_update_delete=None),
            [function_acl("kb_no_update_delete")],
        ),
        Scenario(
            "security-definer-wrapper",
            lambda c: c.definers.append(True),  # type: ignore[union-attr]
            [DEFINER],
        ),
        Scenario(
            "security-definer-owner-membership-unknown",
            lambda c: c.definers.append(None),  # type: ignore[union-attr]
            [DEFINER],
        ),
        Scenario(
            "security-definer-count-unreadable",
            lambda c: _set(c, definers=None),
            [DEFINER],
        ),
        Scenario(
            "table-privilege-beyond-select-insert",
            lambda c: c.table_acl["kb_chunks"].append(Grant(APP, "UPDATE")),
            [table_acl("kb_chunks")],
        ),
        Scenario(
            "table-privilege-to-public",
            lambda c: c.table_acl["kb_documents"].append(Grant(PUBLIC, "SELECT")),
            [table_acl("kb_documents")],
        ),
        Scenario(
            "table-privilege-with-grant-option",
            lambda c: c.table_acl["kb_status_events"].append(Grant(LOADER, "SELECT", True)),
            [table_acl("kb_status_events")],
        ),
        Scenario(
            "column-privilege-beyond-insert",
            lambda c: c.column_acl["kb_documents"].append(Grant(APP, "UPDATE")),
            [column_acl("kb_documents")],
        ),
        Scenario(
            "column-privilege-to-public",
            lambda c: c.column_acl["kb_chunks"].append(Grant(PUBLIC, "INSERT")),
            [column_acl("kb_chunks"), MANY_INSERTERS],
        ),
        Scenario(
            "column-privilege-with-grant-option",
            lambda c: c.column_acl["kb_status_events"].append(Grant(LOADER, "INSERT", True)),
            [column_acl("kb_status_events")],
        ),
        Scenario(
            "status-events-table-level-insert",
            lambda c: c.table_acl["kb_status_events"].append(Grant(LOADER, "INSERT")),
            [EVENTS_TABLE_INSERT],
        ),
        Scenario(
            "status-events-table-level-insert-by-the-app",
            lambda c: c.table_acl["kb_status_events"].append(Grant(APP, "INSERT")),
            [EVENTS_TABLE_INSERT, MANY_INSERTERS],
        ),
        Scenario(
            "second-table-inserter",
            lambda c: c.table_acl["kb_chunks"].append(Grant(APP, "INSERT")),
            [MANY_INSERTERS],
        ),
        Scenario(
            "second-column-inserter",
            lambda c: c.column_acl["kb_status_events"].append(Grant(APP, "INSERT")),
            [MANY_INSERTERS],
        ),
        Scenario(
            "second-inserter-on-a-column-of-another-table",
            lambda c: c.column_acl["kb_documents"].append(Grant(APP, "INSERT")),
            [MANY_INSERTERS],
        ),
        Scenario(
            "two-roles-split-the-insert-grants",  # three (table, role) pairs, two roles
            _split_inserts,
            [MANY_INSERTERS],
        ),
        Scenario(
            "a-second-role-with-select-only-is-not-an-inserter",
            lambda c: c.table_acl["kb_chunks"].append(Grant("reader_test", "SELECT")),
            [],
        ),
        Scenario(
            "the-loader-replaced-by-one-other-role",
            _replace_loader,
            [],
        ),
        Scenario(
            "the-owner-holds-a-column-insert-grant",  # the owner is never an inserter to count
            lambda c: c.column_acl["kb_documents"].append(Grant(SELF, "INSERT")),
            [],
        ),
        Scenario(
            "shape-row-not-serialisable",
            _edit_row("constraints", display, 7, object()),
            [shape_unreadable("kb_documents", "constraints")],
        ),
        Scenario(
            "column-select-privilege",
            lambda c: c.column_acl["kb_chunks"].append(Grant(APP, "SELECT")),
            [column_acl("kb_chunks")],
        ),
    ]
    out += [
        Scenario(name, _sequence(position, value), [SEQUENCE_PARAMETERS])
        for name, position, value in sequence
    ]
    return out


def build_scenarios() -> list[Scenario]:
    triggers = list(KNOWLEDGE_TRIGGERS)
    out = [
        Scenario("pristine-16", lambda c: None, []),
        Scenario(
            "pristine-17",
            lambda c: _set(c, major=17, digests=dict(KNOWLEDGE_FUNCTION_SHA256[17])),
            [],
        ),
        Scenario("pristine-false-path", lambda c: _set(c, current_user="app_test"), [], False),
        Scenario(
            "current-user-path-uses-current-user",
            lambda c: _set(c, current_user="app_test"),
            _all_owner(),
        ),
        Scenario(
            "false-path-uses-the-assessments-owner",
            lambda c: _set(c, assessments_owner="other_test"),
            _all_owner(),
            False,
        ),
        Scenario(
            "false-path-without-assessments-owner",
            lambda c: _set(c, assessments_owner=None),
            _all_owner(),
            False,
        ),
        Scenario(
            "wrong-table-owner",
            lambda c: setattr(c.tables["kb_chunks"], "owner", "leaky_owner_test"),
            [wrong_owner("kb_chunks")],
        ),
        Scenario(
            "wrong-public-owner",
            lambda c: _set(c, schema_owner="leaky_owner_test"),
            [PUBLIC_OWNER],
        ),
        Scenario(
            "public-owned-by-the-database-owner",
            lambda c: _set(c, schema_owner="pg_database_owner"),
            [],
        ),
        Scenario(
            "public-owned-by-the-database-owner-false-path",
            lambda c: _set(c, schema_owner="pg_database_owner", current_user="app_test"),
            [],
            False,
        ),
        Scenario(
            "public-owned-by-a-database-owner-who-is-not-expected",
            lambda c: _set(c, schema_owner="pg_database_owner", db_owner="leaky_owner_test"),
            [PUBLIC_OWNER],
        ),
        Scenario(
            "trigger-missing",
            lambda c: c.triggers.remove(_trig(c, triggers[0])),
            [NINE],
        ),
        Scenario(
            "last-trigger-missing",
            lambda c: c.triggers.remove(_trig(c, triggers[-1])),
            [NINE],
        ),
        Scenario(
            "trigger-extra",
            lambda c: c.triggers.append(
                Trig("kb_chunks", "leaky_trigger_test", "kb_chunks_insert")
            ),
            [NINE],
        ),
        Scenario(
            "trigger-duplicate",
            lambda c: c.triggers.append(Trig("kb_chunks", triggers[1], "kb_chunks_insert")),
            [NINE],
        ),
        Scenario(
            "trigger-on-the-wrong-table",
            lambda c: setattr(_trig(c, "kb_documents_insert_guard"), "rel", "kb_chunks"),
            [wrong_table("kb_documents_insert_guard")],
        ),
        Scenario(
            "trigger-calls-the-wrong-function",
            lambda c: setattr(_trig(c, "kb_chunks_insert_guard"), "func", "kb_no_truncate"),
            [not_call("kb_chunks_insert_guard"), mismatch("kb_chunks_insert")],
        ),
        Scenario(
            "trigger-function-in-the-wrong-schema",
            lambda c: setattr(_trig(c, "kb_chunks_insert_guard"), "schema", "leaky_schema"),
            [not_call("kb_chunks_insert_guard")],
        ),
        Scenario(
            "trigger-sha256-mismatch",
            lambda c: c.digests.update(kb_chunks_insert="0" * 64),
            [mismatch("kb_chunks_insert")],
        ),
        Scenario(
            "shared-function-sha256-mismatch",
            lambda c: c.digests.update(kb_no_truncate="0" * 64),
            [mismatch("kb_no_truncate")] * 3,
        ),
        Scenario(
            "no-pin-for-major-15",
            lambda c: _major(c, 15),
            [no_pin(KNOWLEDGE_FUNCTIONS[n]) for n in triggers] + _no_shape_pins(),
        ),
        Scenario(
            "no-pin-for-major-18",
            lambda c: _major(c, 18),
            [no_pin(KNOWLEDGE_FUNCTIONS[n]) for n in triggers] + _no_shape_pins(),
        ),
        Scenario(
            "empty-pin-table",
            lambda c: None,
            [no_pin(KNOWLEDGE_FUNCTIONS[n]) for n in triggers],
            pins={},
        ),
        Scenario(
            "function-missing-from-the-pin-table",
            lambda c: None,
            [no_pin("kb_no_truncate")] * 3,
            pins=_pins_without("kb_no_truncate"),
        ),
        Scenario("rewrite-rule", lambda c: _set(c, rules=1), [RULE]),
        Scenario("policy-row", lambda c: _set(c, policies=1), [POLICY]),
        Scenario("inheritance-parent", lambda c: _set(c, inherit_parent=1), [INHERIT]),
        Scenario("inheritance-child", lambda c: _set(c, inherit_child=1), [INHERIT]),
        Scenario(
            "counts-unreadable-fails-closed",
            lambda c: _set(c, counts_unreadable=True),
            [RULE, POLICY, INHERIT],
        ),
        Scenario(
            "row-security-enabled",
            lambda c: setattr(c.tables["kb_documents"], "rls", True),
            [rls_on("kb_documents")],
        ),
        Scenario(
            "row-security-forced",
            lambda c: setattr(c.tables["kb_status_events"], "forced", True),
            [rls_on("kb_status_events")],
        ),
        Scenario(
            "row-security-forced-and-enabled",
            lambda c: (
                setattr(c.tables["kb_chunks"], "forced", True),
                setattr(c.tables["kb_chunks"], "rls", True),
            ),
            [rls_on("kb_chunks")],
        ),
        Scenario(
            "unlogged-table",
            lambda c: setattr(c.tables["kb_chunks"], "persistence", "u"),
            [not_permanent("kb_chunks")],
        ),
        Scenario(
            "temporary-table",
            lambda c: setattr(c.tables["kb_documents"], "persistence", "t"),
            [not_permanent("kb_documents")],
        ),
        Scenario("table-unreadable", _hide, [UNREAD]),
        # --- trigger type (tgtype) and plain shape -------------------------------------------
        Scenario(
            "tgtype-insert-guard-fires-on-update",
            _fault("kb_chunks_insert_guard", tgtype=19),  # BEFORE UPDATE FOR EACH ROW
            [not_type("kb_chunks_insert_guard")],
        ),
        Scenario(
            "tgtype-append-only-guard-fires-on-delete-only",
            _fault("kb_chunks_append_only_guard", tgtype=11),  # BEFORE DELETE FOR EACH ROW
            [not_type("kb_chunks_append_only_guard")],
        ),
        Scenario(
            "tgtype-append-only-guard-fires-on-update-only",
            _fault("kb_documents_append_only_guard", tgtype=19),
            [not_type("kb_documents_append_only_guard")],
        ),
        Scenario(
            "tgtype-insert-guard-is-after",
            _fault("kb_documents_insert_guard", tgtype=5),  # AFTER INSERT FOR EACH ROW
            [not_type("kb_documents_insert_guard")],
        ),
        Scenario(
            "tgtype-truncate-guard-is-per-row",
            _fault("kb_status_events_no_truncate_guard", tgtype=35),
            [not_type("kb_status_events_no_truncate_guard")],
        ),
        Scenario(
            "tgtype-truncate-bit-missing-only",  # BEFORE STATEMENT with no event: 34 - 32
            _fault("kb_documents_no_truncate_guard", tgtype=2),
            [not_type("kb_documents_no_truncate_guard")],
        ),
        Scenario(
            "tgtype-instead-bit-set-only",  # 34 + 64
            _fault("kb_chunks_no_truncate_guard", tgtype=98),
            [not_type("kb_chunks_no_truncate_guard")],
        ),
        Scenario(
            "tgtype-delete-bit-missing-only",  # BEFORE UPDATE FOR EACH ROW is 19; 27 - 8
            _fault("kb_status_events_append_only_guard", tgtype=19),
            [not_type("kb_status_events_append_only_guard")],
        ),
        Scenario(
            "tgtype-update-bit-missing-only",  # 27 - 16
            _fault("kb_documents_append_only_guard", tgtype=11),
            [not_type("kb_documents_append_only_guard")],
        ),
        Scenario(
            "tgtype-row-bit-missing-only",  # a statement-level insert guard: 7 - 1
            _fault("kb_status_events_insert_guard", tgtype=6),
            [not_type("kb_status_events_insert_guard")],
        ),
        Scenario(
            "tgtype-before-bit-missing-only",  # AFTER ROW INSERT: 7 - 2
            _fault("kb_chunks_insert_guard", tgtype=5),
            [not_type("kb_chunks_insert_guard")],
        ),
        Scenario(
            "tgtype-extra-event-bit-only",  # insert guard that also fires on delete: 7 + 8
            _fault("kb_documents_insert_guard", tgtype=15),
            [not_type("kb_documents_insert_guard")],
        ),
        Scenario(
            "when-clause-on-an-insert-guard",
            _fault("kb_documents_insert_guard", qual=True),
            [not_plain("kb_documents_insert_guard")],
        ),
        Scenario(
            "when-clause-on-a-truncate-guard",
            _fault("kb_chunks_no_truncate_guard", qual=True),
            [not_plain("kb_chunks_no_truncate_guard")],
        ),
        Scenario(
            "update-of-column-list",  # BEFORE UPDATE OF title OR DELETE keeps tgtype 27
            _fault("kb_chunks_append_only_guard", attr="2"),
            [not_plain("kb_chunks_append_only_guard")],
        ),
        Scenario(
            "trigger-arguments",
            _fault("kb_status_events_insert_guard", nargs=1),
            [not_plain("kb_status_events_insert_guard")],
        ),
        Scenario(
            "deferrable-trigger",
            _fault("kb_documents_no_truncate_guard", deferrable=True),
            [not_plain("kb_documents_no_truncate_guard")],
        ),
        Scenario(
            "constraint-trigger",
            _fault("kb_chunks_insert_guard", constraint=16385),
            [not_plain("kb_chunks_insert_guard")],
        ),
        Scenario(
            "transition-table-old",
            _fault("kb_documents_append_only_guard", oldtable="old_rows"),
            [not_plain("kb_documents_append_only_guard")],
        ),
        Scenario(
            "transition-table-new",
            _fault("kb_status_events_append_only_guard", newtable="new_rows"),
            [not_plain("kb_status_events_append_only_guard")],
        ),
        Scenario(
            "several-shape-faults-give-one-problem",
            _fault("kb_chunks_insert_guard", qual=True, nargs=2, deferrable=True),
            [not_plain("kb_chunks_insert_guard")],
        ),
        Scenario(
            "wrong-type-and-wrong-shape-give-both-problems",
            _fault("kb_chunks_insert_guard", tgtype=19, qual=True),
            [not_type("kb_chunks_insert_guard"), not_plain("kb_chunks_insert_guard")],
        ),
        Scenario(
            "extra-trigger-shape-is-not-judged",
            lambda c: c.triggers.append(
                Trig("kb_chunks", "leaky_trigger_test", "kb_chunks_insert", tgtype=99, qual=True)
            ),
            [NINE],
        ),
        Scenario(
            "no-trigger-type-pins",
            lambda c: None,
            [no_type_pin(n) for n in triggers],
            type_pins={},
        ),
        Scenario(
            "trigger-type-pin-missing-for-one-trigger",
            lambda c: None,
            [no_type_pin("kb_chunks_insert_guard")],
            type_pins=_types_without("kb_chunks_insert_guard"),
        ),
        Scenario(
            "missing-type-pin-fails-closed-even-for-a-wrong-type",
            _fault("kb_chunks_insert_guard", tgtype=19),
            [no_type_pin("kb_chunks_insert_guard")],
            type_pins=_types_without("kb_chunks_insert_guard"),
        ),
        Scenario(
            "wrong-trigger-type-pin",
            lambda c: None,
            [not_type("kb_chunks_insert_guard")],
            type_pins={**_types_without(""), "kb_chunks_insert_guard": 19},
        ),
    ]
    out += _condition_11_scenarios()
    for state in ("O", "D", "R"):
        out.append(
            Scenario(
                f"trigger-enabled-state-{state}",
                lambda c, st=state: _swap_always(c, st),
                [not_always("kb_chunks_append_only_guard")],
            )
        )
    return out


SCENARIOS = build_scenarios()


def run_scenario(module: Any, scenario: Scenario) -> list[str]:
    catalog = Catalog()
    scenario.mutate(catalog)
    original = module.KNOWLEDGE_FUNCTION_SHA256
    original_types = module.KNOWLEDGE_TRIGGER_TGTYPE
    original_shapes = module.KNOWLEDGE_SHAPE_SHA256
    if scenario.shape_pins is not None:
        module.KNOWLEDGE_SHAPE_SHA256 = scenario.shape_pins
    if scenario.pins is not None:
        module.KNOWLEDGE_FUNCTION_SHA256 = scenario.pins
    if scenario.type_pins is not None:
        module.KNOWLEDGE_TRIGGER_TGTYPE = scenario.type_pins
    try:
        found: list[str] = module.knowledge_problems(
            CatalogConnection(catalog), owner_is_current_user=scenario.current
        )
    finally:
        module.KNOWLEDGE_FUNCTION_SHA256 = original
        module.KNOWLEDGE_TRIGGER_TGTYPE = original_types
        module.KNOWLEDGE_SHAPE_SHA256 = original_shapes
    return found


def failing_scenarios(module: Any) -> list[str]:
    """Ids of the scenarios `module` gets wrong. A crash counts as wrong (it is not a refusal)."""
    failed = []
    for scenario in SCENARIOS:
        try:
            found = run_scenario(module, scenario)
        except Exception:  # noqa: BLE001
            failed.append(scenario.id)
            continue
        if sorted(found) != sorted(scenario.expected):
            failed.append(scenario.id)
    return failed


# --- fakes for migrate() and verify() around the catalogue ----------------------------------------


class MigrateConnection:
    """The database holds every migration already. Records how the `with` block was left."""

    def __init__(self, catalog: Catalog) -> None:
        from divesafe.services.migrations import GUARD_TRIGGERS

        self.catalog = catalog
        self.audit = list(GUARD_TRIGGERS)
        self.exited = False
        self.exit_type: type[BaseException] | None = None
        self.queries: list[str] = []

    def __enter__(self) -> MigrateConnection:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *rest: object) -> None:
        self.exited = True
        self.exit_type = exc_type  # psycopg rolls the transaction back when this is not None

    def execute(self, query: Any, params: Any = None) -> Rows:
        from divesafe.services.migrations import MIGRATIONS

        text = str(query)
        self.queries.append(text)
        if text.startswith("SET ") and not text.startswith("SET search_path"):
            return self.catalog.answer(text, params) or Rows([])
        if text == "SELECT current_user":
            return Rows([(self.catalog.current_user,)])
        if "FROM public.schema_migrations" in text:
            return Rows([(m.version, m.name, m.checksum) for m in MIGRATIONS])
        if "FROM pg_trigger" in text:
            names = list(KNOWLEDGE_TRIGGERS) if "kb_documents" in text else self.audit
            return Rows([(n,) for n in names])
        if "pg_catalog." in text:
            answer = self.catalog.answer(text, params)
            if answer is not None:
                return answer
        return Rows([])


class Transaction:
    def __init__(self) -> None:
        self.entered = 0
        self.left_with: list[type[BaseException] | None] = []

    def __enter__(self) -> Transaction:
        self.entered += 1
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *rest: object) -> None:
        self.left_with.append(exc_type)


class VerifyConnection:
    """Answers what `PostgresAssessmentRepository.verify(least_privilege=False)` asks."""

    def __init__(self, catalog: Catalog, missing: tuple[str, ...] = ()) -> None:
        self.catalog = catalog
        self.missing = missing
        self.server_encoding = "UTF8"
        self.calls: list[str] = []
        self.transaction_log = Transaction()

    def __enter__(self) -> VerifyConnection:
        return self

    def __exit__(self, *rest: object) -> None:
        return None

    def transaction(self) -> Transaction:
        return self.transaction_log

    def execute(self, query: Any, params: Any = None) -> Rows:
        from divesafe.services.migrations import GUARD_FUNCTIONS, MIGRATIONS

        text = str(query)
        self.calls.append(text)
        if text.startswith("SET search_path"):
            return Rows([])
        if "current_setting('server_encoding')" in text:
            return Rows([(self.server_encoding,)])
        if text.startswith("SET ") and not text.startswith("SET search_path"):
            return self.catalog.answer(text, params) or Rows([])
        if text.startswith("SELECT to_regclass(%s)"):
            table = str(params[0]).removeprefix("public.")
            return Rows([(None,)] if table in self.missing else [(table,)])
        if text.startswith("SELECT to_regclass("):
            return Rows([("x",)])
        if "FROM public.schema_migrations" in text:
            return Rows([(m.version, m.name, m.checksum) for m in MIGRATIONS])
        if "FROM pg_trigger t" in text:
            return Rows([(t, f) for t, f in GUARD_FUNCTIONS.items()])
        if "session_replication_role" in text:
            return Rows([("origin",)])
        if "pg_catalog." in text:
            answer = self.catalog.answer(text, params)
            if answer is not None:
                return answer
        return Rows([])
