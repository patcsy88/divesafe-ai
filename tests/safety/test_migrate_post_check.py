"""`migrate()` checks after applying that the audit and knowledge triggers exist and are enabled
('A', always) and raises `MigrationError` if not. Database-free: a fake connection answers the
reads, so a trigger set missing one name, or not enabled, is simulated. The fake also answers the
ADR 0022 condition 9 catalogue queries from an intact catalogue, so what varies here is only the
trigger names and enablement; the catalogue checks themselves (owners, rules, row security,
inheritance, persistence, function and trigger-type pins, trigger shape) are tested in
test_kb_catalogue.py. Grant drift and added columns are not covered by either."""

from __future__ import annotations

import inspect
import textwrap
from typing import Any

import pytest
from tests.safety.kb_catalogue_fake import Catalog

from divesafe.services import migrations as migrations_module
from divesafe.services.migrations import (
    GUARD_TRIGGERS,
    KNOWLEDGE_TRIGGERS,
    MIGRATIONS,
    MigrationError,
    migrate,
)

pytestmark = pytest.mark.safety

KB_MESSAGE = "the knowledge guard is not intact after migrating"
AUDIT_MESSAGE = "the audit guard is not intact after migrating"


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeConnection:
    """The database already holds every migration; `triggers` is what pg_trigger would contain
    as (table, name, tgenabled). The query text decides whether the 'A' filter is applied."""

    def __init__(self, triggers: list[tuple[str, str, str]]) -> None:
        self.triggers = triggers

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> _Rows:
        text = str(query)
        if text == "SELECT current_user":
            return _Rows([("divesafe_owner",)])
        if "FROM public.schema_migrations" in text:
            return _Rows([(m.version, m.name, m.checksum) for m in MIGRATIONS])
        if "FROM pg_trigger" in text:
            kb = "kb_documents" in text
            only_enabled = "tgenabled = 'A'" in text
            rows = [
                (name,)
                for table, name, enabled in self.triggers
                if (table == "kb") == kb and (enabled == "A" or not only_enabled)
            ]
            return _Rows(rows)
        if "pg_catalog." in text:
            # ADR 0022 condition 9 queries: an intact catalogue (the trigger names are what this
            # file varies; the catalogue checks themselves are in test_kb_catalogue.py)
            answer = Catalog(current_user="divesafe_owner", assessments_owner="divesafe_owner")
            for table in answer.tables.values():
                table.owner = "divesafe_owner"
            answer.schema_owner = answer.db_owner = "divesafe_owner"
            found = answer.answer(text, params)
            if found is not None:
                return found  # type: ignore[return-value]
        return _Rows([])


def _triggers(drop: str | None = None, disabled: str | None = None) -> list[tuple[str, str, str]]:
    rows = [("audit", n, "A") for n in GUARD_TRIGGERS] + [
        ("kb", n, "A") for n in KNOWLEDGE_TRIGGERS
    ]
    rows = [r for r in rows if r[1] != drop]
    return [(t, n, "O" if n == disabled else e) for t, n, e in rows]


def _install(monkeypatch: pytest.MonkeyPatch, triggers: list[tuple[str, str, str]]) -> None:
    monkeypatch.setattr(
        migrations_module.psycopg, "connect", lambda *a, **k: _FakeConnection(triggers)
    )


def test_a_complete_enabled_trigger_set_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _triggers())
    assert migrate("postgresql://never-used/x_test") == []


@pytest.mark.parametrize("missing", KNOWLEDGE_TRIGGERS)
def test_a_knowledge_trigger_set_missing_one_name_is_refused(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _install(monkeypatch, _triggers(drop=missing))
    with pytest.raises(MigrationError, match=KB_MESSAGE):
        migrate("postgresql://never-used/x_test")


@pytest.mark.parametrize("name", KNOWLEDGE_TRIGGERS)
def test_a_knowledge_trigger_that_is_not_enabled_always_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _install(monkeypatch, _triggers(disabled=name))
    with pytest.raises(MigrationError, match=KB_MESSAGE):
        migrate("postgresql://never-used/x_test")


@pytest.mark.parametrize("name", GUARD_TRIGGERS)
def test_an_audit_trigger_missing_or_disabled_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    for triggers in (_triggers(drop=name), _triggers(disabled=name)):
        _install(monkeypatch, triggers)
        with pytest.raises(MigrationError, match=AUDIT_MESSAGE):
            migrate("postgresql://never-used/x_test")


def test_the_refusal_does_not_echo_the_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _triggers(drop=KNOWLEDGE_TRIGGERS[0]))
    with pytest.raises(MigrationError) as caught:
        migrate("postgresql://u:never-echoed@host/x_test")
    assert "never-echoed" not in str(caught.value)


# --- mutations: a COPY of migrate() with one edit; src is not touched ---------------------------


def _mutated_migrate(old: str, new: str) -> Any:
    source = textwrap.dedent(inspect.getsource(migrations_module.migrate))
    assert source.count(old) == 1, old
    namespace: dict[str, Any] = {}
    exec(source.replace(old, new), vars(migrations_module), namespace)  # noqa: S102
    return namespace["migrate"]


KB_BAD = {
    "one trigger missing": _triggers(drop=KNOWLEDGE_TRIGGERS[0]),
    "one trigger not enabled": _triggers(disabled=KNOWLEDGE_TRIGGERS[-1]),
}

MUTATIONS = [
    (
        "post-check deleted",
        "if not set(KNOWLEDGE_TRIGGERS) <= kb_enabled:",
        "if False:",
    ),
    (
        "subset test turned into any-overlap",
        "if not set(KNOWLEDGE_TRIGGERS) <= kb_enabled:",
        "if not (set(KNOWLEDGE_TRIGGERS) & kb_enabled):",
    ),
    (
        "enabled filter dropped from the query",
        "WHERE tgenabled = 'A' AND tgrelid IN ",
        "WHERE tgrelid IN ",
    ),
    (
        "check skipped for the knowledge migration",
        "if len(known) >= KNOWLEDGE_MIGRATION:",
        "if False:",
    ),
]


@pytest.mark.parametrize(("label", "old", "new"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_mutation_a_weakened_post_check_lets_a_broken_guard_through(
    monkeypatch: pytest.MonkeyPatch, label: str, old: str, new: str
) -> None:
    mutated = _mutated_migrate(old, new)
    escaped = []
    for case, triggers in KB_BAD.items():
        _install(monkeypatch, triggers)
        try:
            mutated("postgresql://never-used/x_test")
        except MigrationError:
            continue
        escaped.append(case)
    assert escaped, f"{label}: the mutation was not noticed"
    # the real function refuses every one of those cases (the tests above are what notice)
    for triggers in KB_BAD.values():
        _install(monkeypatch, triggers)
        with pytest.raises(MigrationError, match=KB_MESSAGE):
            migrate("postgresql://never-used/x_test")
