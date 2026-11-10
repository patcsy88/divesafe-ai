"""Read side of the knowledge store (ADR 0022 decisions 2-4, 10, 12 and 14).

Two read-only queries in one REPEATABLE READ transaction, so both see one snapshot:

- the per-version summary: dates, status facts and chunk count for every version that applies
  to the site (never a title), which `rag.selection` classifies in code;
- the selection: the chunks of each family whose governing version is returned, chosen in SQL
  by the same rules, ordered by (document id, version, chunk), at most `MAX_KNOWLEDGE_ITEMS`
  rows plus the count available.

The only parameters are the plan's `site_id`, the run's `now` and the dive window end. Local dates
use `AT TIME ZONE 'Asia/Kuala_Lumpur'` written in the SQL, never the database clock or a numeric
zone. Every database error becomes a `KnowledgeStoreError` with a fixed string, never driver text.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import psycopg

from divesafe.data import ConnectorError
from divesafe.domain import MAX_KNOWLEDGE_ITEMS
from divesafe.rag.selection import STATUSES, VersionRow

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 3000
_OPTIONS = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}"
# `options` cannot pin DateStyle or client_encoding (the environment and stored settings win
# over it); a session SET wins over both, as in `services.postgres`.
_DATE_STYLE = "SET DateStyle = 'ISO, MDY'"
_CLIENT_ENCODING = "SET client_encoding = 'UTF8'"
_SESSION_SETUP = (_DATE_STYLE, _CLIENT_ENCODING)

# A chunk is at most this many characters (decision 12; the load refuses longer ones).
MAX_CHUNK_CHARACTERS = 1500

# Fixed `ConnectorError` texts (decision 14): nothing from the driver, the corpus or the request.
STORE_UNAVAILABLE = "knowledge store unavailable"
STORE_TIMED_OUT = "knowledge store did not answer in time"
STORE_MALFORMED = "knowledge store returned a malformed row"

# The per-version facts selection needs. Never a title. `hash_checked` is true only when every
# APPROVED event is checked; `approved_on` is the latest APPROVED event's local date.
_VERSIONS = """
SELECT d.document_id, d.version, d.site_id, d.all_sites, d.effective_date, d.review_by_date,
       ev.latest IS NOT NULL AS has_events,
       coalesce(ev.approved, false) AS approved,
       coalesce(ev.hash_checked, false) AS hash_checked,
       coalesce(ev.withdrawn, false) AS withdrawn,
       ev.approved_on,
       ev.latest,
       (SELECT count(*) FROM ONLY public.kb_chunks c
         WHERE c.document_id = d.document_id AND c.version = d.version) AS chunks
