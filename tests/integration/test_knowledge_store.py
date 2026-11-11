"""Migration 4 against a real PostgreSQL: the knowledge tables are append-only, their insert
triggers enforce the status machine, and the loader role holds only the privileges ADR 0022
gives it. Needs DIVESAFE_TEST_DATABASE_URL (a database named *_test).

The knowledge tables can never be emptied, so every test uses a fresh document id instead."""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import psycopg
import psycopg.conninfo
import pytest
from tests.unit.test_repository_contract import TEST_DSN as DSN

from divesafe.services import migrations as migrations_module
from divesafe.services import postgres as postgres_module
from divesafe.services.migrations import (
    KNOWLEDGE_FUNCTIONS,
    KNOWLEDGE_TABLES,
    KNOWLEDGE_TRIGGERS,
    MIGRATIONS,
    Migration,
    MigrationError,
    migrate,
)
from divesafe.services.postgres import (
    LOADER_ROLE_MISSING,
    TEMP_STILL_HELD,
    LoaderRoleError,
    TempPrivilegeError,
    install_schema,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
]

APP_ROLE = "divesafe_kb_app_test"
LOADER_ROLE = "divesafe_kb_loader_test"
MISSING_ROLE = "divesafe_kb_absent_test"
PASSWORD = "kb-test-only"
FUNCTIONS = (
    "kb_no_update_delete()",
    "kb_no_truncate()",
    "kb_documents_insert()",
    "kb_chunks_insert()",
    "kb_status_events_insert()",
)
SEQUENCE = "public.kb_status_event_order_seq"
EVENT_COLUMNS = ("event_order", "document_id", "version", "status", "hash_checked", "recorded_at")
LOADER_EVENT_COLUMNS = ("document_id", "version", "status", "hash_checked")
STATUSES = ("DRAFT", "APPROVED", "WITHDRAWN")


# --- plumbing ------------------------------------------------------------------------------------


def _role_dsn(user: str) -> str:
    assert DSN is not None
    base = psycopg.conninfo.conninfo_to_dict(DSN)
    base.update(user=user, password=PASSWORD)
    return psycopg.conninfo.make_conninfo(**base)


def _run(
    dsn: str,
    query: str,
    params: Any = None,
    *,
    isolation: psycopg.IsolationLevel | None = None,
) -> list[tuple[Any, ...]]:
    """One statement in its own transaction (committed on success, rolled back on error)."""
    with psycopg.connect(dsn) as conn:
        if isolation is not None:
            conn.isolation_level = isolation
        cur = conn.execute(query, params)  # type: ignore[call-overload]
        return cur.fetchall() if cur.description else []


def _drop_role(conn: psycopg.Connection[Any], name: str) -> None:
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
        conn.execute(f"DROP OWNED BY {name}")  # type: ignore[call-overload]
        conn.execute(f"DROP ROLE {name}")  # type: ignore[call-overload]


