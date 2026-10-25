"""ADR 0022 condition 9 hardening: before any catalogue query, `knowledge_problems`,
`knowledge_function_hashes`, `knowledge_trigger_types`, `migrate()`'s post-check and `verify()` set
`search_path = pg_catalog, pg_temp` and `quote_all_identifiers = off` with SET LOCAL (so a hostile
search_path or setting cannot change what the queries or `pg_get_functiondef` return), and the
`verify()` path also sets `transaction_read_only = on`. The mutations remove each statement, drop
the call, or move it after the first query; each runs on a COPY (`mutated_module`).

Offline: the fakes record every statement, SET LOCAL included, in order. They prove the statements
are sent and when. Whether PostgreSQL honours them is proved only by a live run.

EQUIVALENT MUTANTS (deliberately not tested):
  - changing `SET LOCAL transaction_read_only = on` to `SET LOCAL default_transaction_read_only`
    is NOT equivalent (caught); but ordering `quote_all_identifiers` before `search_path` is
    equivalent (both are sent before the first query, and their relative order has no effect).
  - the docstring of `_harden`."""

from __future__ import annotations

import pytest
from tests.safety.kb_catalogue_fake import (
    Catalog,
    CatalogConnection,
    MigrateConnection,
    VerifyConnection,
)
from tests.safety.kb_fakes import mutated_module

from divesafe.services import migrations, postgres

pytestmark = pytest.mark.safety

SEARCH_PATH = "SET LOCAL search_path = pg_catalog, pg_temp"
QUOTING = "SET LOCAL quote_all_identifiers = off"
READ_ONLY = "SET LOCAL transaction_read_only = on"
DSN = "postgresql://never-used/x_test"


def first_read(statements: list[str]) -> int:
    return next(
        i for i, s in enumerate(statements) if s.startswith("SELECT") and "pg_catalog." in s
    )


def order_problems(statements: list[str], *, read_only: bool) -> list[str]:
    found = []
    reading = first_read(statements)
    for wanted in (SEARCH_PATH, QUOTING):
        at = [i for i, s in enumerate(statements) if s == wanted]
        if not at or at[0] > reading:
            found.append(f"{wanted!r} is not sent before the first catalogue query")
    ro = [i for i, s in enumerate(statements) if s == READ_ONLY]
    if read_only and (not ro or ro[0] > reading):
        found.append("transaction_read_only is not set before the first catalogue query")
    if not read_only and ro:
        found.append("transaction_read_only is set where it must not be")
    return found


def run_entries(
    mig: object, post: object, monkeypatch: pytest.MonkeyPatch
) -> dict[str, tuple[list[str], bool]]:
    """Entry point -> (statements in the order sent, whether read-only is expected)."""
    out: dict[str, tuple[list[str], bool]] = {}
    for label, read_only in (("knowledge_problems", False), ("knowledge_problems_ro", True)):
        cat = Catalog()
        mig.knowledge_problems(  # type: ignore[attr-defined]
            CatalogConnection(cat), owner_is_current_user=True, read_only=read_only
        )
        out[label] = (cat.sequence, read_only)
    cat = Catalog()
    mig.knowledge_function_hashes(CatalogConnection(cat))  # type: ignore[attr-defined]
    out["knowledge_function_hashes"] = (cat.sequence, False)
    cat = Catalog()
    mig.knowledge_trigger_types(CatalogConnection(cat))  # type: ignore[attr-defined]
    out["knowledge_trigger_types"] = (cat.sequence, False)

    conn = MigrateConnection(Catalog())
    monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, **k: conn)
    mig.migrate(DSN)  # type: ignore[attr-defined]
    out["migrate"] = (conn.queries, False)

    monkeypatch.setattr(post, "knowledge_problems", mig.knowledge_problems)  # type: ignore[attr-defined]
    vconn = VerifyConnection(Catalog())
    repo = post.PostgresAssessmentRepository(DSN)  # type: ignore[attr-defined]
    monkeypatch.setattr(repo, "_connect", lambda: vconn)
    repo.verify(least_privilege=False)
    out["verify"] = (vconn.calls, True)
    return out