FROM ONLY public.kb_documents d
LEFT JOIN LATERAL (
    SELECT bool_or(e.status = 'APPROVED') AS approved,
           bool_and(e.hash_checked) FILTER (WHERE e.status = 'APPROVED') AS hash_checked,
           bool_or(e.status = 'WITHDRAWN') AS withdrawn,
           max((e.recorded_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date)
               FILTER (WHERE e.status = 'APPROVED') AS approved_on,
           (array_agg(e.status ORDER BY e.event_order DESC))[1] AS latest
    FROM ONLY public.kb_status_events e
    WHERE e.document_id = d.document_id AND e.version = d.version
) ev ON true
WHERE (d.site_id = %(site_id)s OR d.all_sites)
"""

VERSIONS_SQL = _VERSIONS + "ORDER BY d.document_id, d.version\n"

# Decision 3 in SQL: the governing version is the highest ever-approved version in force by the
# window end; its chunks are returned only when it is checked, never withdrawn, currently
# APPROVED, approved on or before the run's local date and usable to the window end. No
# fall-back: a governing version that fails returns nothing from its family.
SELECTION_SQL = f"""
WITH v AS ({_VERSIONS}),
governing AS (
    SELECT DISTINCT ON (v.document_id) v.*
    FROM v
    WHERE v.approved
      AND (v.effective_date::timestamp AT TIME ZONE 'Asia/Kuala_Lumpur') <= %(window_end)s
    ORDER BY v.document_id, v.version DESC
),
chosen AS (
    SELECT g.document_id, g.version
    FROM governing g
    WHERE g.hash_checked
      AND NOT g.withdrawn
      AND g.latest = 'APPROVED'
      AND g.approved_on <= (%(now)s::timestamptz AT TIME ZONE 'Asia/Kuala_Lumpur')::date
      AND %(window_end)s
          < ((g.review_by_date + 1)::timestamp AT TIME ZONE 'Asia/Kuala_Lumpur')
)
SELECT c.document_id, c.version, c.chunk, c.section, c.chunk_text, c.content_sha256,
       d.origin, d.licence, d.attribution, d.author_role, d.approver_role,
       d.site_id, d.all_sites, d.effective_date, d.review_by_date,
       count(*) OVER () AS available
FROM chosen
JOIN ONLY public.kb_chunks c
  ON c.document_id = chosen.document_id AND c.version = chosen.version
JOIN ONLY public.kb_documents d
  ON d.document_id = chosen.document_id AND d.version = chosen.version
ORDER BY c.document_id, c.version, c.chunk
LIMIT {MAX_KNOWLEDGE_ITEMS}
"""


class KnowledgeStoreError(ConnectorError):
    """The store could not answer. The message is always one of the fixed texts above."""


@dataclass(frozen=True)
class ChunkRow:
    """One selected chunk with what its item needs. Never a title."""

    document_id: str
    version: int
    chunk: int
    section: str
    text: str
    content_sha256: str
    origin: str
    licence: str
    attribution: str | None
    author_role: str
    approver_role: str
    site_id: str | None
    all_sites: bool
    effective_date: date
    review_by_date: date


@dataclass(frozen=True)
class Selection:
    """What the store returned: the per-version summary, the selected chunks (at most the bound
    from SQL; a fake may return more) and the count SQL found before its LIMIT."""

    versions: tuple[VersionRow, ...]
    chunks: tuple[ChunkRow, ...]
    available: int


class KnowledgeStore(Protocol):
    def select(self, site_id: str, now: datetime, window_end: datetime) -> Selection: ...


def _str(value: object, *, optional: bool = False) -> Any:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise KnowledgeStoreError(STORE_MALFORMED)
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeStoreError(STORE_MALFORMED)
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise KnowledgeStoreError(STORE_MALFORMED)
    return value


def _date(value: object, *, optional: bool = False) -> Any:
    if value is None and optional:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise KnowledgeStoreError(STORE_MALFORMED)
    return value


def version_row(row: tuple[Any, ...]) -> VersionRow:
    """A summary row, type-checked; anything unexpected is a malformed row."""
    if len(row) != 13:
        raise KnowledgeStoreError(STORE_MALFORMED)
    latest = _str(row[11], optional=True)
    if latest is not None and latest not in STATUSES:
        raise KnowledgeStoreError(STORE_MALFORMED)
    try:
        return VersionRow(
            document_id=_str(row[0]),
            version=_int(row[1]),
            site_id=_str(row[2], optional=True),
            all_sites=_bool(row[3]),
            effective_date=_date(row[4]),
            review_by_date=_date(row[5]),
            has_events=_bool(row[6]),
            approved=_bool(row[7]),
            hash_checked=_bool(row[8]),
            withdrawn=_bool(row[9]),
            approved_on=_date(row[10], optional=True),
            latest=latest,
            chunks=_int(row[12]),
        )
    except ValueError:
        raise KnowledgeStoreError(STORE_MALFORMED) from None


def chunk_row(row: tuple[Any, ...]) -> tuple[ChunkRow, int]:
    """A selection row and its `available` count, type-checked. The text must hash to its
    stored `content_sha256` (UTF-8, decision 7), or the row is malformed."""
    if len(row) != 16:
        raise KnowledgeStoreError(STORE_MALFORMED)
    chunk = ChunkRow(
        document_id=_str(row[0]),
        version=_int(row[1]),
        chunk=_int(row[2]),
        section=_str(row[3]),
        text=_str(row[4]),
        content_sha256=_str(row[5]),
        origin=_str(row[6]),
        licence=_str(row[7]),
        attribution=_str(row[8], optional=True),
        author_role=_str(row[9]),
        approver_role=_str(row[10]),
        site_id=_str(row[11], optional=True),
        all_sites=_bool(row[12]),
        effective_date=_date(row[13]),
        review_by_date=_date(row[14]),
    )
    if hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() != chunk.content_sha256:
        raise KnowledgeStoreError(STORE_MALFORMED)
    return chunk, _int(row[15])


def parameters(site_id: str, now: datetime, window_end: datetime) -> dict[str, Any]:
    """The only values that reach SQL (decision 2)."""
    return {"site_id": site_id, "now": now, "window_end": window_end}


class PostgresKnowledgeStore:
    """One short-lived read-only connection per run, with connect, statement and lock timeouts.
    Synchronous: the connector runs it in a worker thread."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        conn = psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, options=_OPTIONS)
        try:
            for statement in _SESSION_SETUP:
                conn.execute(statement)
            conn.commit()
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
        except BaseException:
            conn.close()
            raise
        return conn

    def select(self, site_id: str, now: datetime, window_end: datetime) -> Selection:
        params = parameters(site_id, now, window_end)
        try:
            conn = self._connect()
            try:
                versions = conn.execute(VERSIONS_SQL, {"site_id": site_id}).fetchall()
                rows = conn.execute(SELECTION_SQL, params).fetchall()
                conn.rollback()
            finally:
                conn.close()
        except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable):
            logger.warning("knowledge store timed out")
            raise KnowledgeStoreError(STORE_TIMED_OUT) from None
        except psycopg.Error:
            logger.warning("knowledge store failed")
            raise KnowledgeStoreError(STORE_UNAVAILABLE) from None
        summary = tuple(version_row(tuple(r)) for r in versions)
        parsed = [chunk_row(tuple(r)) for r in rows]
        available = max((n for _, n in parsed), default=0)
        return Selection(summary, tuple(c for c, _ in parsed), available)