@pytest.fixture(scope="module")
def roles() -> Iterator[dict[str, str]]:
    """The schema installed as the owner, plus a real application role and a real loader role."""
    assert DSN is not None
    name = str(psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", ""))
    if not name.endswith("_test"):
        raise RuntimeError("DIVESAFE_TEST_DATABASE_URL must point at a database named *_test")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for role in (APP_ROLE, LOADER_ROLE, MISSING_ROLE):
            _drop_role(conn, role)
        for role in (APP_ROLE, LOADER_ROLE):
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'")  # type: ignore[call-overload]
    install_schema(DSN, APP_ROLE, loader_role=LOADER_ROLE)
    try:
        yield {"owner": DSN, "loader": _role_dsn(LOADER_ROLE), "app": _role_dsn(APP_ROLE)}
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for role in (APP_ROLE, LOADER_ROLE, MISSING_ROLE):
                _drop_role(conn, role)


def _assert_no_secret_in(text: object, dsn: str) -> None:
    """Fail with a FIXED message: a plain `assert dsn not in text` would print both on failure."""
    parts = psycopg.conninfo.conninfo_to_dict(dsn)
    secrets = [v for k, v in parts.items() if k == "password" and v] + [PASSWORD]
    if any(str(secret) in str(text) for secret in secrets) or dsn in str(text):
        raise AssertionError("a connection secret appeared in the message")


@pytest.fixture
def owner(roles: dict[str, str]) -> str:
    return roles["owner"]


@pytest.fixture
def loader(roles: dict[str, str]) -> str:
    return roles["loader"]


@pytest.fixture(params=["owner", "loader"])
def writer(request: pytest.FixtureRequest, roles: dict[str, str]) -> str:
    """The DSN of each role that may insert: the same rules bind both."""
    return roles[request.param]


def _fresh_id() -> str:
    return f"t{uuid.uuid4().hex[:12]}"


def _kl_today(dsn: str) -> date:
    row = _run(dsn, "SELECT (clock_timestamp() AT TIME ZONE 'Asia/Kuala_Lumpur')::date")
    value = row[0][0]
    assert isinstance(value, date)
    return value


def _doc(did: str, version: int = 1, **over: Any) -> dict[str, Any]:
    today = date.today()
    row: dict[str, Any] = {
        "document_id": did,
        "version": version,
        "title": "Synthetic test document",
        "origin": "dive_professional",
        "author_role": "dive_professional",
        "approver_role": "dive_professional",
        "effective_date": today - timedelta(days=2),
        "review_by_date": today + timedelta(days=90),
        "licence": "Synthetic test licence",
        "licence_reference_present": True,
        "attribution": None,
        "site_id": None,
        "all_sites": True,
        "display_permitted": True,
        "supersedes": None,
        "manifest_sha256": "a" * 64,
    }
    row.update(over)
    return row


def _insert(dsn: str, table: str, row: dict[str, Any], **kw: Any) -> list[tuple[Any, ...]]:
    columns = ", ".join(row)
    marks = ", ".join(f"%({c})s" for c in row)
    return _run(dsn, f"INSERT INTO public.{table} ({columns}) VALUES ({marks})", row, **kw)


def _add_doc(dsn: str, did: str, version: int = 1, **over: Any) -> None:
    _insert(dsn, "kb_documents", _doc(did, version, **over))


def _chunk(did: str, text: str = "Synthetic chunk text.", version: int = 1) -> dict[str, Any]:
    return {
        "document_id": did,
        "version": version,
        "chunk": 1,
        "section": "Synthetic section",
        "chunk_text": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def _event(
    did: str, status: str, version: int = 1, hash_checked: bool | None = None
) -> dict[str, Any]:
    return {
        "document_id": did,
        "version": version,
        "status": status,
        "hash_checked": status == "APPROVED" if hash_checked is None else hash_checked,
    }


def _add_event(dsn: str, did: str, status: str, version: int = 1, **kw: Any) -> None:
    _insert(dsn, "kb_status_events", _event(did, status, version, **kw))


def _ensure_chunk(dsn: str, did: str, version: int = 1) -> None:
    """A version cannot be approved without a chunk, and chunks freeze at the first status event,
    so the chunk goes in before the DRAFT event."""
    found = _run(
        dsn,
        "SELECT count(*) FROM public.kb_chunks WHERE document_id = %s AND version = %s",
        (did, version),
    )
    if found == [(0,)]:
        _insert(dsn, "kb_chunks", _chunk(did, version=version))


def _drive(dsn: str, did: str, path: tuple[str, ...], version: int = 1) -> None:
    if "APPROVED" in path and path[0] == "DRAFT":
        _ensure_chunk(dsn, did, version)
    for status in path:
        _add_event(dsn, did, status, version)


def _events(did: str) -> list[tuple[Any, ...]]:
    assert DSN is not None
    return _run(
        DSN,
        "SELECT event_order, status, hash_checked, recorded_at FROM public.kb_status_events "
        "WHERE document_id = %s ORDER BY event_order",
        (did,),
    )


# --- append-only ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "update"),
    [
        ("kb_documents", "SET title = 'changed'"),
        ("kb_chunks", "SET section = 'changed'"),
        ("kb_status_events", "SET hash_checked = false"),
    ],
)
def test_update_and_delete_are_refused_for_the_owner(owner: str, table: str, update: str) -> None:
    did = _fresh_id()
    _add_doc(owner, did)
    _insert(owner, "kb_chunks", _chunk(did))
    _add_event(owner, did, "DRAFT")
    for statement in (f"UPDATE public.{table} {update}", f"DELETE FROM public.{table}"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            _run(owner, f"{statement} WHERE document_id = %s", (did,))
    assert _run(owner, f"SELECT count(*) FROM public.{table} WHERE document_id = %s", (did,)) == [
        (1,)
    ]


@pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
def test_the_loader_has_no_update_or_delete_privilege(loader: str, table: str) -> None:
    for statement in (
        f"UPDATE public.{table} SET document_id = document_id",
        f"DELETE FROM public.{table}",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _run(loader, statement)


def test_truncate_is_refused_on_every_table_for_the_owner(owner: str) -> None:
    did = _fresh_id()
    _add_doc(owner, did)
    _insert(owner, "kb_chunks", _chunk(did))
    _add_event(owner, did, "DRAFT")
    for table in ("kb_chunks", "kb_status_events"):
        with pytest.raises(psycopg.errors.RaiseException, match="never truncated"):
            _run(owner, f"TRUNCATE public.{table}")
    # kb_documents is referenced by foreign keys, so it can only be named with its referrers; the
    # trigger must still stop that.
    with pytest.raises(psycopg.errors.RaiseException, match="never truncated"):
        _run(owner, "TRUNCATE public.kb_documents, public.kb_chunks, public.kb_status_events")
    with pytest.raises(psycopg.Error):
        _run(owner, "TRUNCATE public.kb_documents")
    for table in ("kb_documents", "kb_chunks", "kb_status_events"):
        found = _run(owner, f"SELECT count(*) FROM public.{table} WHERE document_id = %s", (did,))
        assert found == [(1,)]


@pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
def test_the_loader_cannot_truncate(loader: str, table: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run(loader, f"TRUNCATE public.{table}")


def test_the_loader_cannot_disable_a_trigger_or_skip_triggers(loader: str) -> None:
    for trigger, table in zip(
        KNOWLEDGE_TRIGGERS[:3], ("kb_documents", "kb_chunks", "kb_status_events"), strict=True
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _run(loader, f"ALTER TABLE public.{table} DISABLE TRIGGER {trigger}")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run(loader, "SET session_replication_role = replica")


def test_update_delete_and_truncate_still_fire_in_a_replica_session(owner: str) -> None:
    """ENABLE ALWAYS covers the append-only and no-truncate guards too, not only the insert one."""
    did = _fresh_id()
    _add_doc(owner, did)
    _insert(owner, "kb_chunks", _chunk(did))
    _add_event(owner, did, "DRAFT")
    updates = {
        "kb_documents": "SET title = 'changed'",
        "kb_chunks": "SET section = 'changed'",
        "kb_status_events": "SET hash_checked = true",
    }
    for table in KNOWLEDGE_TABLES:
        statements = (
            (f"UPDATE public.{table} {updates[table]} WHERE document_id = %s", "append-only"),
            (f"DELETE FROM public.{table} WHERE document_id = %s", "append-only"),
            # all three together: a referenced table alone fails on the foreign key first
            (
                "TRUNCATE public.kb_documents, public.kb_chunks, public.kb_status_events",
                "never truncated",
            ),
        )
        for statement, message in statements:
            with psycopg.connect(owner) as conn:
                try:
                    conn.execute("SET session_replication_role = replica")
                except psycopg.errors.InsufficientPrivilege:
                    pytest.skip("the owner role may not set session_replication_role here")
                with pytest.raises(psycopg.errors.RaiseException, match=message):
                    conn.execute(statement, (did,) if "%s" in statement else None)  # type: ignore[call-overload]
    count = _run(
        owner, "SELECT count(*) FROM public.kb_status_events WHERE document_id = %s", (did,)
    )
    assert count == [(1,)]


def test_the_guard_still_fires_in_a_replica_session(owner: str) -> None:
    """ENABLE ALWAYS: even session_replication_role = replica does not turn the guard off."""
    did = _fresh_id()
    _add_doc(owner, did)
    with psycopg.connect(owner) as conn:
        try:
            conn.execute("SET session_replication_role = replica")
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("the owner role may not set session_replication_role here")
        with pytest.raises(psycopg.errors.RaiseException, match="not allowed"):
            conn.execute(
                "INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) "
                "VALUES (%s, 1, 'APPROVED', true)",
                (did,),
            )


# --- the ordering key and the clock belong to the database ---------------------------------------


def test_a_supplied_event_order_and_recorded_at_never_survive(owner: str) -> None:
    did = _fresh_id()
    _add_doc(owner, did)
    forged = date(2000, 1, 1)
    row = {**_event(did, "DRAFT"), "event_order": 1, "recorded_at": forged}
    try:
        _insert(owner, "kb_status_events", row)
    except psycopg.Error:
        assert _events(did) == []
        return
    ((order, _status, _hash, stamp),) = _events(did)
    assert order != 1
    assert stamp.year != 2000


def test_overriding_system_value_cannot_choose_the_order(owner: str) -> None:
    did = _fresh_id()
    _add_doc(owner, did)
    try:
        _run(
            owner,
            "INSERT INTO public.kb_status_events "
            "(event_order, document_id, version, status, hash_checked, recorded_at) "
            "OVERRIDING SYSTEM VALUE VALUES (-5, %s, 1, 'DRAFT', false, '2000-01-01')",
            (did,),
        )
    except psycopg.Error:
        assert _events(did) == []
        return
    ((order, _status, _hash, stamp),) = _events(did)
    assert order != -5
    assert stamp.year != 2000


@pytest.mark.parametrize("column", ["event_order", "recorded_at"])
def test_the_loader_cannot_insert_the_order_or_the_clock(loader: str, column: str) -> None:
    did = _fresh_id()
    _add_doc(loader, did)
    value = "1" if column == "event_order" else "'2000-01-01'"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run(
            loader,
            f"INSERT INTO public.kb_status_events (document_id, version, status, hash_checked, "
            f"{column}) VALUES (%s, 1, 'DRAFT', false, {value})",
            (did,),
        )
    assert _events(did) == []


def test_event_order_is_assigned_in_increasing_order(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, ("DRAFT", "APPROVED", "WITHDRAWN"))
    events = _events(did)
    assert [e[1] for e in events] == ["DRAFT", "APPROVED", "WITHDRAWN"]
    orders = [e[0] for e in events]
    assert orders == sorted(set(orders))
    stamps = [e[3] for e in events]
    assert stamps == sorted(stamps)


# --- the status machine --------------------------------------------------------------------------

ALLOWED = [
    ((), "DRAFT"),
    (("DRAFT",), "APPROVED"),
    (("DRAFT",), "WITHDRAWN"),
    (("DRAFT", "APPROVED"), "WITHDRAWN"),
]
FORBIDDEN = [
    ((), "APPROVED"),
    ((), "WITHDRAWN"),
    (("DRAFT",), "DRAFT"),
    (("DRAFT", "APPROVED"), "DRAFT"),
    (("DRAFT", "APPROVED"), "APPROVED"),
    (("DRAFT", "WITHDRAWN"), "DRAFT"),
    (("DRAFT", "WITHDRAWN"), "APPROVED"),
    (("DRAFT", "APPROVED", "WITHDRAWN"), "DRAFT"),
    (("DRAFT", "APPROVED", "WITHDRAWN"), "APPROVED"),
]


@pytest.mark.parametrize(("history", "new"), ALLOWED)
def test_allowed_status_changes(writer: str, history: tuple[str, ...], new: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    if "APPROVED" in (*history, new):
        _ensure_chunk(writer, did)  # chunks go in before the first event
    _drive(writer, did, history)
    _add_event(writer, did, new)
    assert [e[1] for e in _events(did)] == [*history, new]


@pytest.mark.parametrize(("history", "new"), FORBIDDEN)
def test_forbidden_status_changes_are_refused_without_the_withdrawn_code(
    writer: str, history: tuple[str, ...], new: str
) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, history)
    with pytest.raises(psycopg.errors.RaiseException, match="not allowed") as caught:
        _add_event(writer, did, new)
    assert caught.value.sqlstate == "P0001"
    assert [e[1] for e in _events(did)] == list(history)


@pytest.mark.parametrize("history", [("DRAFT", "WITHDRAWN"), ("DRAFT", "APPROVED", "WITHDRAWN")])
def test_withdrawing_twice_uses_the_distinct_sqlstate(
    writer: str, history: tuple[str, ...]
) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, history)
    with pytest.raises(psycopg.Error, match="already withdrawn") as caught:
        _add_event(writer, did, "WITHDRAWN")
    assert caught.value.sqlstate == "KB001"
    assert [e[1] for e in _events(did)] == list(history)


def test_no_other_refusal_uses_the_withdrawn_sqlstate(owner: str) -> None:
    """Every other trigger refusal is an ordinary RAISE EXCEPTION (P0001)."""
    did = _fresh_id()
    other = _fresh_id()
    _add_doc(owner, did, site_id="site-a", all_sites=False)
    _add_doc(owner, did, 2, site_id="site-a", all_sites=False)
    attempts: list[tuple[str, str, dict[str, Any]]] = [
        ("kb_documents", "greater than", _doc(did, 2, site_id="site-a", all_sites=False)),
        ("kb_documents", "same site", _doc(did, 3)),
        (
            "kb_documents",
            "supersedes",
            _doc(did, 3, site_id="site-a", all_sites=False, supersedes=2 + 1),
        ),
        ("kb_chunks", "SHA-256", {**_chunk(did), "content_sha256": "0" * 64}),
        ("kb_status_events", "existing document", _event(other, "DRAFT")),
        ("kb_status_events", "not allowed", _event(did, "APPROVED")),
    ]
    for table, message, row in attempts:
        with pytest.raises(psycopg.errors.RaiseException, match=message) as caught:
            _insert(owner, table, row)
        assert caught.value.sqlstate == "P0001"
    _insert(owner, "kb_chunks", _chunk(did))  # a row must exist for a row trigger to fire
    _add_event(owner, did, "DRAFT")
    for table in KNOWLEDGE_TABLES:
        with pytest.raises(psycopg.errors.RaiseException) as caught:
            _run(owner, f"DELETE FROM public.{table} WHERE document_id = %s", (did,))
        assert caught.value.sqlstate == "P0001"


def test_a_status_event_needs_an_existing_version(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    with pytest.raises(psycopg.errors.RaiseException, match="existing document version"):
        _add_event(writer, did, "DRAFT", version=2)


# --- hash_checked and the approval date bounds ---------------------------------------------------


@pytest.mark.parametrize(("history", "status"), [((), "DRAFT"), (("DRAFT",), "WITHDRAWN")])
def test_hash_checked_true_is_refused_unless_approved(
    writer: str, history: tuple[str, ...], status: str
) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, history)
    with pytest.raises(psycopg.errors.CheckViolation):
        _add_event(writer, did, status, hash_checked=True)
    assert [e[1] for e in _events(did)] == list(history)


def test_a_withdrawal_records_hash_checked_false(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, ("DRAFT", "APPROVED"))
    _add_event(writer, did, "WITHDRAWN", hash_checked=False)
    assert [(e[1], e[2]) for e in _events(did)] == [
        ("DRAFT", False),
        ("APPROVED", True),
        ("WITHDRAWN", False),
    ]


def test_the_effective_date_may_be_at_most_twelve_months_after_approval(writer: str) -> None:
    today = _kl_today(writer)
    limit = _run(writer, "SELECT (%s::date + interval '12 months')::date", (today,))[0][0]
    ok, too_late = _fresh_id(), _fresh_id()
    for did, effective in ((ok, limit), (too_late, limit + timedelta(days=1))):
        _add_doc(
            writer,
            did,
            effective_date=effective,
            review_by_date=effective + timedelta(days=30),
        )
        _ensure_chunk(writer, did)
        _add_event(writer, did, "DRAFT")
    _add_event(writer, ok, "APPROVED")
    with pytest.raises(psycopg.errors.RaiseException, match="12 months after approval"):
        _add_event(writer, too_late, "APPROVED")
    assert [e[1] for e in _events(too_late)] == ["DRAFT"]


def test_the_review_by_date_may_not_be_before_approval(writer: str) -> None:
    today = _kl_today(writer)
    on_day, before = _fresh_id(), _fresh_id()
    for did, review_by in ((on_day, today), (before, today - timedelta(days=1))):
        _add_doc(writer, did, effective_date=today - timedelta(days=30), review_by_date=review_by)
        _ensure_chunk(writer, did)
        _add_event(writer, did, "DRAFT")
    _add_event(writer, on_day, "APPROVED")
    with pytest.raises(psycopg.errors.RaiseException, match="review-by date is before"):
        _add_event(writer, before, "APPROVED")
    assert [e[1] for e in _events(before)] == ["DRAFT"]


def test_the_bounds_apply_to_approval_only(writer: str) -> None:
    """A draft or a withdrawal of a document with out-of-range dates is not refused for them."""
    today = _kl_today(writer)
    did = _fresh_id()
    far = today + timedelta(days=800)
    _add_doc(writer, did, effective_date=far, review_by_date=far + timedelta(days=1))
    _drive(writer, did, ("DRAFT", "WITHDRAWN"))
    assert [e[1] for e in _events(did)] == ["DRAFT", "WITHDRAWN"]


# --- documents and chunks ------------------------------------------------------------------------


def test_a_new_version_must_be_greater_than_every_existing_one(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did, 3)
    for version in (3, 2, 1):
        with pytest.raises(psycopg.errors.RaiseException, match="greater than every existing"):
            _add_doc(writer, did, version)
    _add_doc(writer, did, 7)  # gaps are allowed; only the order matters
    assert _run(
        writer, "SELECT max(version) FROM public.kb_documents WHERE document_id = %s", (did,)
    ) == [(7,)]


@pytest.mark.parametrize(
    "other",
    [
        {"site_id": "site-b", "all_sites": False},
        {"site_id": None, "all_sites": True},
    ],
)
def test_every_version_keeps_the_same_site(writer: str, other: dict[str, Any]) -> None:
    did = _fresh_id()
    _add_doc(writer, did, site_id="site-a", all_sites=False)
    with pytest.raises(psycopg.errors.RaiseException, match="same site"):
        _add_doc(writer, did, 2, **other)
    _add_doc(writer, did, 2, site_id="site-a", all_sites=False)


def test_an_all_sites_family_cannot_gain_a_site(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    with pytest.raises(psycopg.errors.RaiseException, match="same site"):
        _add_doc(writer, did, 2, site_id="site-a", all_sites=False)


def test_supersedes_must_name_an_existing_version_of_the_same_document(writer: str) -> None:
    did, other = _fresh_id(), _fresh_id()
    _add_doc(writer, did)
    _add_doc(writer, other)
    for target in (2, 5):
        with pytest.raises(psycopg.errors.RaiseException, match="supersedes must name"):
            _add_doc(writer, did, 3, supersedes=target)
    _add_doc(writer, did, 2, supersedes=1)
    assert _run(
        writer,
        "SELECT supersedes FROM public.kb_documents WHERE document_id = %s AND version = 2",
        (did,),
    ) == [(1,)]


@pytest.mark.parametrize(
    "bad",
    [
        {"display_permitted": False},
        {"licence_reference_present": False},
        {"approver_role": "author"},
        {"all_sites": False},
        {"manifest_sha256": "A" * 64},
    ],
)
def test_document_constraints_hold_for_every_writer(writer: str, bad: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _add_doc(writer, _fresh_id(), **bad)


def test_a_chunk_whose_text_does_not_match_its_hash_is_refused(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    bad = {**_chunk(did), "content_sha256": hashlib.sha256(b"something else").hexdigest()}
    with pytest.raises(psycopg.errors.RaiseException, match="does not match its SHA-256"):
        _insert(writer, "kb_chunks", bad)
    _insert(writer, "kb_chunks", _chunk(did, "Récif à 20 m, résumé."))  # hashed as UTF-8
    assert _run(writer, "SELECT count(*) FROM public.kb_chunks WHERE document_id = %s", (did,)) == [
        (1,)
    ]


def test_a_chunk_needs_its_document(writer: str) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert(writer, "kb_chunks", _chunk(_fresh_id()))


# --- the insert triggers need READ COMMITTED -----------------------------------------------------


@pytest.mark.parametrize(
    "level", [psycopg.IsolationLevel.REPEATABLE_READ, psycopg.IsolationLevel.SERIALIZABLE]
)
@pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
def test_inserts_outside_read_committed_are_refused(writer: str, table: str, level: Any) -> None:
    did = _fresh_id()
    _add_doc(writer, did)  # the parent for the chunk and the event
    row = {
        "kb_documents": _doc(_fresh_id()),
        "kb_chunks": _chunk(did),
        "kb_status_events": _event(did, "DRAFT"),
    }[table]
    with pytest.raises(psycopg.errors.RaiseException, match="READ COMMITTED"):
        _insert(writer, table, row, isolation=level)
    key = row["document_id"]
    assert _run(writer, f"SELECT count(*) FROM public.{table} WHERE document_id = %s", (key,)) == [
        (1 if table == "kb_documents" and key == did else 0,)
    ]


def test_an_explicit_read_committed_transaction_is_accepted(writer: str) -> None:
    did = _fresh_id()
    with psycopg.connect(writer) as conn:
        conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        row = _doc(did)
        conn.execute(
            "INSERT INTO public.kb_documents ("
            + ", ".join(row)
            + ") VALUES ("  # type: ignore[arg-type]
            + ", ".join(f"%({c})s" for c in row)
            + ")",
            row,
        )
    count = _run(writer, "SELECT count(*) FROM public.kb_documents WHERE document_id = %s", (did,))
    assert count == [(1,)]


# --- the privileges of each role -----------------------------------------------------------------


def _has(owner: str, query: str, *params: Any) -> bool:
    return bool(_run(owner, query, params)[0][0])


@pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
def test_the_loader_holds_select_and_insert_only(owner: str, table: str) -> None:
    sql = "SELECT has_table_privilege(%s, %s, %s)"
    assert _has(owner, sql, LOADER_ROLE, f"public.{table}", "SELECT")
    for privilege in ("UPDATE", "DELETE", "TRUNCATE", "TRIGGER", "REFERENCES"):
        assert not _has(owner, sql, LOADER_ROLE, f"public.{table}", privilege), privilege
    table_insert = _has(owner, sql, LOADER_ROLE, f"public.{table}", "INSERT")
    assert table_insert is (table != "kb_status_events")


def test_the_loader_may_insert_only_four_event_columns(owner: str) -> None:
    sql = "SELECT has_column_privilege(%s, 'public.kb_status_events', %s, %s)"
    for column in EVENT_COLUMNS:
        granted = column in LOADER_EVENT_COLUMNS
        assert _has(owner, sql, LOADER_ROLE, column, "INSERT") is granted, column
        assert _has(owner, sql, LOADER_ROLE, column, "SELECT"), column
        for privilege in ("UPDATE", "REFERENCES"):
            assert not _has(owner, sql, LOADER_ROLE, column, privilege), (column, privilege)


def test_the_loader_has_no_sequence_temp_or_function_privilege(owner: str) -> None:
    for privilege in ("USAGE", "SELECT", "UPDATE"):
        assert not _has(
            owner, "SELECT has_sequence_privilege(%s, %s, %s)", LOADER_ROLE, SEQUENCE, privilege
        )
    assert not _has(
        owner, "SELECT has_database_privilege(%s, current_database(), 'TEMPORARY')", LOADER_ROLE
    )
    for function in FUNCTIONS:
        assert not _has(
            owner,
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            LOADER_ROLE,
            f"public.{function}",
        ), function


def test_the_loader_cannot_use_the_sequence_a_function_or_a_temp_table(loader: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run(loader, f"SELECT nextval('{SEQUENCE}')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run(loader, "SELECT public.kb_documents_insert()")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run(loader, "CREATE TEMP TABLE scratch (x int)")


@pytest.mark.parametrize("table", KNOWLEDGE_TABLES)
def test_the_application_role_may_only_read(owner: str, table: str) -> None:
    sql = "SELECT has_table_privilege(%s, %s, %s)"
    assert _has(owner, sql, APP_ROLE, f"public.{table}", "SELECT")
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER", "REFERENCES"):
        assert not _has(owner, sql, APP_ROLE, f"public.{table}", privilege), privilege
    for column in EVENT_COLUMNS if table == "kb_status_events" else ():
        assert not _has(
            owner,
            "SELECT has_column_privilege(%s, 'public.kb_status_events', %s, 'INSERT')",
            APP_ROLE,
            column,
        )


def test_the_application_role_has_no_sequence_or_function_privilege_and_cannot_write(
    owner: str, roles: dict[str, str]
) -> None:
    for privilege in ("USAGE", "SELECT", "UPDATE"):
        assert not _has(
            owner, "SELECT has_sequence_privilege(%s, %s, %s)", APP_ROLE, SEQUENCE, privilege
        )
    for function in FUNCTIONS:
        assert not _has(
            owner,
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            APP_ROLE,
            f"public.{function}",
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _add_doc(roles["app"], _fresh_id())
    assert _run(roles["app"], "SELECT count(*) FROM public.kb_documents")[0][0] >= 0


def test_the_loader_and_the_application_role_can_read_what_the_loader_wrote(
    loader: str, roles: dict[str, str]
) -> None:
    did = _fresh_id()
    _add_doc(loader, did)
    _insert(loader, "kb_chunks", _chunk(did))
    _add_event(loader, did, "DRAFT")
    for dsn in (loader, roles["app"]):
        for table in KNOWLEDGE_TABLES:
            found = _run(dsn, f"SELECT count(*) FROM public.{table} WHERE document_id = %s", (did,))
            assert found == [(1,)]


# --- triggers and functions as installed ---------------------------------------------------------


def test_each_knowledge_trigger_is_always_enabled_and_calls_its_own_function(owner: str) -> None:
    rows = _run(
        owner,
        "SELECT t.tgname, t.tgenabled, p.proname, c.relname, p.prosecdef, p.provolatile, "
        "p.proconfig FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND c.relname = ANY(%s)",
        (list(KNOWLEDGE_TABLES),),
    )
    assert {r[0] for r in rows} == set(KNOWLEDGE_TRIGGERS)
    for name, enabled, function, table, definer, volatility, config in rows:
        assert enabled == "A", name
        assert function == KNOWLEDGE_FUNCTIONS[name], name
        assert name.startswith(table), name
        assert definer is True and volatility == "v", name
        assert config == ["search_path=pg_catalog, pg_temp"], name


def test_the_triggers_have_the_intended_timing_and_level(owner: str) -> None:
    rows = _run(
        owner,
        "SELECT tgname, tgtype FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT tgisinternal AND relname = ANY(%s)",
        (list(KNOWLEDGE_TABLES),),
    )
    row_level, before, insert, delete, update, truncate = 1, 2, 4, 8, 16, 32
    for name, tgtype in rows:
        assert tgtype & before, name
        if name.endswith("insert_guard"):
            assert (
                tgtype & row_level and tgtype & insert and not tgtype & (update | delete | truncate)
            )
        elif name.endswith("append_only_guard"):
            assert (
                tgtype & row_level and tgtype & update and tgtype & delete and not tgtype & insert
            )
        else:
            assert not tgtype & row_level and tgtype & truncate, name


def test_the_knowledge_functions_are_owned_by_the_schema_owner_and_not_public(owner: str) -> None:
    rows = _run(
        owner,
        "SELECT p.proname, pg_get_userbyid(p.proowner), p.proacl::text "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = ANY(%s)",
        (sorted(set(KNOWLEDGE_FUNCTIONS.values())),),
    )
    assert {r[0] for r in rows} == set(KNOWLEDGE_FUNCTIONS.values())
    for _name, function_owner, acl in rows:
        assert function_owner == _run(owner, "SELECT current_user")[0][0]
        assert acl is None or "=X" not in acl.replace(f"{function_owner}=X", "")


# --- install_schema ------------------------------------------------------------------------------


def _acls(owner: str) -> list[tuple[Any, ...]]:
    return _run(
        owner,
        "SELECT c.relname, c.relacl::text, "
        "(SELECT string_agg(a.attname || ':' || a.attacl::text, ',' ORDER BY a.attname) "
        " FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attacl IS NOT NULL) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND (c.relname LIKE 'kb\\_%%' OR c.relname = 'assessments') "
        "ORDER BY c.relname",
    )


def test_install_schema_twice_changes_nothing(owner: str, roles: dict[str, str]) -> None:
    before = _acls(owner)
    assert migrate(owner, app_role=APP_ROLE) == []
    install_schema(owner, APP_ROLE, loader_role=LOADER_ROLE)
    install_schema(owner, APP_ROLE, loader_role=LOADER_ROLE)
    assert _acls(owner) == before
    versions = _run(owner, "SELECT version FROM public.schema_migrations ORDER BY version")
    assert [v[0] for v in versions][: len(MIGRATIONS)] == [m.version for m in MIGRATIONS]


def test_a_missing_loader_role_stops_install_schema_with_the_fixed_message(owner: str) -> None:
    before = _acls(owner)
    with pytest.raises(LoaderRoleError) as caught:
        install_schema(owner, APP_ROLE, loader_role=MISSING_ROLE)
    assert str(caught.value) == LOADER_ROLE_MISSING
    assert MISSING_ROLE not in str(caught.value)
    # The schema was migrated first (it is committed), but no grant was applied or changed.
    assert _acls(owner) == before
    assert not _run(owner, "SELECT 1 FROM pg_roles WHERE rolname = %s", (MISSING_ROLE,))


def test_db_init_exits_with_the_fixed_message_when_the_loader_role_is_missing(
    owner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.services import db_init

    monkeypatch.setenv("DIVESAFE_ADMIN_DATABASE_URL", owner)
    monkeypatch.setenv("DIVESAFE_APP_DB_ROLE", APP_ROLE)
    monkeypatch.setenv("DIVESAFE_LOADER_DB_ROLE", MISSING_ROLE)
    before = _acls(owner)
    with pytest.raises(SystemExit) as caught:
        db_init.main()
    assert str(caught.value) == LOADER_ROLE_MISSING
    _assert_no_secret_in(caught.value, owner)
    assert _acls(owner) == before


@pytest.mark.parametrize(
    "name", [APP_ROLE, "divesafe_owner", "divesafe_bootstrap", "public", "pg_monitor"]
)
def test_install_schema_refuses_unacceptable_loader_names_before_changing_anything(
    owner: str, name: str
) -> None:
    before = _acls(owner)
    with pytest.raises(ValueError, match="invalid"):
        install_schema(owner, APP_ROLE, loader_role=name)
    assert _acls(owner) == before


def test_install_schema_refuses_the_owner_as_the_loader(owner: str) -> None:
    current = _run(owner, "SELECT current_user")[0][0]
    before = _acls(owner)
    with pytest.raises((LoaderRoleError, ValueError)):
        install_schema(owner, APP_ROLE, loader_role=current)
    assert _acls(owner) == before


# --- owner policy: APPROVED with hash_checked=false (ADR 0022 decision 10, 2026-10-21) -----------


def test_approval_with_hash_checked_false_is_stored_and_selection_treats_it_as_unchecked(
    writer: str,
) -> None:
    """Owner policy (ADR 0022 decision 10, 2026-10-21): the database accepts APPROVED with
    hash_checked=false (the CHECK only forbids true on a non-APPROVED event); retrieval selection
    treats such a version as unchecked. Change this test only with a new owner decision."""
    did = _fresh_id()
    _add_doc(writer, did)
    _ensure_chunk(writer, did)
    _add_event(writer, did, "DRAFT")
    _add_event(writer, did, "APPROVED", hash_checked=False)
    assert [(e[1], e[2]) for e in _events(did)] == [("DRAFT", False), ("APPROVED", False)]


# --- the loader role check runs after the migration (owner decision) -----------------------------


@contextlib.contextmanager
def _scratch_db() -> Iterator[str]:
    """A throwaway database on the same server, created and dropped here."""
    assert DSN is not None
    name = f"divesafe_scratch_{uuid.uuid4().hex[:8]}_test"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')  # type: ignore[call-overload]
    base = psycopg.conninfo.conninfo_to_dict(DSN)
    base["dbname"] = name
    try:
        yield psycopg.conninfo.make_conninfo(**base)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')  # type: ignore[call-overload]


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    with _scratch_db() as dsn:
        yield dsn


def test_a_missing_loader_role_leaves_the_schema_migrated_and_applies_no_grants(
    scratch_dsn: str,
) -> None:
    with pytest.raises(LoaderRoleError) as caught:
        install_schema(scratch_dsn, loader_role=MISSING_ROLE)
    assert str(caught.value) == LOADER_ROLE_MISSING
    versions = _run(scratch_dsn, "SELECT version FROM public.schema_migrations ORDER BY version")
    assert [v[0] for v in versions] == [m.version for m in MIGRATIONS]
    for table in KNOWLEDGE_TABLES:
        assert _run(scratch_dsn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)) == [
            (True,)
        ]
    # No grant step ran, so PUBLIC still holds nothing it was not given and nothing was revoked:
    # the TEMPORARY revoke on the database is among the skipped statements.
    temp = _run(
        scratch_dsn, "SELECT has_database_privilege('public', current_database(), 'TEMPORARY')"
    )
    assert temp == [(True,)]


def test_db_init_exits_nonzero_with_the_fixed_message_after_migrating(
    scratch_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.services import db_init

    monkeypatch.setenv("DIVESAFE_ADMIN_DATABASE_URL", scratch_dsn)
    monkeypatch.setenv("DIVESAFE_LOADER_DB_ROLE", MISSING_ROLE)
    with pytest.raises(SystemExit) as caught:
        db_init.main()
    assert caught.value.code == LOADER_ROLE_MISSING  # a non-integer code exits with status 1
    _assert_no_secret_in(caught.value.code, scratch_dsn)
    versions = _run(scratch_dsn, "SELECT version FROM public.schema_migrations ORDER BY version")
    assert [v[0] for v in versions] == [m.version for m in MIGRATIONS]


# --- chunks freeze at the first status event ----------------------------------------------------

_PATHS = [
    pytest.param(("DRAFT",), id="draft"),
    pytest.param(("DRAFT", "APPROVED"), id="approved"),
    pytest.param(("DRAFT", "APPROVED", "WITHDRAWN"), id="withdrawn"),
]


@pytest.mark.parametrize("path", _PATHS)
def test_chunks_cannot_be_added_once_a_version_has_a_status_event(
    writer: str, path: tuple[str, ...]
) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, path)  # a path that reaches APPROVED gets its one chunk first
    sql = "SELECT count(*) FROM public.kb_chunks WHERE document_id = %s"
    before = _run(writer, sql, (did,))
    with pytest.raises(psycopg.errors.RaiseException, match="chunks cannot be added") as caught:
        _insert(writer, "kb_chunks", {**_chunk(did), "chunk": 9})
    assert caught.value.sqlstate == "P0001"  # plain; KB001 belongs to the withdrawn refusal only
    assert _run(writer, sql, (did,)) == before


def test_chunks_added_before_the_first_event_stay_and_later_ones_are_refused(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _insert(writer, "kb_chunks", _chunk(did))
    _add_event(writer, did, "DRAFT")
    with pytest.raises(psycopg.errors.RaiseException, match="chunks cannot be added"):
        _insert(writer, "kb_chunks", {**_chunk(did), "chunk": 2})
    count = _run(writer, "SELECT count(*) FROM public.kb_chunks WHERE document_id = %s", (did,))
    assert count == [(1,)]


def test_the_freeze_is_per_version_not_per_document(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, ("DRAFT", "APPROVED"))
    _add_doc(writer, did, 2)  # a new version has no status event yet
    _insert(writer, "kb_chunks", _chunk(did, version=2))
    with pytest.raises(psycopg.errors.RaiseException, match="chunks cannot be added"):
        _insert(writer, "kb_chunks", _chunk(did, version=1))


def _conn_insert(conn: psycopg.Connection[Any], table: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    marks = ", ".join(f"%({c})s" for c in row)
    conn.execute(f"INSERT INTO public.{table} ({columns}) VALUES ({marks})", row)  # type: ignore[call-overload]


def test_the_document_its_chunks_and_the_draft_event_succeed_in_one_transaction(
    writer: str,
) -> None:
    did = _fresh_id()
    with psycopg.connect(writer) as conn:
        _conn_insert(conn, "kb_documents", _doc(did))
        _conn_insert(conn, "kb_chunks", _chunk(did))
        _conn_insert(conn, "kb_chunks", {**_chunk(did, "Second synthetic chunk."), "chunk": 2})
        _conn_insert(conn, "kb_status_events", _event(did, "DRAFT"))
    count = _run(writer, "SELECT count(*) FROM public.kb_chunks WHERE document_id = %s", (did,))
    assert count == [(2,)]
    assert [e[1] for e in _events(did)] == ["DRAFT"]


def test_a_chunk_after_the_event_in_the_same_transaction_is_refused_and_nothing_is_kept(
    writer: str,
) -> None:
    did = _fresh_id()
    with (
        pytest.raises(psycopg.errors.RaiseException, match="chunks cannot be added"),
        psycopg.connect(writer) as conn,
    ):
        _conn_insert(conn, "kb_documents", _doc(did))
        _conn_insert(conn, "kb_status_events", _event(did, "DRAFT"))
        _conn_insert(conn, "kb_chunks", _chunk(did))
    assert _run(
        writer, "SELECT count(*) FROM public.kb_documents WHERE document_id = %s", (did,)
    ) == [(0,)]


# --- the review-by date is at most 12 months after the effective date ---------------------------


def _twelve_months_after(dsn: str, day: date) -> date:
    value = _run(dsn, "SELECT (%s::date + interval '12 months')::date", (day,))[0][0]
    assert isinstance(value, date)
    return value


def test_review_by_exactly_twelve_months_after_the_effective_date_is_accepted(writer: str) -> None:
    did = _fresh_id()
    effective = date.today() - timedelta(days=2)
    limit = _twelve_months_after(writer, effective)
    _add_doc(writer, did, effective_date=effective, review_by_date=limit)
    stored = _run(
        writer, "SELECT review_by_date FROM public.kb_documents WHERE document_id = %s", (did,)
    )
    assert stored == [(limit,)]


@pytest.mark.parametrize("extra", ["one day", "thirteen months"])
def test_review_by_more_than_twelve_months_after_the_effective_date_is_refused(
    writer: str, extra: str
) -> None:
    effective = date.today() - timedelta(days=2)
    limit = _twelve_months_after(writer, effective)
    late = (
        limit + timedelta(days=1)
        if extra == "one day"
        else _run(writer, "SELECT (%s::date + interval '13 months')::date", (effective,))[0][0]
    )
    did = _fresh_id()
    with pytest.raises(psycopg.errors.CheckViolation):
        _add_doc(writer, did, effective_date=effective, review_by_date=late)
    assert _run(
        writer, "SELECT count(*) FROM public.kb_documents WHERE document_id = %s", (did,)
    ) == [(0,)]


def test_review_by_before_the_effective_date_is_still_refused(writer: str) -> None:
    effective = date.today()
    with pytest.raises(psycopg.errors.CheckViolation):
        _add_doc(
            writer,
            _fresh_id(),
            effective_date=effective,
            review_by_date=effective - timedelta(days=1),
        )


def test_the_twelve_month_limit_from_29_february_is_28_february(writer: str) -> None:
    effective = date(2028, 2, 29)
    assert _twelve_months_after(writer, effective) == date(2029, 2, 28)
    accepted, refused = _fresh_id(), _fresh_id()
    _add_doc(writer, accepted, effective_date=effective, review_by_date=date(2029, 2, 28))
    with pytest.raises(psycopg.errors.CheckViolation):
        _add_doc(writer, refused, effective_date=effective, review_by_date=date(2029, 3, 1))
    found = _run(
        writer,
        "SELECT document_id FROM public.kb_documents WHERE document_id = ANY(%s)",
        ([accepted, refused],),
    )
    assert found == [(accepted,)]


# --- a guarded() bypass is still caught after the migration ---------------------------------------


def test_a_migration_that_disables_a_knowledge_trigger_is_refused_and_rolled_back(
    owner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = Migration(
        len(MIGRATIONS) + 1,
        "disable a knowledge trigger",
        "ALTER TABLE public.kb_chunks DISABLE TRIGGER kb_chunks_insert_guard;",
    )
    known = (*MIGRATIONS, bad)
    with pytest.raises(MigrationError, match="touches"):
        migrate(owner, known)  # the front door: guarded() refuses before any connection
    monkeypatch.setattr(migrations_module, "guarded", lambda known: None)  # the bypass
    with pytest.raises(MigrationError, match="not intact"):
        migrate(owner, known)
    state = _run(owner, "SELECT tgenabled FROM pg_trigger WHERE tgname = 'kb_chunks_insert_guard'")
    assert state == [("A",)]
    applied = _run(
        owner, "SELECT count(*) FROM public.schema_migrations WHERE version = %s", (bad.version,)
    )
    assert applied == [(0,)]


# --- concurrent sessions are serialised per document ---------------------------------------------


def _contend(
    dsn: str, first: tuple[str, dict[str, Any]], second: tuple[str, dict[str, Any]]
) -> tuple[tuple[Any, ...] | None, BaseException | None]:
    """Session A inserts `first` and holds its transaction open; session B inserts `second`.
    Returns what B was waiting on while A held it (None if B never waited), and B's error."""
    result: dict[str, Any] = {}

    def run_second() -> None:
        try:
            with psycopg.connect(dsn) as conn:
                result["pid"] = conn.info.backend_pid
                _conn_insert(conn, *second)
            result["error"] = None
        except psycopg.Error as exc:
            result["error"] = exc

    waiting: tuple[Any, ...] | None = None
    thread = threading.Thread(target=run_second)
    with psycopg.connect(dsn) as a:
        _conn_insert(a, *first)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and thread.is_alive():
            pid = result.get("pid")
            if pid is not None:
                rows = _run(
                    dsn,
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                    (pid,),
                )
                if rows and rows[0][0] == "Lock":
                    waiting = rows[0]
                    break
            time.sleep(0.05)
    thread.join(20)
    assert not thread.is_alive()
    return waiting, result.get("error")


def test_two_sessions_adding_the_same_next_version_are_serialised(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    waiting, error = _contend(
        writer, ("kb_documents", _doc(did, 2)), ("kb_documents", _doc(did, 2))
    )
    # The second waited on the document's advisory lock (not on a row lock), then saw the first.
    assert waiting == ("Lock", "advisory")
    assert isinstance(error, psycopg.errors.RaiseException)
    assert "greater than" in str(error)
    rows = _run(writer, "SELECT count(*) FROM public.kb_documents WHERE document_id = %s", (did,))
    assert rows == [(2,)]  # versions 1 and 2, exactly one version 2


def test_two_sessions_adding_first_versions_at_different_sites_are_serialised(writer: str) -> None:
    did = _fresh_id()
    waiting, error = _contend(
        writer,
        ("kb_documents", _doc(did, 1, site_id="site-a", all_sites=False)),
        ("kb_documents", _doc(did, 2, site_id="site-b", all_sites=False)),
    )
    assert waiting == ("Lock", "advisory")
    assert isinstance(error, psycopg.errors.RaiseException)
    assert "same site" in str(error)
    sites = _run(writer, "SELECT site_id FROM public.kb_documents WHERE document_id = %s", (did,))
    assert sites == [("site-a",)]


_LOCK_CALL = (
    "PERFORM pg_catalog.pg_advisory_xact_lock(1220001, pg_catalog.hashtext(NEW.document_id));"
)


def _allow_the_weakened_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0022 condition 9 correctly makes migrate() refuse a migration 4 whose functions differ
    from the pinned hashes. These mutation tests apply a deliberately weakened COPY, so for that one
    test the knowledge check is replaced by one that finds nothing. Their own assertions are
    unchanged; the unpatched refusal is proved by
    `test_the_weakened_lock_copy_is_refused_by_the_pin_check`."""
    monkeypatch.setattr(migrations_module, "knowledge_problems", lambda *a, **k: [])


def _weakened_lock_copy(function_only: bool) -> Migration:
    original = MIGRATIONS[3]
    if not function_only:
        return Migration(4, original.name, original.sql.replace(_LOCK_CALL, ""))
    start = original.sql.index("FUNCTION public.kb_chunks_insert()")
    end = original.sql.index("$$;", start)
    body = original.sql[start:end].replace(_LOCK_CALL, "")
    return Migration(4, original.name, original.sql[:start] + body + original.sql[end:])


@pytest.mark.parametrize("function_only", [False, True])
def test_the_weakened_lock_copy_is_refused_by_the_pin_check(
    monkeypatch: pytest.MonkeyPatch, function_only: bool
) -> None:
    """Condition 9 has teeth: WITHOUT the patch above, migrate() rejects the copy and rolls back."""
    weakened = _weakened_lock_copy(function_only)
    assert weakened.sql != MIGRATIONS[3].sql
    monkeypatch.setattr(migrations_module, "guarded", lambda known: None)
    with _scratch_db() as dsn:
        with pytest.raises(MigrationError) as caught:
            migrate(dsn, (*MIGRATIONS[:3], weakened))
        message = str(caught.value)
        tables = _run(dsn, "SELECT to_regclass('public.kb_chunks')")
    assert "the knowledge guard is not intact after migrating" in message
    assert "does not match its pinned hash" in message
    assert "the definition of kb_chunks_insert does not match its pinned hash" in message
    assert tables == [(None,)]  # rolled back: no knowledge table


def test_mutation_without_the_advisory_lock_the_two_sessions_do_not_serialise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutated COPY of migration 4 (never src) on a scratch database: with the lock removed the
    site rule can be broken, and the test above is what notices (it expects the wait)."""
    original = MIGRATIONS[3]
    assert _LOCK_CALL in original.sql
    mutated = Migration(4, original.name, original.sql.replace(_LOCK_CALL, ""))
    monkeypatch.setattr(migrations_module, "guarded", lambda known: None)
    _allow_the_weakened_copy(monkeypatch)
    with _scratch_db() as dsn:
        migrate(dsn, (*MIGRATIONS[:3], mutated))
        did = _fresh_id()
        waiting, error = _contend(
            dsn,
            ("kb_documents", _doc(did, 1, site_id="site-a", all_sites=False)),
            ("kb_documents", _doc(did, 2, site_id="site-b", all_sites=False)),
        )
        assert waiting is None and error is None  # both went in: two sites for one document
        sites = _run(
            dsn,
            "SELECT site_id FROM public.kb_documents WHERE document_id = %s ORDER BY version",
            (did,),
        )
        assert sites == [("site-a",), ("site-b",)]


# --- temporary objects cannot shadow the real ones -----------------------------------------------


def test_temporary_objects_named_like_the_knowledge_objects_do_not_shadow_them(owner: str) -> None:
    did = _fresh_id()
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE kb_documents (document_id text)")
        conn.execute("CREATE TEMP TABLE kb_chunks (x int)")
        conn.execute("CREATE TEMP TABLE kb_status_events (x int)")
        conn.execute("CREATE TEMP SEQUENCE kb_status_event_order_seq")
        conn.execute("SET search_path = pg_temp, public")
        before = conn.execute("SELECT last_value FROM public.kb_status_event_order_seq").fetchone()
        _conn_insert(conn, "kb_documents", _doc(did))
        _conn_insert(conn, "kb_chunks", _chunk(did))
        # The status trigger must read the real document (not the empty temp table of that name).
        _conn_insert(conn, "kb_status_events", _event(did, "DRAFT"))
        after = conn.execute("SELECT last_value FROM public.kb_status_event_order_seq").fetchone()
        temp_seq = conn.execute(
            "SELECT is_called FROM pg_temp.kb_status_event_order_seq"
        ).fetchone()
        assert before is not None and after is not None and after[0] > before[0]
        assert temp_seq == (False,)  # the temporary sequence was never used
        assert conn.execute("SELECT count(*) FROM pg_temp.kb_documents").fetchone() == (0,)
    assert [e[1] for e in _events(did)] == ["DRAFT"]


# --- approval uses the Kuala Lumpur date whatever the session time zone --------------------------


@pytest.mark.parametrize("zone", ["+14", "-12"])
def test_approval_uses_the_kuala_lumpur_date_in_any_session_time_zone(
    loader: str, zone: str
) -> None:
    before = _kl_today(loader)
    limit = _twelve_months_after(loader, before)
    ok, late = _fresh_id(), _fresh_id()
    with psycopg.connect(loader) as conn:
        conn.execute(f"SET TimeZone = '{zone}'")  # type: ignore[call-overload]
        for did, effective in ((ok, limit), (late, limit + timedelta(days=1))):
            _conn_insert(
                conn,
                "kb_documents",
                _doc(did, effective_date=effective, review_by_date=effective + timedelta(days=30)),
            )
            _conn_insert(conn, "kb_chunks", _chunk(did))  # before the first event
            _conn_insert(conn, "kb_status_events", _event(did, "DRAFT"))
        conn.commit()
        _conn_insert(conn, "kb_status_events", _event(ok, "APPROVED"))
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="12 months after approval"):
            _conn_insert(conn, "kb_status_events", _event(late, "APPROVED"))
    after = _kl_today(loader)
    if before != after:
        pytest.skip("the run crossed midnight in Kuala Lumpur")
    stamped = _run(
        loader,
        "SELECT (recorded_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date FROM public.kb_status_events "
        "WHERE document_id = %s AND status = 'APPROVED'",
        (ok,),
    )
    assert stamped == [(before,)]


# --- install_schema without a loader role ---------------------------------------------------------


def _check_no_loader_grants(dsn: str) -> None:
    for table in KNOWLEDGE_TABLES:
        sel = _run(
            dsn, "SELECT has_table_privilege(%s, %s, 'SELECT')", (APP_ROLE, f"public.{table}")
        )
        assert sel == [(True,)], table
        for who in ("public", APP_ROLE):
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                got = _run(
                    dsn, "SELECT has_table_privilege(%s, %s, %s)", (who, f"public.{table}", priv)
                )
                assert got == [(False,)], (who, table, priv)
        assert _run(
            dsn, "SELECT has_table_privilege('public', %s, 'SELECT')", (f"public.{table}",)
        ) == [(False,)]
    temp = _run(dsn, "SELECT has_database_privilege('public', current_database(), 'TEMPORARY')")
    assert temp == [(False,)]
    for function in FUNCTIONS:
        got = _run(
            dsn, "SELECT has_function_privilege('public', %s, 'EXECUTE')", (f"public.{function}",)
        )
        assert got == [(False,)], function
    seq = _run(dsn, "SELECT has_sequence_privilege('public', %s, 'USAGE')", (SEQUENCE,))
    assert seq == [(False,)]


def test_install_schema_without_a_loader_role_still_locks_the_knowledge_objects(
    roles: dict[str, str],
) -> None:
    with _scratch_db() as dsn:
        install_schema(dsn, APP_ROLE)
        _check_no_loader_grants(dsn)


def test_mutation_kb_grants_only_with_a_loader_role_is_caught(
    roles: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = postgres_module._kb_grants

    def only_with_a_loader(conn: Any, app_role: str | None, loader_role: str | None) -> None:
        if loader_role is not None:
            real(conn, app_role, loader_role)

    monkeypatch.setattr(postgres_module, "_kb_grants", only_with_a_loader)
    with _scratch_db() as dsn:
        install_schema(dsn, APP_ROLE)
        with pytest.raises(AssertionError):
            _check_no_loader_grants(dsn)


# --- TEMPORARY that stays held is refused ---------------------------------------------------------


@pytest.mark.parametrize("inherit", ["INHERIT", "NOINHERIT"])
def test_a_loader_that_is_a_member_of_a_role_holding_temporary_is_refused(
    roles: dict[str, str], inherit: str
) -> None:
    """NOINHERIT does not help: the member can still SET ROLE to the group and use TEMP."""
    group, member = "divesafe_kb_tempgrp_test", "divesafe_kb_tempmember_test"
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as admin:
        for role in (member, group):
            _drop_role(admin, role)
        admin.execute(f"CREATE ROLE {group} NOLOGIN")  # type: ignore[call-overload]
        admin.execute(f"CREATE ROLE {member} LOGIN {inherit} PASSWORD '{PASSWORD}'")  # type: ignore[call-overload]
        admin.execute(f"GRANT {group} TO {member}")  # type: ignore[call-overload]
    try:
        with _scratch_db() as dsn:
            migrate(dsn)
            with psycopg.connect(dsn, autocommit=True) as conn:
                name = conn.execute("SELECT current_database()").fetchone()
                assert name is not None
                conn.execute(f'GRANT TEMPORARY ON DATABASE "{name[0]}" TO {group}')  # type: ignore[call-overload]
            with pytest.raises(TempPrivilegeError) as caught:
                install_schema(dsn, loader_role=member)
            assert str(caught.value) == TEMP_STILL_HELD
            assert isinstance(caught.value, LoaderRoleError)
            assert member not in str(caught.value) and group not in str(caught.value)
            with psycopg.connect(dsn, autocommit=True) as conn:
                for role in (member, group):
                    conn.execute(f"DROP OWNED BY {role}")  # type: ignore[call-overload]
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            for role in (member, group):
                _drop_role(admin, role)


# --- a version without chunks cannot be approved (owner decision 2026-10-22) ----------------------


def _sequence_state() -> tuple[Any, ...]:
    assert DSN is not None  # the loader has no privilege on the sequence; read it as the owner
    return _run(DSN, "SELECT last_value, is_called FROM public.kb_status_event_order_seq")[0]


def test_a_version_without_chunks_cannot_be_approved_and_burns_no_sequence_value(
    writer: str,
) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _add_event(writer, did, "DRAFT")
    before = _sequence_state()
    with pytest.raises(psycopg.errors.RaiseException, match="without chunks") as caught:
        _add_event(writer, did, "APPROVED")
    assert caught.value.sqlstate == "P0001"  # plain; KB001 is for the withdrawn refusal only
    assert _sequence_state() == before
    assert [e[1] for e in _events(did)] == ["DRAFT"]


def test_chunks_of_another_version_do_not_let_a_version_be_approved(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _insert(writer, "kb_chunks", _chunk(did))
    _add_doc(writer, did, 2)
    _add_event(writer, did, "DRAFT", version=2)
    with pytest.raises(psycopg.errors.RaiseException, match="without chunks"):
        _add_event(writer, did, "APPROVED", version=2)


def test_a_chunk_added_before_the_draft_event_lets_the_approval_succeed(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _insert(writer, "kb_chunks", _chunk(did))
    _drive(writer, did, ("DRAFT", "APPROVED"))
    assert [e[1] for e in _events(did)] == ["DRAFT", "APPROVED"]


def test_withdrawing_a_version_that_has_no_chunks_is_still_allowed(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    _drive(writer, did, ("DRAFT", "WITHDRAWN"))
    assert [e[1] for e in _events(did)] == ["DRAFT", "WITHDRAWN"]


def test_the_transition_check_runs_before_the_chunks_check(writer: str) -> None:
    """APPROVED as a first event is refused as a bad transition, not for missing chunks."""
    did = _fresh_id()
    _add_doc(writer, did)
    with pytest.raises(psycopg.errors.RaiseException, match="not allowed"):
        _add_event(writer, did, "APPROVED")


# --- races between a chunk and the first status event --------------------------------------------


def test_a_chunk_waits_for_a_draft_event_in_flight_and_is_then_refused(writer: str) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    waiting, error = _contend(
        writer, ("kb_status_events", _event(did, "DRAFT")), ("kb_chunks", _chunk(did))
    )
    assert waiting == ("Lock", "advisory")
    assert isinstance(error, psycopg.errors.RaiseException)
    assert "chunks cannot be added" in str(error)
    count = _run(writer, "SELECT count(*) FROM public.kb_chunks WHERE document_id = %s", (did,))
    assert count == [(0,)]


def test_a_draft_event_waits_for_a_chunk_in_flight_and_the_approval_then_succeeds(
    writer: str,
) -> None:
    did = _fresh_id()
    _add_doc(writer, did)
    waiting, error = _contend(
        writer, ("kb_chunks", _chunk(did)), ("kb_status_events", _event(did, "DRAFT"))
    )
    assert waiting == ("Lock", "advisory")
    assert error is None  # the event was applied after the chunk committed
    _add_event(writer, did, "APPROVED")
    assert [e[1] for e in _events(did)] == ["DRAFT", "APPROVED"]


def test_mutation_without_the_advisory_lock_a_chunk_can_follow_the_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a scratch database with a mutated COPY of migration 4 (no lock in the chunk trigger),
    the chunk does not wait for the in-flight DRAFT event and is stored beside it, so the
    freeze is bypassed. The test above (it expects the wait and the refusal) is what notices."""
    original = MIGRATIONS[3]
    start = original.sql.index("FUNCTION public.kb_chunks_insert()")
    end = original.sql.index("$$;", start)
    assert _LOCK_CALL in original.sql[start:end]
    mutated_sql = (
        original.sql[:start] + original.sql[start:end].replace(_LOCK_CALL, "") + original.sql[end:]
    )
    mutated = Migration(4, original.name, mutated_sql)
    monkeypatch.setattr(migrations_module, "guarded", lambda known: None)
    _allow_the_weakened_copy(monkeypatch)
    with _scratch_db() as dsn:
        migrate(dsn, (*MIGRATIONS[:3], mutated))
        did = _fresh_id()
        _add_doc(dsn, did)
        waiting, error = _contend(
            dsn, ("kb_status_events", _event(did, "DRAFT")), ("kb_chunks", _chunk(did))
        )
        assert waiting is None and error is None  # no wait, no refusal: the freeze was bypassed
        count = _run(dsn, "SELECT count(*) FROM public.kb_chunks WHERE document_id = %s", (did,))
        assert count == [(1,)]


def test_a_loader_in_a_nested_membership_chain_to_a_temporary_holder_is_refused(
    roles: dict[str, str],
) -> None:
    """loader -> middle -> group; only the group holds TEMPORARY."""
    group, middle, member = (
        "divesafe_kb_nestgrp_test",
        "divesafe_kb_nestmid_test",
        "divesafe_kb_nestmember_test",
    )
    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as admin:
        for role in (member, middle, group):
            _drop_role(admin, role)
        admin.execute(f"CREATE ROLE {group} NOLOGIN")  # type: ignore[call-overload]
        admin.execute(f"CREATE ROLE {middle} NOLOGIN NOINHERIT")  # type: ignore[call-overload]
        admin.execute(f"CREATE ROLE {member} LOGIN NOINHERIT PASSWORD '{PASSWORD}'")  # type: ignore[call-overload]
        admin.execute(f"GRANT {group} TO {middle}")  # type: ignore[call-overload]
        admin.execute(f"GRANT {middle} TO {member}")  # type: ignore[call-overload]
    try:
        with _scratch_db() as dsn:
            migrate(dsn)
            with psycopg.connect(dsn, autocommit=True) as conn:
                name = conn.execute("SELECT current_database()").fetchone()
                assert name is not None
                conn.execute(f'GRANT TEMPORARY ON DATABASE "{name[0]}" TO {group}')  # type: ignore[call-overload]
            with pytest.raises(TempPrivilegeError) as caught:
                install_schema(dsn, loader_role=member)
            assert str(caught.value) == TEMP_STILL_HELD
            for role in (member, middle, group):
                assert role not in str(caught.value)
            with psycopg.connect(dsn, autocommit=True) as conn:
                for role in (member, middle, group):
                    conn.execute(f"DROP OWNED BY {role}")  # type: ignore[call-overload]
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            for role in (member, middle, group):
                _drop_role(admin, role)