def hardening_misses(mig: object, post: object, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    bad = []
    try:
        entries = run_entries(mig, post, monkeypatch)
    except Exception as exc:  # noqa: BLE001
        return [f"crash: {type(exc).__name__}"]  # a crash is not a pass
    for label, (statements, read_only) in entries.items():
        try:
            for problem in order_problems(statements, read_only=read_only):
                bad.append(f"{label}: {problem}")
        except StopIteration:
            bad.append(f"{label}: no catalogue query was sent")
    return bad


# --- the real code -------------------------------------------------------------------------------


def test_every_entry_point_sets_the_hardening_before_its_first_catalogue_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    misses = hardening_misses(migrations, postgres, monkeypatch)
    assert misses == []


def test_the_statements_are_exactly_these_fixed_texts_with_no_value_in_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = run_entries(migrations, postgres, monkeypatch)
    for label, (statements, _) in entries.items():
        sets = [s for s in statements if s.startswith("SET LOCAL")]
        assert set(sets) <= {SEARCH_PATH, QUOTING, READ_ONLY}, label
        assert "%s" not in "".join(sets), label


def test_verify_asks_for_a_read_only_transaction_and_the_others_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = run_entries(migrations, postgres, monkeypatch)
    with_read_only = sorted(k for k, (s, _) in entries.items() if READ_ONLY in s)
    assert with_read_only == ["knowledge_problems_ro", "verify"]


def test_read_only_adds_exactly_the_one_statement() -> None:
    plain, ro = Catalog(), Catalog()
    migrations.knowledge_problems(CatalogConnection(plain), owner_is_current_user=True)
    migrations.knowledge_problems(CatalogConnection(ro), owner_is_current_user=True, read_only=True)
    assert ro.sequence[:3] == [SEARCH_PATH, QUOTING, READ_ONLY]
    assert plain.sequence[:2] == [SEARCH_PATH, QUOTING]
    assert READ_ONLY not in plain.sequence
    assert ro.sequence[3:] == plain.sequence[2:]  # nothing else differs


def test_verify_calls_knowledge_problems_with_read_only_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def spy(conn: object, **kwargs: object) -> list[str]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(postgres, "knowledge_problems", spy)
    repo = postgres.PostgresAssessmentRepository(DSN)
    conn = VerifyConnection(Catalog())
    monkeypatch.setattr(repo, "_connect", lambda: conn)
    repo.verify(least_privilege=False)
    assert seen == [{"owner_is_current_user": False, "read_only": True}]


def test_migrate_calls_knowledge_problems_without_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def spy(conn: object, **kwargs: object) -> list[str]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(migrations, "knowledge_problems", spy)
    conn = MigrateConnection(Catalog())
    monkeypatch.setattr(migrations.psycopg, "connect", lambda *a, **k: conn)
    migrations.migrate(DSN)
    assert seen == [{"owner_is_current_user": True}]  # migrate writes: not read-only


def test_the_checker_itself_notices_a_statement_after_the_first_query() -> None:
    late = ["SELECT pg_catalog.current_setting('x')", SEARCH_PATH, QUOTING]
    assert order_problems(late, read_only=False) != []
    assert order_problems([SEARCH_PATH, QUOTING, "SELECT pg_catalog.x"], read_only=False) == []
    assert order_problems([SEARCH_PATH, QUOTING, "SELECT pg_catalog.x"], read_only=True) != []


# --- mutations of _harden and its callers (copies) ------------------------------------------------

SEARCH_LINE = '    conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")\n'
QUOTE_LINE = '    conn.execute("SET LOCAL quote_all_identifiers = off")\n'
RO_LINE = '        conn.execute("SET LOCAL transaction_read_only = on")\n'
HARDEN_HASHES = "    _harden(conn)\n    major_row"
HARDEN_TYPES = '    _harden(conn)\n    rows = conn.execute(\n        "SELECT t.tgname, t.tgtype'
HARDEN_PROBLEMS = "    _harden(conn, read_only=read_only)\n    problems: list[str] = []"

MIGRATIONS_MUTATIONS = [
    pytest.param(SEARCH_LINE, "", id="h-search-path-statement-removed"),
    pytest.param(QUOTE_LINE, "", id="h-quote-all-identifiers-statement-removed"),
    pytest.param(RO_LINE, "        pass\n", id="h-read-only-statement-removed"),
    pytest.param(
        SEARCH_LINE,
        '    conn.execute("SET search_path = pg_catalog, pg_temp")\n',
        id="h-search-path-not-local",
    ),
    pytest.param(
        SEARCH_LINE,
        '    conn.execute("SET LOCAL search_path = public")\n',
        id="h-search-path-includes-public",
    ),
    pytest.param(
        QUOTE_LINE,
        '    conn.execute("SET LOCAL quote_all_identifiers = on")\n',
        id="h-quote-all-identifiers-on",
    ),
    pytest.param(
        RO_LINE,
        '        conn.execute("SET LOCAL transaction_read_only = off")\n',
        id="h-read-only-off",
    ),
    pytest.param(
        "    if read_only:\n        conn.execute(",
        "    if not read_only:\n        conn.execute(",
        id="h-read-only-inverted",
    ),
    pytest.param(
        "    if read_only:\n        conn.execute(",
        "    if True:\n        conn.execute(",
        id="h-read-only-always",
    ),
    pytest.param(HARDEN_HASHES, "    major_row", id="h-hashes-not-hardened"),
    pytest.param(
        HARDEN_TYPES,
        '    rows = conn.execute(\n        "SELECT t.tgname, t.tgtype',
        id="h-trigger-types-not-hardened",
    ),
    pytest.param(HARDEN_PROBLEMS, "    problems: list[str] = []", id="h-problems-not-hardened"),
    pytest.param(
        HARDEN_PROBLEMS,
        "    _harden(conn)\n    problems: list[str] = []",
        id="h-problems-read-only-argument-dropped",
    ),
]

POSTGRES_MUTATIONS = [
    pytest.param(
        "return knowledge_problems(conn, owner_is_current_user=False, read_only=True)",
        "return knowledge_problems(conn, owner_is_current_user=False)",
        id="h-verify-does-not-ask-for-read-only",
    ),
    pytest.param(
        "return knowledge_problems(conn, owner_is_current_user=False, read_only=True)",
        "return knowledge_problems(conn, owner_is_current_user=False, read_only=False)",
        id="h-verify-read-only-false",
    ),
]


@pytest.mark.parametrize(("old", "new"), MIGRATIONS_MUTATIONS)
def test_mutation_of_the_migrations_hardening_is_caught(
    monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    assert hardening_misses(migrations, postgres, monkeypatch) == []
    mutant = mutated_module(migrations, (old, new))
    misses = hardening_misses(mutant, postgres, monkeypatch)
    assert misses != []
    assert not all(m.startswith("crash") for m in misses), "caught only by a crash"


@pytest.mark.parametrize(("old", "new"), POSTGRES_MUTATIONS)
def test_mutation_of_the_verify_hardening_is_caught(
    monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    mutant = mutated_module(postgres, (old, new))
    misses = hardening_misses(migrations, mutant, monkeypatch)
    assert misses != []
    assert not all(m.startswith("crash") for m in misses), "caught only by a crash"


def test_mutation_the_hardening_moved_after_the_first_query_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_harden` runs, but only after the first catalogue query (a count of statements passes)."""
    mutant = mutated_module(
        migrations,
        (
            HARDEN_HASHES,
            "    major_row",
        ),
        (
            "    assert major_row is not None\n    rows = conn.execute(\n"
            '        "SELECT p.proname, ',
            "    assert major_row is not None\n    _harden(conn)\n    rows = conn.execute(\n"
            '        "SELECT p.proname, ',
        ),
    )
    misses = hardening_misses(mutant, postgres, monkeypatch)
    assert any(m.startswith("knowledge_function_hashes") for m in misses)


def test_the_fake_really_records_a_set_local_that_the_fake_cannot_apply() -> None:
    cat = Catalog()
    CatalogConnection(cat).execute(SEARCH_PATH)
    assert cat.sequence == [SEARCH_PATH]
    assert cat.log == []  # not a SELECT: the SELECT log holds only the queries
