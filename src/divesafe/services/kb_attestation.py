"""Start-up check of the model-host attestation (ADR 0028 decision 7).

With the knowledge host set, the API refuses to start while any document version whose latest
status event is APPROVED, and that has no WITHDRAWN event, has `attests_model_host_egress = false`,
whatever its dates or site. It refuses as well if the store cannot be read or answers with a
malformed row: it never passes on a failure. One read-only query on the application's own
connection string; only a count comes back, never a document id, title or text. Every refusal is a
fixed message, never driver text.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 3000
_OPTIONS = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}"
_SEARCH_PATH = "SET search_path = pg_catalog, pg_temp"

# Latest event per version by `event_order`; no date or site condition (stricter than the in-force
# query, so a future-dated or expired unattested version counts too).
UNATTESTED_SQL = """
SELECT count(*)
FROM public.kb_documents d
JOIN (
    SELECT e.document_id, e.version,
           (array_agg(e.status ORDER BY e.event_order DESC))[1] AS latest,
           bool_or(e.status = 'WITHDRAWN') AS withdrawn
    FROM public.kb_status_events e
    GROUP BY e.document_id, e.version
) ev ON ev.document_id = d.document_id AND ev.version = d.version
WHERE ev.latest = 'APPROVED'
  AND NOT ev.withdrawn
  AND d.attests_model_host_egress IS NOT TRUE
"""

UNATTESTED_REFUSED = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set and an approved knowledge version does "
    "not attest that it may be sent to the model host; withdraw or replace it (ADR 0028)"
)
STORE_UNREADABLE = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set and the knowledge store's attestations "
    "cannot be read (ADR 0028)"
)
STORE_MALFORMED = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set and the knowledge store returned a "
    "malformed answer (ADR 0028)"
)
NO_STORE = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set but no knowledge store is configured "
    "(DIVESAFE_DATABASE_URL; ADR 0028)"
)


class AttestationRefused(RuntimeError):
    """Start-up must not continue. The message is one of the fixed texts above."""


def unattested_count(rows: list[tuple[Any, ...]]) -> int:
    """The count from the query's rows: exactly one row holding one non-negative int."""
    if len(rows) != 1 or len(rows[0]) != 1:
        raise AttestationRefused(STORE_MALFORMED)
    value = rows[0][0]
    if type(value) is not int or value < 0:
        raise AttestationRefused(STORE_MALFORMED)
    return value


def check_attestations(dsn: str) -> None:
    """Raise `AttestationRefused` unless every APPROVED, non-withdrawn version attests."""
    try:
        with psycopg.connect(
            dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, options=_OPTIONS
        ) as conn:
            conn.execute(_SEARCH_PATH)
            conn.commit()
            conn.read_only = True
            rows = [tuple(r) for r in conn.execute(UNATTESTED_SQL).fetchall()]
            conn.rollback()
    except psycopg.Error:
        logger.warning("knowledge attestation check could not read the store")
        raise AttestationRefused(STORE_UNREADABLE) from None
    if unattested_count(rows) > 0:
        raise AttestationRefused(UNATTESTED_REFUSED)
