"""Fixes from the reviews of c24e680 (ADR 0022): the session's search_path is pinned (security N1),
a naive clock is a fixed connector error (N4), and the load-time character screen runs again at
read time, dropping the chunk's whole family and never repairing it (N3). Each check has a
mutation check on a copy of the module."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
from tests.agent.test_knowledge_connector import (
    END,
    NOW,
    SITE,
    START,
    FakeStore,
    _Conn,
    chunk,
    ver,
)
from tests.safety.kb_fakes import mutated_module

from divesafe.data import ConnectorError
from divesafe.rag import connector as connector_module
from divesafe.rag import items as items_module
from divesafe.rag import store as store_module
from divesafe.rag.connector import SCREEN_ISSUE, KnowledgeConnector, check
from divesafe.rag.store import (
    STORE_NAIVE_CLOCK,
    ChunkRow,
    KnowledgeStoreError,
    PostgresKnowledgeStore,
    Selection,
)
from divesafe.services import kb_screen

pytestmark = pytest.mark.safety

DSN = "postgresql://u@db.invalid/x_test"
SEARCH_PATH = ("SET search_path = pg_catalog, pg_temp", None)


# --- N1: search_path pinned ------------------------------------------------------------------


def test_the_session_pins_search_path_before_any_query(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn()
    monkeypatch.setattr(store_module.psycopg, "connect", lambda *a, **k: conn)
    PostgresKnowledgeStore(DSN).select(SITE.id, NOW, END)
    assert conn.log.index(SEARCH_PATH) < conn.log.index("COMMIT")
    first_query = next(i for i, e in enumerate(conn.log) if isinstance(e, tuple) and e[1])
    assert conn.log.index(SEARCH_PATH) < first_query


def test_mutation_search_path_dropped_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    mutant = mutated_module(
        store_module,
        (
            "_SESSION_SETUP = (_DATE_STYLE, _CLIENT_ENCODING, _SEARCH_PATH)",
            "_SESSION_SETUP = (_DATE_STYLE, _CLIENT_ENCODING)",
        ),
    )
    conn = _Conn()
    monkeypatch.setattr(mutant.psycopg, "connect", lambda *a, **k: conn)
    mutant.PostgresKnowledgeStore(DSN).select(SITE.id, NOW, END)
    assert SEARCH_PATH not in conn.log


# --- N4: a naive clock is a fixed connector error --------------------------------------------

NAIVE = datetime(2026, 6, 15, 10, 0)


@pytest.mark.parametrize("which", ["now", "window_end"])
def test_a_naive_clock_is_a_fixed_connector_error(which: str) -> None:
    now, end = (NAIVE, END) if which == "now" else (NOW, NAIVE)
    with pytest.raises(KnowledgeStoreError) as raised:
        store_module.parameters(SITE.id, now, end)
    assert str(raised.value) == STORE_NAIVE_CLOCK
    store = FakeStore(Selection((), (), 0))
    with pytest.raises(ConnectorError) as caught:
        asyncio.run(KnowledgeConnector(store).retrieve(SITE, START, end, now))
    assert str(caught.value) == STORE_NAIVE_CLOCK
    assert not isinstance(caught.value, ValueError)
    assert store.calls == []


def test_mutation_naive_check_in_parameters_is_caught() -> None:
    mutant = mutated_module(
        store_module,
        (
            "    if any(i.tzinfo is None or i.utcoffset() is None for i in instants):\n",
            "    if False:\n",
        ),
    )
    assert mutant.parameters(SITE.id, NAIVE, END)["now"] is NAIVE


def test_mutation_naive_check_in_retrieve_is_caught() -> None:
    mutant = mutated_module(connector_module, ("        require_aware(now, window_end)\n", ""))
    store = FakeStore(Selection((), (), 0))
    # The fake store does not check; without the guard a naive clock reaches it.
    asyncio.run(mutant.KnowledgeConnector(store).retrieve(SITE, START, END, NAIVE))
    assert store.calls == [(SITE.id, NAIVE, END)]


# --- N3: the load-time character screen again at read time -----------------------------------

SCREEN_CASES: dict[str, dict[str, str]] = {
    "not-nfc-text": {"text": "cafe\u0301 entry"},
    "carriage-return": {"text": "line one\r\nline two"},
    "bidi-control": {"text": "safe \u202e text"},
    "zero-width": {"text": "no\u200bspace"},
    "curly-quote": {"text": "\u201cquoted\u201d"},
    "control": {"text": "bell\x07"},
    "newline-in-section": {"section": "Haz\nards"},
    "bidi-in-section": {"section": "Haz\u202eards"},
    "not-nfc-section": {"section": "Cafe\u0301"},
}


def _with(row: ChunkRow, **over: str) -> ChunkRow:
    text = over.get("text", row.text)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return replace(row, text=text, section=over.get("section", row.section), content_sha256=sha)


def _rows(case: str) -> list[ChunkRow]:
    return [_with(chunk("x", n=1), **SCREEN_CASES[case]), chunk("x", n=2)]


def _check(module: Any, rows: list[ChunkRow]) -> Any:
    return module.check(SITE, [ver("x", chunks=2)], rows, 2, NOW, END)


@pytest.mark.parametrize("case", sorted(SCREEN_CASES))
def test_a_chunk_failing_the_screen_drops_its_family(case: str) -> None:
    result = _check(connector_module, _rows(case))
    assert result.fetch.items == ()
    assert result.fetch.issues == (SCREEN_ISSUE,)


def test_screened_text_is_kept_exactly() -> None:
    ok = _with(chunk("x"), text="Caf\u00e9 entry.\nSecond line: (20%) - see [map].")
    (item,) = check(SITE, [ver("x")], [ok], 1, NOW, END).fetch.items
    assert item.value["text"] == ok.text
    assert item.value["section"] == ok.section


def test_unreviewed_unicode_data_refuses_every_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kb_screen, "REVIEWED_UNIDATA_VERSION", "0.0.0")
    result = check(SITE, [ver("x")], [chunk("x")], 1, NOW, END)
    assert result.fetch.items == ()
    assert result.fetch.issues == (SCREEN_ISSUE,)


def test_mutation_screen_not_called_is_caught() -> None:
    mutant = mutated_module(
        connector_module, ("            if not screened(row):\n", "            if False:\n")
    )
    assert len(_check(mutant, _rows("bidi-control")).fetch.items) == 2


def _connector_with(screen_mutant: Any) -> Any:
    copy = mutated_module(connector_module)
    copy.screened = screen_mutant.screened
    return copy


@pytest.mark.parametrize(
    ("edit", "case"),
    [
        pytest.param(
            ("    return kb_screen.allowed_text(row.text, newlines=True) and ", "    return "),
            "not-nfc-text",
            id="text-not-screened",
        ),
        pytest.param(
            ("        row.section, newlines=False\n", "        row.text, newlines=True\n"),
            "bidi-in-section",
            id="section-not-screened",
        ),
        pytest.param(
            ("        row.section, newlines=False\n", "        row.section, newlines=True\n"),
            "newline-in-section",
            id="section-allows-newlines",
        ),
        pytest.param(
            ("        return False\n    return kb_screen", "        pass\n    return kb_screen"),
            None,
            id="unreviewed-unicode-trusted",
        ),
    ],
)
def test_mutation_screen_is_caught(
    edit: tuple[str, str], case: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = _connector_with(mutated_module(items_module, edit))
    if case is None:
        monkeypatch.setattr(kb_screen, "REVIEWED_UNIDATA_VERSION", "0.0.0")
        rows = [chunk("x", n=1), chunk("x", n=2)]
    else:
        rows = _rows(case)
    assert len(_check(mutant, rows).fetch.items) == 2
