"""The knowledge connector's code layer (ADR 0022 decisions 2-4, 6, 12, 14, 16) against a fake
store: the SQL choice is checked again in code, bounds are re-checked, items have the decision 6
shape, failures are fixed-string issues, nothing from the corpus is logged. Mutation checks edit
copies of `rag.connector`, `rag.items` and `rag.store`."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError
from tests.safety.kb_fakes import mutated_module

from divesafe.data import ConnectorError
from divesafe.data.sites import TIOMAN_ISLAND
from divesafe.domain import (
    MAX_KNOWLEDGE_ITEMS,
    DataCategory,
    DataKind,
    DataQuality,
    EvidenceItem,
)
from divesafe.rag import connector as connector_module
from divesafe.rag import items as items_module
from divesafe.rag import store as store_module
from divesafe.rag.connector import (
    DISAGREEMENT_ISSUE,
    MALFORMED_ID_ISSUE,
    SCHEMA_ISSUE,
    TOO_LONG_ISSUE,
    KnowledgeConnector,
    bound_issue,
    check,
)
from divesafe.rag.items import ALL_SITES_NOTE, record_form, run_item
from divesafe.rag.selection import (
    ALL_EXPIRED_ISSUE,
    CLOCK_ISSUE,
    UNCHECKED_ISSUE,
    Note,
    NoteCode,
    VersionRow,
)
from divesafe.rag.store import (
    STORE_MALFORMED,
    STORE_TIMED_OUT,
    STORE_UNAVAILABLE,
    ChunkRow,
    KnowledgeStoreError,
    PostgresKnowledgeStore,
    Selection,
    chunk_row,
    version_row,
)

pytestmark = pytest.mark.safety

KL = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 15, 10, 0, tzinfo=KL)
TODAY = date(2026, 6, 15)
START = NOW + timedelta(hours=1)
END = NOW + timedelta(hours=3)
DAY = timedelta(days=1)
SITE = TIOMAN_ISLAND
TITLE = "Secret Title Of The Document"


def ver(
    doc: str,
    version: int = 1,
    *,
    site: str | None = None,
    chunks: int = 1,
    checked: bool = True,
    withdrawn: bool = False,
    approved_on: date | None = TODAY - 5 * DAY,
    eff: date = TODAY - 10 * DAY,
    rev: date = TODAY + 90 * DAY,
) -> VersionRow:
    latest = "WITHDRAWN" if withdrawn else ("APPROVED" if approved_on else "DRAFT")
    return VersionRow(
        doc,
        version,
        site,
        site is None,
        eff,
        rev,
        True,
        approved_on is not None,
        checked and approved_on is not None,
        withdrawn,
        approved_on,
        latest,
        chunks,
    )


def chunk(
    doc: str,
    version: int = 1,
    n: int = 1,
    *,
    site: str | None = None,
    text: str | None = None,
    rev: date = TODAY + 90 * DAY,
) -> ChunkRow:
    body = text if text is not None else f"Text of {doc} v{version} chunk {n}."
    return ChunkRow(
        document_id=doc,
        version=version,
        chunk=n,
        section="Hazards",
        text=body,
        content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        origin="dive_professional",
        licence="Synthetic test licence",
        attribution="A. Person",
        author_role="dive_professional",
        approver_role="dive_professional",
        site_id=site,
        all_sites=site is None,
        effective_date=TODAY - 10 * DAY,
        review_by_date=rev,
    )


class FakeStore:
    def __init__(self, selection: Selection | Exception) -> None:
        self.selection = selection
        self.calls: list[tuple[str, datetime, datetime]] = []

    def select(self, site_id: str, now: datetime, window_end: datetime) -> Selection:
        self.calls.append((site_id, now, window_end))
        if isinstance(self.selection, Exception):
            raise self.selection
        return self.selection


def run(versions: list[VersionRow], chunks: list[ChunkRow], available: int | None = None) -> Any:
    sel = Selection(tuple(versions), tuple(chunks), len(chunks) if available is None else available)
    return asyncio.run(KnowledgeConnector(FakeStore(sel)).retrieve(SITE, START, END, NOW))


def ids(result: Any) -> list[str]:
    return [i.id for i in result.fetch.items]


# --- shape --------------------------------------------------------------------------------


def test_a_site_item_has_the_decision_6_shape() -> None:
    result = run([ver("hazards", site=SITE.id)], [chunk("hazards", site=SITE.id)])
    assert result.fetch.issues == ()
    assert result.notes == ()
    (item,) = result.fetch.items
    assert item.id == "kb:hazards:1:1"
    assert item.category is DataCategory.SITE_INFORMATION
    assert item.source == "kb:hazards"
    assert item.source_version == "1"
    assert item.data_kind is DataKind.KNOWLEDGE
    assert item.is_forecast is False
    assert item.upstream == () and item.upstream_known is False
    assert item.quality is DataQuality.DEGRADED
    assert item.quality_notes and ALL_SITES_NOTE not in item.quality_notes
    assert [t.step for t in item.transformations] == ["chunked"]
    assert item.location == SITE.location
    assert item.retrieved_at == NOW
    assert item.valid_at == datetime(2026, 6, 5, 0, 0, tzinfo=KL)
    assert item.valid_until == datetime(2026, 9, 14, 0, 0, tzinfo=KL)
    assert set(item.value) == {
        "text",
        "document_id",
        "version",
        "section",
        "chunk",
        "content_sha256",
        "origin",
        "licence",
        "attribution",
        "author_role",
        "approver_role",
    }
    assert items_module.scope(item) == "site"


def test_an_all_sites_item_has_no_location_and_a_scope_marker() -> None:
    (item,) = run([ver("general")], [chunk("general")]).fetch.items
    assert item.category is DataCategory.LOCAL_GUIDANCE
    assert item.location is None
    assert ALL_SITES_NOTE in item.quality_notes
    assert items_module.scope(item) == "all_sites"


def test_the_record_form_keeps_id_version_chunk_and_hash_only() -> None:
    (item,) = run([ver("general")], [chunk("general")]).fetch.items
    stored = record_form(item)
    assert stored.value == {
        "document_id": "general",
        "version": 1,
        "chunk": 1,
        "content_sha256": item.value["content_sha256"],
    }
    assert stored.source == "kb:general"
    assert stored.model_dump(exclude={"value"}) == item.model_dump(exclude={"value"})
    EvidenceItem.model_validate(stored.model_dump())  # still a valid item
    other = EvidenceItem.model_validate({**item.model_dump(), "id": "weather:x", "value": {"a": 1}})
    assert record_form(other) is other


def test_no_title_reaches_an_item_or_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "Unique chunk body that must never be logged."
    result = run([ver("general")], [chunk("general", text=secret)])
    dumped = result.fetch.items[0].model_dump_json()
    assert TITLE not in dumped
    assert "title" not in dumped
    assert secret not in caplog.text
    assert "general" not in caplog.text  # no document id either
    assert "Hazards" not in caplog.text


# --- selection checked again in code ------------------------------------------------------


# Each case: the store's summary, plus chunks SQL should NOT have returned. Code drops them all.
INELIGIBLE = {
    "other-site": ([ver("x", site="other-site")], [chunk("x", site="other-site")]),
    "draft": ([ver("x", approved_on=None)], [chunk("x")]),
    "withdrawn": ([ver("x", withdrawn=True)], [chunk("x")]),
    "expired": (
        [ver("x", rev=TODAY - DAY, eff=TODAY - 30 * DAY)],
        [chunk("x", rev=TODAY - DAY)],
    ),
    "future-effective": ([ver("x", eff=TODAY + 5 * DAY)], [chunk("x")]),
    "approval-in-future": ([ver("x", approved_on=TODAY + DAY)], [chunk("x")]),
    "unchecked": ([ver("x", checked=False)], [chunk("x")]),
    "unchecked-withdrawn": ([ver("x", checked=False, withdrawn=True)], [chunk("x")]),
    "lower-under-unchecked": (
        [ver("x", 1), ver("x", 2, checked=False)],
        [chunk("x", 1)],
    ),
    "lower-under-withdrawn": ([ver("x", 1), ver("x", 2, withdrawn=True)], [chunk("x", 1)]),
    "lower-under-clock": ([ver("x", 1), ver("x", 2, approved_on=TODAY + DAY)], [chunk("x", 1)]),
    "not-in-summary": ([], [chunk("x")]),
}


@pytest.mark.parametrize("name", sorted(INELIGIBLE))
def test_the_code_drops_every_ineligible_row_sql_returns(name: str) -> None:
    versions, chunks = INELIGIBLE[name]
    result = run(versions, chunks)
    assert result.fetch.items == ()
    assert DISAGREEMENT_ISSUE in result.fetch.issues


def test_a_lower_version_is_never_returned_beside_its_unchecked_successor() -> None:
    result = run([ver("x", 1), ver("x", 2, checked=False)], [chunk("x", 1)])
    assert ids(result) == []
    assert result.fetch.issues == (UNCHECKED_ISSUE, DISAGREEMENT_ISSUE)


def test_issue_families_raise_their_fixed_issue() -> None:
    result = run([ver("a", approved_on=TODAY + DAY), ver("b", checked=False)], [])
    assert result.fetch.issues == (CLOCK_ISSUE, UNCHECKED_ISSUE)
    assert result.fetch.items == ()


def test_a_missing_chunk_is_a_disagreement() -> None:
    result = run([ver("x", chunks=2)], [chunk("x", n=1)])
    assert result.fetch.items == ()
    assert result.fetch.issues == (DISAGREEMENT_ISSUE,)


def test_a_duplicate_chunk_is_a_disagreement() -> None:
    result = run([ver("x", chunks=2)], [chunk("x"), chunk("x")])
    assert result.fetch.items == ()
    assert DISAGREEMENT_ISSUE in result.fetch.issues


def test_the_result_is_ordered_by_document_version_chunk() -> None:
    rows = [chunk(d, 1, n) for d in ("b-doc", "a-doc") for n in (2, 1, 3)]
    random.Random(7).shuffle(rows)
    result = run([ver("a-doc", chunks=3), ver("b-doc", chunks=3)], rows)
    assert ids(result) == [f"kb:{d}:1:{n}" for d in ("a-doc", "b-doc") for n in (1, 2, 3)]


def test_another_sites_versions_are_ignored_in_code() -> None:
    result = run([ver("x", site="other-site", checked=False)], [])
    assert result.fetch.issues == ()
    assert result.notes == (Note(NoteCode.NO_DOCUMENTS, 1),)


# --- notes --------------------------------------------------------------------------------


def test_notes() -> None:
    assert run([], []).notes == (Note(NoteCode.NO_DOCUMENTS, 1),)
    assert run([ver("x", withdrawn=True)], []).notes == (Note(NoteCode.WITHDRAWN, 1),)
    assert run([ver("x", approved_on=None)], []).notes == (Note(NoteCode.NOT_IN_FORCE, 1),)
    live = run(
        [ver("a"), ver("b", rev=TODAY - DAY, eff=TODAY - 30 * DAY)],
        [chunk("a")],
    )
    assert live.notes == (Note(NoteCode.EXPIRED, 1),)
    assert ids(live) == ["kb:a:1:1"]
    assert live.fetch.issues == ()


def test_zero_items_and_no_issue_is_always_a_note() -> None:
    for versions in ([], [ver("x", withdrawn=True)], [ver("x", eff=TODAY + 5 * DAY)]):
        result = run(versions, [])
        assert result.fetch.items == () and result.fetch.issues == ()
        assert result.notes


# --- bounds -------------------------------------------------------------------------------


def _many(n: int) -> tuple[list[VersionRow], list[ChunkRow]]:
    return [ver("big", chunks=n)], [chunk("big", n=i) for i in range(1, n + 1)]


def test_below_the_bound_is_no_issue() -> None:
    versions, chunks = _many(MAX_KNOWLEDGE_ITEMS - 1)
    result = run(versions, chunks)
    assert len(result.fetch.items) == MAX_KNOWLEDGE_ITEMS - 1
    assert result.fetch.issues == ()


def test_reaching_the_bound_is_an_issue_with_both_counts() -> None:
    # Owner decision 2026-11-09 (B1): 20 or more available chunks raise the issue.
    versions, chunks = _many(MAX_KNOWLEDGE_ITEMS)
    result = run(versions, chunks)
    assert len(result.fetch.items) == MAX_KNOWLEDGE_ITEMS
    assert result.fetch.issues == (bound_issue(MAX_KNOWLEDGE_ITEMS, MAX_KNOWLEDGE_ITEMS),)
    assert result.fetch.issues[0] == (
        "knowledge store: the per-run bound of 20 chunks was reached (20 returned of 20 available)"
    )


def test_more_than_the_bound_is_cut_and_reported() -> None:
    versions, chunks = _many(25)
    result = run(versions, chunks[:MAX_KNOWLEDGE_ITEMS], available=25)
    assert len(result.fetch.items) == MAX_KNOWLEDGE_ITEMS
    assert result.fetch.issues == (bound_issue(20, 25),)


def test_a_store_returning_past_the_bound_is_cut_in_code() -> None:
    versions, chunks = _many(25)
    result = run(versions, chunks, available=25)
    assert len(result.fetch.items) == MAX_KNOWLEDGE_ITEMS
    assert result.fetch.issues == (bound_issue(20, 25),)


def test_an_over_long_chunk_is_dropped_with_a_fixed_issue() -> None:
    # Risk review S1: the whole family goes, never the rest of it as if complete.
    result = run([ver("x", chunks=2)], [chunk("x", n=1, text="a" * 1501), chunk("x", n=2)])
    assert ids(result) == []
    assert result.fetch.issues == (TOO_LONG_ISSUE,)
    ok = run([ver("x")], [chunk("x", text="a" * 1500)])
    assert len(ok.fetch.items) == 1 and ok.fetch.issues == ()


def test_a_malformed_id_is_dropped_with_a_fixed_issue() -> None:
    result = run([ver("Bad-Id", chunks=1)], [chunk("Bad-Id")])
    assert result.fetch.items == ()
    assert result.fetch.issues == (MALFORMED_ID_ISSUE,)


def test_a_row_failing_the_item_schema_is_dropped_with_a_fixed_issue() -> None:
    bad = replace(chunk("x"), effective_date=TODAY + 400 * DAY)  # valid_until before valid_at
    result = check(SITE, [ver("x")], [bad], 1, NOW, END)
    assert result.fetch.items == ()
    assert result.fetch.issues == (SCHEMA_ISSUE,)


# --- store failures -----------------------------------------------------------------------


@pytest.mark.parametrize("text", [STORE_UNAVAILABLE, STORE_TIMED_OUT, STORE_MALFORMED])
def test_a_store_error_propagates_as_a_fixed_connector_error(text: str) -> None:
    store = FakeStore(KnowledgeStoreError(text))
    with pytest.raises(ConnectorError) as raised:
        asyncio.run(KnowledgeConnector(store).fetch(SITE, START, END, NOW))
    assert str(raised.value) == text


class _Conn:
    def __init__(self, fail: BaseException | None = None, rows: Any = ()) -> None:
        self.fail = fail
        self.rows = rows
        self.log: list[Any] = []
        self.isolation_level: Any = None
        self.read_only = False

    def execute(self, query: str, params: Any = None) -> _Conn:
        self.log.append((query, params))
        if self.fail is not None and query not in store_module._SESSION_SETUP:
            raise self.fail
        return self

    def fetchall(self) -> Any:
        return self.rows

    def commit(self) -> None:
        self.log.append("COMMIT")

    def rollback(self) -> None:
        self.log.append("ROLLBACK")

    def close(self) -> None:
        self.log.append("CLOSE")


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (psycopg.OperationalError("password authentication failed for x"), STORE_UNAVAILABLE),
        (psycopg.errors.QueryCanceled("canceling statement due to timeout"), STORE_TIMED_OUT),
        (psycopg.errors.LockNotAvailable("could not obtain lock"), STORE_TIMED_OUT),
        (psycopg.errors.InsufficientPrivilege("permission denied"), STORE_UNAVAILABLE),
    ],
)
def test_driver_errors_become_fixed_strings(
    monkeypatch: pytest.MonkeyPatch, error: Exception, text: str
) -> None:
    conn = _Conn(fail=error)
    monkeypatch.setattr(store_module.psycopg, "connect", lambda *a, **k: conn)
    with pytest.raises(KnowledgeStoreError) as raised:
        PostgresKnowledgeStore("postgresql://u@db.invalid/x_test").select(SITE.id, NOW, END)
    assert str(raised.value) == text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert "CLOSE" in conn.log


def test_a_failed_connect_is_a_fixed_string(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise psycopg.OperationalError("could not connect to server at 10.1.2.3")

    monkeypatch.setattr(store_module.psycopg, "connect", refuse)
    with pytest.raises(KnowledgeStoreError) as raised:
        PostgresKnowledgeStore("postgresql://u@db.invalid/x_test").select(SITE.id, NOW, END)
    assert str(raised.value) == STORE_UNAVAILABLE


def test_the_connection_has_options_timeouts_and_a_read_only_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn()
    seen: list[tuple[Any, dict[str, Any]]] = []

    def connect(dsn: str, **kwargs: Any) -> _Conn:
        seen.append((dsn, kwargs))
        return conn

    monkeypatch.setattr(store_module.psycopg, "connect", connect)
    PostgresKnowledgeStore("postgresql://u@db.invalid/x_test").select(SITE.id, NOW, END)
    ((_, kwargs),) = seen
    assert kwargs == {
        "connect_timeout": store_module.CONNECT_TIMEOUT_SECONDS,
        "options": "-c statement_timeout=5000 -c lock_timeout=3000",
    }
    assert conn.log[:4] == [
        ("SET DateStyle = 'ISO, MDY'", None),
        ("SET client_encoding = 'UTF8'", None),
        ("SET search_path = pg_catalog, pg_temp", None),
        "COMMIT",
    ]
    assert conn.read_only is True
    assert conn.isolation_level is psycopg.IsolationLevel.REPEATABLE_READ
    assert conn.log[-2:] == ["ROLLBACK", "CLOSE"]


def test_a_session_that_cannot_be_set_up_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing(_Conn):
        def execute(self, query: str, params: Any = None) -> _Conn:
            raise psycopg.OperationalError("server closed the connection")

    conn = Failing()
    monkeypatch.setattr(store_module.psycopg, "connect", lambda *a, **k: conn)
    with pytest.raises(KnowledgeStoreError):
        PostgresKnowledgeStore("postgresql://u@db.invalid/x_test").select(SITE.id, NOW, END)
    assert conn.log == ["CLOSE"]


# --- what reaches SQL ---------------------------------------------------------------------


def test_only_site_id_now_and_window_end_reach_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn()
    monkeypatch.setattr(store_module.psycopg, "connect", lambda *a, **k: conn)
    hostile = SITE.model_copy(update={"name": "'; DROP TABLE x; --", "area_names": ("$$",)})
    connector = KnowledgeConnector(PostgresKnowledgeStore("postgresql://u@db.invalid/x_test"))
    asyncio.run(connector.fetch(hostile, START, END, NOW))
    queries = [entry for entry in conn.log if isinstance(entry, tuple) and entry[1] is not None]
    assert [q for q, _ in queries] == [store_module.VERSIONS_SQL, store_module.SELECTION_SQL]
    assert queries[0][1] == {"site_id": SITE.id}
    assert queries[1][1] == {"site_id": SITE.id, "now": NOW, "window_end": END}
    for query, _ in queries:
        assert "'; DROP" not in query and "$$" not in query


def test_the_sql_text_follows_decision_3() -> None:
    for sql in (store_module.VERSIONS_SQL, store_module.SELECTION_SQL):
        assert "+08" not in sql
        assert "now()" not in sql.lower() and "current_" not in sql.lower()
        assert "clock_timestamp" not in sql and "localtimestamp" not in sql.lower()
        assert "title" not in sql
        for table in ("kb_documents", "kb_chunks", "kb_status_events"):
            assert f"public.{table}" in sql
            assert sql.count(f"public.{table}") == sql.count(f"ONLY public.{table}")
    assert "ORDER BY c.document_id, c.version, c.chunk" in store_module.SELECTION_SQL
    assert f"LIMIT {MAX_KNOWLEDGE_ITEMS}" in store_module.SELECTION_SQL
    assert "AT TIME ZONE 'Asia/Kuala_Lumpur'" in store_module.SELECTION_SQL


# --- row parsing --------------------------------------------------------------------------


def _summary_tuple(**over: Any) -> tuple[Any, ...]:
    base: dict[str, Any] = {
        "document_id": "x",
        "version": 1,
        "site_id": None,
        "all_sites": True,
        "eff": TODAY - DAY,
        "rev": TODAY + DAY,
        "has": True,
        "approved": True,
        "checked": True,
        "withdrawn": False,
        "on": TODAY - DAY,
        "latest": "APPROVED",
        "chunks": 1,
    }
    base.update(over)
    return tuple(base.values())


def test_summary_rows_are_type_checked() -> None:
    assert version_row(_summary_tuple()).version == 1
    for bad in (
        {"version": "1"},
        {"version": True},
        {"all_sites": 1},
        {"eff": datetime(2026, 1, 1, tzinfo=KL)},
        {"latest": "PUBLISHED"},
        {"site_id": None, "all_sites": False},
        {"on": None},
    ):
        with pytest.raises(KnowledgeStoreError) as raised:
            version_row(_summary_tuple(**bad))
        assert str(raised.value) == STORE_MALFORMED
    with pytest.raises(KnowledgeStoreError):
        version_row(_summary_tuple()[:-1])


def _chunk_tuple(text: str = "hello", sha: str | None = None) -> tuple[Any, ...]:
    digest = sha or hashlib.sha256(text.encode()).hexdigest()
    return (
        "x", 1, 1, "S", text, digest, "dive_professional", "L", None,
        "dive_professional", "dive_professional", None, True, TODAY, TODAY, 1,
    )  # fmt: skip


def test_chunk_rows_are_type_checked_and_hash_checked() -> None:
    row, available = chunk_row(_chunk_tuple())
    assert row.text == "hello" and available == 1
    with pytest.raises(KnowledgeStoreError):
        chunk_row(_chunk_tuple(sha="0" * 64))
    with pytest.raises(KnowledgeStoreError):
        chunk_row(_chunk_tuple()[:-1])
    with pytest.raises(KnowledgeStoreError):
        chunk_row(("x", "1", *_chunk_tuple()[2:]))


def test_run_item_refuses_a_malformed_id() -> None:
    with pytest.raises(ValueError):
        run_item(chunk("has:colon"), SITE, NOW)
    with pytest.raises(ValidationError):
        run_item(replace(chunk("x"), effective_date=TODAY + 400 * DAY), SITE, NOW)


# --- mutation checks ----------------------------------------------------------------------


def _mut_run(module: Any, versions: list[VersionRow], chunks: list[ChunkRow], n: int) -> Any:
    return module.check(SITE, versions, chunks, n, NOW, END)


CONNECTOR_MUTATIONS = [
    pytest.param(
        ("            (row.document_id, row.version) not in returned\n", "            False\n"),
        "wrong-version",
        id="code-check-on-version-removed",
    ),
    pytest.param(
        ("            or not (row.all_sites or row.site_id == site.id)\n", ""),
        "other-site-chunk-only",
        id="code-check-on-site-removed",
    ),
    pytest.param(
        ("    disagree = expected != available\n", "    disagree = False\n"),
        "count-mismatch",
        id="count-cross-check-removed",
    ),
    pytest.param(
        (
            "    if len(agreed) not in (expected, min(expected, MAX_KNOWLEDGE_ITEMS)):\n",
            "    if False:\n",
        ),
        "missing-chunk",
        id="row-count-check-removed",
    ),
    pytest.param(
        (
            "    results = classify((v for v in versions if v.applies_to(site.id)), now, "
            "window_end)",
            "    results = classify(versions, now, window_end)",
        ),
        "other-site-unchecked",
        id="code-site-filter-removed",
    ),
]

MUT_CASES: dict[str, tuple[list[VersionRow], list[ChunkRow], int]] = {
    # v2 governs; SQL returned v1's chunk with matching counts.
    "wrong-version": ([ver("x", 1), ver("x", 2)], [chunk("x", 1)], 1),
    "lower-under-unchecked": ([ver("x", 1), ver("x", 2, checked=False)], [chunk("x", 1)], 0),
    # The summary says the site document is returned, SQL's row claims another site.
    "other-site-chunk-only": ([ver("x", site=SITE.id)], [chunk("x", site="other-site")], 1),
    "missing-chunk": ([ver("x", chunks=2)], [chunk("x", n=1)], 2),
    # SQL counted a chunk the summary does not know.
    "count-mismatch": ([ver("x", chunks=2)], [chunk("x", n=1), chunk("x", n=2)], 3),
    "other-site-unchecked": ([ver("x", site="other-site", checked=False)], [], 0),
}


@pytest.mark.parametrize(("edit", "case"), CONNECTOR_MUTATIONS)
def test_connector_mutation_is_caught(edit: tuple[str, str], case: str) -> None:
    versions, chunks, n = MUT_CASES[case]
    real = _mut_run(connector_module, versions, chunks, n)
    mutant = _mut_run(mutated_module(connector_module, edit), versions, chunks, n)
    assert (real.fetch.items, real.fetch.issues) != (mutant.fetch.items, mutant.fetch.issues)
    # The real code fails closed in every case: nothing returned.
    assert real.fetch.items == ()


def test_mutation_bound_raised_only_above_twenty_is_caught() -> None:
    versions, chunks = _many(MAX_KNOWLEDGE_ITEMS)
    mutant = mutated_module(
        connector_module,
        ("    if total >= MAX_KNOWLEDGE_ITEMS or", "    if total > MAX_KNOWLEDGE_ITEMS or"),
    )
    assert _mut_run(connector_module, versions, chunks, 20).fetch.issues
    assert _mut_run(mutant, versions, chunks, 20).fetch.issues == ()


def test_mutation_no_cut_in_code_is_caught() -> None:
    versions, chunks = _many(25)
    mutant = mutated_module(connector_module, ("        items = items[:MAX_KNOWLEDGE_ITEMS]\n", ""))
    assert len(_mut_run(mutant, versions, chunks, 25).fetch.items) == 25
    assert len(_mut_run(connector_module, versions, chunks, 25).fetch.items) == 20


def test_mutation_no_length_recheck_is_caught() -> None:
    rows = [chunk("x", text="a" * 1501)]
    mutant = mutated_module(
        connector_module,
        ("        if len(row.text) > MAX_CHUNK_CHARACTERS:\n", "        if False:\n"),
    )
    assert _mut_run(connector_module, [ver("x")], rows, 1).fetch.items == ()
    assert len(_mut_run(mutant, [ver("x")], rows, 1).fetch.items) == 1


def test_mutation_location_on_all_sites_items_is_caught() -> None:
    mutant = mutated_module(
        items_module,
        (
            "        location=None if row.all_sites else site.location,\n",
            "        location=site.location,\n",
        ),
    )
    assert run_item(chunk("x"), SITE, NOW).location is None
    assert mutant.run_item(chunk("x"), SITE, NOW).location is not None


def test_mutation_record_form_keeping_text_is_caught() -> None:
    mutant = mutated_module(
        items_module,
        (
            'RECORD_VALUE_KEYS = ("document_id", "version", "chunk", "content_sha256")',
            'RECORD_VALUE_KEYS = ("document_id", "version", "chunk", "content_sha256", "text")',
        ),
    )
    item = run_item(chunk("x"), SITE, NOW)
    assert "text" not in record_form(item).value
    assert "text" in mutant.record_form(item).value


def test_mutation_driver_text_in_the_error_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    mutant = mutated_module(
        store_module,
        (
            "            raise KnowledgeStoreError(STORE_UNAVAILABLE) from None\n",
            "            raise KnowledgeStoreError(str(sys.exc_info()[1])) from None\n",
        ),
    )
    import sys

    mutant.sys = sys
    conn = _Conn(fail=psycopg.OperationalError("driver detail"))
    monkeypatch.setattr(mutant.psycopg, "connect", lambda *a, **k: conn)
    with pytest.raises(ConnectorError) as raised:
        mutant.PostgresKnowledgeStore("postgresql://u@db.invalid/x_test").select(SITE.id, NOW, END)
    assert str(raised.value) != STORE_UNAVAILABLE


def test_mutation_hash_check_removed_is_caught() -> None:
    mutant = mutated_module(
        store_module,
        (
            '    if hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() '
            "!= chunk.content_sha256:\n",
            "    if False:\n",
        ),
    )
    with pytest.raises(KnowledgeStoreError):
        chunk_row(_chunk_tuple(sha="0" * 64))
    mutant.chunk_row(_chunk_tuple(sha="0" * 64))


# --- a family is used whole or not at all (risk review S1) --------------------------------


def _bad_schema(row: ChunkRow) -> ChunkRow:
    return replace(row, effective_date=TODAY + 400 * DAY)  # valid_until before valid_at


def test_a_dropped_chunk_drops_its_whole_family_and_only_that_family() -> None:
    versions = [ver("a-doc", chunks=2), ver("x", chunks=2)]
    rows = [chunk("a-doc", n=1), chunk("a-doc", n=2)]
    for bad, issue in (
        (replace(chunk("x", n=1), text="a" * 1501, content_sha256="0" * 64), TOO_LONG_ISSUE),
        (_bad_schema(chunk("x", n=1)), SCHEMA_ISSUE),
    ):
        result = check(SITE, versions, [*rows, bad, chunk("x", n=2)], 4, NOW, END)
        assert ids(result) == ["kb:a-doc:1:1", "kb:a-doc:1:2"]
        assert result.fetch.issues == (issue,)


def test_a_malformed_id_drops_its_whole_family() -> None:
    result = check(SITE, [ver("Bad-Id", chunks=2)], [chunk("Bad-Id", n=1), chunk("Bad-Id", n=2)],
                   2, NOW, END)  # fmt: skip
    assert result.fetch.items == ()
    assert result.fetch.issues == (MALFORMED_ID_ISSUE,)


def test_mutation_per_chunk_drop_is_caught() -> None:
    mutant = mutated_module(
        connector_module, ("        if failed:\n            dropped.extend(failed)\n        else:\n"
                           "            items.extend(built)\n",
                           "        dropped.extend(failed)\n        items.extend(built)\n"),
    )  # fmt: skip
    rows = [chunk("x", n=1, text="a" * 1501), chunk("x", n=2)]
    assert _mut_run(connector_module, [ver("x", chunks=2)], rows, 2).fetch.items == ()
    assert len(_mut_run(mutant, [ver("x", chunks=2)], rows, 2).fetch.items) == 1


# --- retrieve reads the window END (risk review S2) ---------------------------------------


def _retrieve(module: Any, selection: Selection, start: datetime, end: datetime) -> Any:
    store = FakeStore(selection)
    result = asyncio.run(module.KnowledgeConnector(store).retrieve(SITE, start, end, NOW))
    return store, result


def test_retrieve_passes_the_window_end_to_the_store() -> None:
    store, _ = _retrieve(connector_module, Selection((), (), 0), START, END)
    assert store.calls == [(SITE.id, NOW, END)]


# Review-by today: usable until 00:00 local tomorrow. The window starts before it, ends after.
EXPIRY = datetime(2026, 6, 15, 23, 0, tzinfo=KL), datetime(2026, 6, 16, 0, 30, tzinfo=KL)


def test_a_window_ending_past_review_by_is_the_all_expired_issue() -> None:
    sel = Selection((ver("x", rev=TODAY),), (), 0)
    _, result = _retrieve(connector_module, sel, *EXPIRY)
    assert result.fetch.items == ()
    assert result.fetch.issues == (ALL_EXPIRED_ISSUE,)


def test_mutation_window_start_at_the_store_call_is_caught() -> None:
    mutant = mutated_module(
        connector_module,
        ("self._store.select, site.id, now, window_end)", "self._store.select, site.id, now, "
         "window_start)"),
    )  # fmt: skip
    store, _ = _retrieve(mutant, Selection((), (), 0), START, END)
    assert store.calls == [(SITE.id, NOW, START)]


def test_mutation_window_start_at_check_is_caught() -> None:
    mutant = mutated_module(
        connector_module,
        ("selection.available, now, window_end", "selection.available, now, window_start"),
    )
    sel = Selection((ver("x", rev=TODAY),), (), 0)
    _, result = _retrieve(mutant, sel, *EXPIRY)
    assert result.fetch.issues != (ALL_EXPIRED_ISSUE,)


# --- risk review n1: the chunk row's own review-by is re-checked --------------------------


def test_a_chunk_row_whose_review_by_disagrees_with_the_summary_is_a_disagreement() -> None:
    result = check(SITE, [ver("x")], [chunk("x", rev=TODAY - DAY)], 1, NOW, END)
    assert result.fetch.items == ()
    assert result.fetch.issues == (DISAGREEMENT_ISSUE,)
    mutant = mutated_module(
        connector_module, ("            or not usable_for(row.review_by_date, window_end)\n", "")
    )
    assert len(_mut_run(mutant, [ver("x")], [chunk("x", rev=TODAY - DAY)], 1).fetch.items) == 1


# --- risk review n3: a returned version with no chunks is malformed -----------------------


def test_a_returned_version_with_no_chunks_is_a_malformed_store() -> None:
    with pytest.raises(KnowledgeStoreError) as raised:
        check(SITE, [ver("x", chunks=0)], [], 0, NOW, END)
    assert str(raised.value) == STORE_MALFORMED
    mutant = mutated_module(
        connector_module,
        ("    if any(n == 0 for n in returned.values()):\n", "    if False:\n"),
    )
    result = _mut_run(mutant, [ver("x", chunks=0)], [], 0)
    assert result.fetch.items == () and result.fetch.issues == ()
