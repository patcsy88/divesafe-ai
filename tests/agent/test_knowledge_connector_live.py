"""The knowledge store's SQL against a real PostgreSQL (ADR 0022 decisions 2-4, 14): the SQL alone
returns none of the ineligible rows, each SQL predicate has a mutation check, the run clock (not
the database clock) decides, the zone is the named one, and the full connector agrees with SQL.
Needs DIVESAFE_TEST_DATABASE_URL (a database named *_test); skipped without it.

Lives in tests/agent because tests/integration was not writable for this builder (builder
choice, ADR 0022). No database or role is created or dropped. Rows can never be deleted, so each
run uses fresh document ids under a fresh site id and NO all-sites rows (they would apply to
every site forever); the all-sites path is covered offline in test_knowledge_connector.py."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from tests.integration.test_knowledge_store import (
    _add_doc,
    _add_event,
    _drive,
    _ensure_chunk,
    _kl_today,
    _run,
)
from tests.unit.test_repository_contract import TEST_DSN as DSN

from divesafe.domain import DiveSite, KnowledgeNote, KnowledgeNoteCode
from divesafe.rag import store as store_module
from divesafe.rag.connector import KnowledgeConnector
from divesafe.rag.selection import CLOCK_ISSUE, KL, UNCHECKED_ISSUE
from divesafe.rag.store import PostgresKnowledgeStore

# `integration` only, like the other live tests: `-m safety` must never skip, and this needs a DSN.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
]

DAY = timedelta(days=1)
NAMES = (
    "good", "other", "draft", "withdrawn", "expired", "future", "unchecked", "withdrawn-above",
    "draftw-above", "future-above",
)  # fmt: skip
# Each at version 1: a clean version, a draft withdrawn above a clean one, a future-effective
# version above a clean one.
RETURNED = ("good", "draftw-above", "future-above")


@pytest.fixture(scope="module")
def db() -> dict[str, Any]:
    assert DSN is not None
    tag = uuid.uuid4().hex[:8]
    site = DiveSite(
        id=f"t-site-{tag}", name="Synthetic", latitude=0, longitude=0, coordinate_source="test"
    )
    ids = {n: f"t{tag}{i:02d}" for i, n in enumerate(NAMES)}
    today = _kl_today(DSN)
    _seed(DSN, site.id, ids, today)
    return {"dsn": DSN, "site": site, "ids": ids, "today": today}


def _seed(dsn: str, site: str, ids: dict[str, str], today: date) -> None:
    def doc(name: str, version: int = 1, **over: Any) -> None:
        row: dict[str, Any] = {
            "effective_date": today - 10 * DAY,
            "review_by_date": today + 90 * DAY,
            "site_id": site,
            "all_sites": False,
        }
        row.update(over)
        _add_doc(dsn, ids[name], version, **row)

    def approved(name: str, version: int = 1, **over: Any) -> None:
        doc(name, version, **over)
        _drive(dsn, ids[name], ("DRAFT", "APPROVED"), version)

    approved("good")
    approved("other", site_id=f"{site}-other")
    doc("draft")
    _drive(dsn, ids["draft"], ("DRAFT",))
    doc("withdrawn")
    _drive(dsn, ids["withdrawn"], ("DRAFT", "APPROVED", "WITHDRAWN"))
    # Past review-by cannot be approved (a trigger), so it is usable today and expired by a
    # window that ends after midnight.
    approved("expired", effective_date=today - 30 * DAY, review_by_date=today)
    approved("future", effective_date=today + 5 * DAY)
    # An unchecked v2 above a clean v1: nothing from the family, never v1.
    approved("unchecked")
    doc("unchecked", 2)
    _ensure_chunk(dsn, ids["unchecked"], 2)
    _drive(dsn, ids["unchecked"], ("DRAFT",), 2)
    _add_event(dsn, ids["unchecked"], "APPROVED", 2, hash_checked=False)
    # A withdrawn v2 above a clean v1: nothing, never v1.
    approved("withdrawn-above")
    doc("withdrawn-above", 2)
    _drive(dsn, ids["withdrawn-above"], ("DRAFT", "APPROVED", "WITHDRAWN"), 2)
    # A draft withdrawn without approval does not hide v1.
    approved("draftw-above")
    doc("draftw-above", 2)
    _drive(dsn, ids["draftw-above"], ("DRAFT", "WITHDRAWN"), 2)
    # A future-effective approved v2 does not hide v1.
    approved("future-above")
    doc("future-above", 2, effective_date=today + 5 * DAY)
    _drive(dsn, ids["future-above"], ("DRAFT", "APPROVED"), 2)


def _clock(day: date) -> tuple[datetime, datetime]:
    """23:00 local on `day`, to 00:30 the next day: past the review-by end of `expired`."""
    now = datetime.combine(day, time(23, 0), tzinfo=KL)
    return now, now + timedelta(minutes=90)


def _selected(db: dict[str, Any], sql: str, now: datetime, end: datetime) -> set[tuple[str, int]]:
    rows = _run(db["dsn"], sql, store_module.parameters(db["site"].id, now, end))
    return {(r[0], r[1]) for r in rows}


def _expected(db: dict[str, Any]) -> set[tuple[str, int]]:
    return {(db["ids"][n], 1) for n in RETURNED}


def test_the_sql_alone_returns_only_eligible_versions(db: dict[str, Any]) -> None:
    assert _selected(db, store_module.SELECTION_SQL, *_clock(db["today"])) == _expected(db)


def test_the_run_clock_decides_not_the_database_clock(db: dict[str, Any]) -> None:
    """Every approval is dated today by the database clock; a run whose local date is
    yesterday sees them all as dated after its clock and selects nothing."""
    assert _selected(db, store_module.SELECTION_SQL, *_clock(db["today"] - DAY)) == set()


def test_the_summary_holds_no_title_and_only_this_site(db: dict[str, Any]) -> None:
    rows = _run(db["dsn"], store_module.VERSIONS_SQL, {"site_id": db["site"].id})
    assert {r[0] for r in rows} == set(db["ids"].values()) - {db["ids"]["other"]}
    assert all("Synthetic test document" not in repr(r) for r in rows)
    parsed = {(p.document_id, p.version): p for p in map(store_module.version_row, rows)}
    unchecked = parsed[(db["ids"]["unchecked"], 2)]
    assert unchecked.approved and not unchecked.hash_checked


def test_the_connector_agrees_with_sql(db: dict[str, Any]) -> None:
    # The notes and issues are per site; another run's all-sites rows would apply here too.
    assert _run(db["dsn"], "SELECT count(*) FROM ONLY public.kb_documents WHERE all_sites") == [
        (0,)
    ], "this live test assumes the test database holds no all-sites documents"
    now, end = _clock(db["today"])
    connector = KnowledgeConnector(PostgresKnowledgeStore(db["dsn"]))
    result = asyncio.run(connector.retrieve(db["site"], now, end, now))
    assert [i.id for i in result.fetch.items] == sorted(f"kb:{d}:{v}:1" for d, v in _expected(db))
    assert result.fetch.issues == (UNCHECKED_ISSUE,)
    assert set(result.notes) == {
        KnowledgeNote(code=KnowledgeNoteCode.NOT_IN_FORCE, count=2),
        KnowledgeNote(code=KnowledgeNoteCode.WITHDRAWN, count=2),
        KnowledgeNote(code=KnowledgeNoteCode.EXPIRED, count=1),
    }
    for item in result.fetch.items:
        assert "Synthetic test document" not in item.model_dump_json()
    late_now, late_end = _clock(db["today"] - DAY)
    late = asyncio.run(connector.retrieve(db["site"], late_now, late_end, late_now))
    assert late.fetch.items == ()
    assert CLOCK_ISSUE in late.fetch.issues


SQL_MUTATIONS = [
    pytest.param(("WHERE (d.site_id = %(site_id)s OR d.all_sites)", "WHERE true"), 0, id="site"),
    pytest.param(("    WHERE g.hash_checked\n      AND", "    WHERE"), 0, id="hash-checked"),
    pytest.param(
        ("      AND NOT g.withdrawn\n      AND g.latest = 'APPROVED'\n", ""), 0, id="withdrawn"
    ),
    pytest.param(
        (
            "      AND g.approved_on <= (%(now)s::timestamptz AT TIME ZONE "
            "'Asia/Kuala_Lumpur')::date\n",
            "",
        ),
        -1,
        id="approval-date",
    ),
    pytest.param(
        (
            "      AND %(window_end)s\n          < ((g.review_by_date + 1)::timestamp "
            "AT TIME ZONE 'Asia/Kuala_Lumpur')\n",
            "",
        ),
        0,
        id="review-by",
    ),
    pytest.param(
        (
            "      AND (v.effective_date::timestamp AT TIME ZONE 'Asia/Kuala_Lumpur') "
            "<= %(window_end)s\n",
            "",
        ),
        0,
        id="in-force",
    ),
    pytest.param(("    WHERE v.approved\n      AND", "    WHERE"), 0, id="ever-approved"),
]


@pytest.mark.parametrize(("edit", "shift"), SQL_MUTATIONS)
def test_mutation_each_sql_predicate_is_needed(
    db: dict[str, Any], edit: tuple[str, str], shift: int
) -> None:
    old, new = edit
    sql = store_module.SELECTION_SQL
    assert sql.count(old) == 1, "edit target not unique"
    now, end = _clock(db["today"] + shift * DAY)
    assert _selected(db, sql.replace(old, new), now, end) != _selected(db, sql, now, end)


def test_mutation_numeric_zone_is_caught(db: dict[str, Any]) -> None:
    """`'+08'` is a POSIX zone, UTC-8, 16 hours off. The review-by end of `expired` is 00:00
    local tomorrow, so a window ending at 00:30 has passed it; under the mutant it is 16:00."""
    sql = store_module.SELECTION_SQL
    assert sql.count("'Asia/Kuala_Lumpur'") >= 1
    mutant = sql.replace("'Asia/Kuala_Lumpur'", "'+08'")
    now, end = _clock(db["today"])
    assert _selected(db, sql, now, end) == _expected(db)
    assert _selected(db, mutant, now, end) != _expected(db)
