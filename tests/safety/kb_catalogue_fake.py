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

from divesafe.services.migrations import (
    KNOWLEDGE_FUNCTION_SHA256,
    KNOWLEDGE_FUNCTIONS,
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

    def __post_init__(self) -> None:
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
        if "FROM pg_catalog.pg_proc p JOIN" in text:
            return Rows(sorted(self.digests.items()))
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


# --- one table of scenarios, used by the tests AND by every mutation -----------------------------


@dataclass(frozen=True)
class Scenario:
    id: str
    mutate: Callable[[Catalog], None]
    expected: list[str]
    current: bool = True
    pins: Mapping[int, Mapping[str, str]] | None = None
    type_pins: Mapping[str, int] | None = None


def _trig(cat: Catalog, name: str) -> Trig:
    return next(t for t in cat.triggers if t.name == name)


def _all_owner() -> list[str]:
    return [wrong_owner(t) for t in KNOWLEDGE_TABLES] + [PUBLIC_OWNER]


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
            [no_pin(KNOWLEDGE_FUNCTIONS[n]) for n in triggers],
        ),
        Scenario(
            "no-pin-for-major-18",
            lambda c: _major(c, 18),
            [no_pin(KNOWLEDGE_FUNCTIONS[n]) for n in triggers],
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
