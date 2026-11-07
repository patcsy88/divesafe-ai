"""`load` and `approve` for the knowledge loader (ADR 0022 decisions 7, 10-13; ADR 0024 A3).

    python -m divesafe.services.kb_load load [--approved-manifest-sha256 <hash>]
    python -m divesafe.services.kb_load approve <document_id> <version> \
        [--approved-manifest-sha256 <hash>]

Either may add `--development-no-manifest-hash`, which skips the hash only when
DIVESAFE_ENVIRONMENT is not production. Both run the privilege self-checks BEFORE any write and
refuse on any problem. One transaction, READ COMMITTED first, then SET LOCAL timeouts, then the
global lock (1220002), then per-document locks (1220001). Messages are fixed and echo nothing.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from divesafe.config.loader_settings import (
    LoaderConfigError,
    LoaderSettings,
    load_loader_settings,
)
from divesafe.data.sites import SITES
from divesafe.services import kb_common, kb_manifest, kb_privileges, kb_screen
from divesafe.services.kb_common import (
    ArgumentRefused,
    Deadline,
    DeadlineExceeded,
    LimitExceeded,
    LimitNotSet,
    LoaderRefusal,
    parse_document_version,
    require_limit,
    valid_manifest_sha256,
)
from divesafe.services.kb_manifest import LoadedCorpus

FLAG_HASH = "--approved-manifest-sha256"
FLAG_DEV = "--development-no-manifest-hash"


@dataclass(frozen=True)
class Outcome:
    status: int
    message: str


# ONE table: number and fixed message. Numbers 2-12 keep withdraw's meanings. Highest precedence
# first among the "failed, nothing written" rows; OUTCOME_UNKNOWN follows them; OK is last.
FAILED_ARGUMENTS = Outcome(2, "refused: arguments; nothing written")
FAILED_CONFIGURATION = Outcome(3, "refused: loader configuration; nothing written")
FAILED_CONNECTION = Outcome(4, "failed: database connection; nothing written")
FAILED_ROLE = Outcome(5, "refused: role has owner or application write privileges; nothing written")
FAILED_LOCK_BUSY = Outcome(6, "failed: lock busy: see the runbook; nothing written")
FAILED_STATEMENT_TIMEOUT = Outcome(7, "failed: statement timeout; nothing written")
FAILED_ISOLATION = Outcome(8, "refused: isolation level; nothing written")
FAILED_DEADLOCK = Outcome(9, "failed: deadlock; nothing written")
FAILED_NOT_FOUND = Outcome(10, "refused: version not found; nothing written")
FAILED_OTHER = Outcome(11, "refused: the database refused the write; nothing written")
OUTCOME_UNKNOWN = Outcome(12, "outcome unknown: check the status, then re-run if needed")
FAILED_MANIFEST_INVALID = Outcome(13, "refused: manifest or corpus not valid; nothing written")
FAILED_HASH_MISMATCH = Outcome(14, "refused: manifest hash does not match; nothing written")
FAILED_HASH_REQUIRED = Outcome(15, "refused: approved manifest hash required; nothing written")
FAILED_ZERO_CHUNKS = Outcome(16, "refused: no chunks to load; nothing written")
FAILED_LIMIT = Outcome(17, "refused: a size or count limit was exceeded; nothing written")
FAILED_DEADLINE = Outcome(18, "failed: overall deadline exceeded; nothing written")
FAILED_SELF_CHECK = Outcome(19, "refused: loader role over-privileged; nothing written")
FAILED_SELF_CHECK_INCOMPLETE = Outcome(20, "refused: self-check incomplete; nothing written")
FAILED_WRONG_STATUS = Outcome(21, "refused: version is not in DRAFT status; nothing written")
FAILED_FILE_HASH = Outcome(22, "refused: a file hash does not match; nothing written")
FAILED_CHUNK_HASH = Outcome(23, "refused: a chunk hash does not match; nothing written")
OK_LOAD = Outcome(0, "loaded as DRAFT")
OK_APPROVE = Outcome(0, "approved")

EXIT_TABLE: tuple[Outcome, ...] = (
    FAILED_ARGUMENTS,
    FAILED_CONFIGURATION,
    FAILED_CONNECTION,
    FAILED_ROLE,
    FAILED_LOCK_BUSY,
    FAILED_STATEMENT_TIMEOUT,
    FAILED_ISOLATION,
    FAILED_DEADLOCK,
    FAILED_NOT_FOUND,
    FAILED_OTHER,
    OUTCOME_UNKNOWN,
    FAILED_MANIFEST_INVALID,
    FAILED_HASH_MISMATCH,
    FAILED_HASH_REQUIRED,
    FAILED_ZERO_CHUNKS,
    FAILED_LIMIT,
    FAILED_DEADLINE,
    FAILED_SELF_CHECK,
    FAILED_SELF_CHECK_INCOMPLETE,
    FAILED_WRONG_STATUS,
    FAILED_FILE_HASH,
    FAILED_CHUNK_HASH,
    OK_LOAD,
    OK_APPROVE,
)

_BY_SQLSTATE: dict[str, Outcome] = {
    "55P03": FAILED_LOCK_BUSY,  # never retried (Q9)
    "57014": FAILED_STATEMENT_TIMEOUT,
    "40P01": FAILED_DEADLOCK,  # never retried (Q9)
    "25001": FAILED_ISOLATION,
}
_BY_REFUSAL: tuple[tuple[type[LoaderRefusal], Outcome], ...] = (
    (DeadlineExceeded, FAILED_DEADLINE),
    (kb_manifest.ZeroChunks, FAILED_ZERO_CHUNKS),
    (kb_manifest.FileHashMismatch, FAILED_FILE_HASH),
    (kb_manifest.ChunkHashMismatch, FAILED_CHUNK_HASH),
    (kb_manifest.ManifestTooLarge, FAILED_LIMIT),
    (LimitExceeded, FAILED_LIMIT),
    (kb_manifest.ManifestInvalid, FAILED_MANIFEST_INVALID),
    (LimitNotSet, FAILED_CONFIGURATION),
    (kb_screen.UnicodeNotReviewed, FAILED_CONFIGURATION),
    (ArgumentRefused, FAILED_ARGUMENTS),
)


class _NotFound(Exception):
    pass


class _WrongStatus(Exception):
    pass


class _HashMismatch(Exception):
    pass


@dataclass(frozen=True)
class Request:
    command: str
    document_id: str | None
    version: int | None
    supplied_hash: str | None
    development_flag: bool


def parse_arguments(args: Sequence[str]) -> Request:
    if not args or args[0] not in ("load", "approve"):
        raise ArgumentRefused("arguments refused")
    command = args[0]
    positional: list[str] = []
    supplied: str | None = None
    dev = False
    rest = list(args[1:])
    while rest:
        item = rest.pop(0)
        if item == FLAG_HASH:
            if supplied is not None or not rest or not valid_manifest_sha256(rest[0]):
                raise ArgumentRefused("arguments refused")
            supplied = rest.pop(0)
        elif item == FLAG_DEV:
            if dev:
                raise ArgumentRefused("arguments refused")
            dev = True
        elif item.startswith("-"):
            raise ArgumentRefused("arguments refused")
        else:
            positional.append(item)
    if dev and supplied is not None:
        raise ArgumentRefused("arguments refused")
    if command == "load":
        if positional:
            raise ArgumentRefused("arguments refused")
        return Request(command, None, None, supplied, dev)
    document_id, version = parse_document_version(positional)
    return Request(command, document_id, version, supplied, dev)


class HashRequired(LoaderRefusal):
    pass


def hash_must_be_checked(production: bool, request: Request) -> bool:
    """The hash is skipped ONLY with the flag AND a non-production environment. The environment
    alone never skips it; the flag alone in production is refused."""
    if request.development_flag:
        if production:
            raise HashRequired("the development flag is refused in production")
        return False
    if request.supplied_hash is None:
        raise HashRequired("the approved manifest hash is required")
    return True


def _set_local(conn: psycopg.Connection[Any], name: str, milliseconds: int) -> None:
    if name not in {"lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout"}:
        raise ValueError("not a timeout setting")
    conn.execute(sql.SQL("SET LOCAL {} = {}").format(sql.SQL(name), sql.Literal(milliseconds)))


def _begin(conn: psycopg.Connection[Any]) -> None:
    """READ COMMITTED first, then the SET LOCAL timeouts, then the global lock."""
    conn.read_only = False
    conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    conn.execute(
        sql.SQL("SET LOCAL search_path = {}, {}").format(
            sql.Identifier("pg_catalog"), sql.Identifier("pg_temp")
        )
    )
    _set_local(conn, "lock_timeout", require_limit(kb_common.LOAD_LOCK_TIMEOUT_MS))
    _set_local(conn, "statement_timeout", require_limit(kb_common.LOAD_STATEMENT_TIMEOUT_MS))
    _set_local(
        conn,
        "idle_in_transaction_session_timeout",
        require_limit(kb_common.LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS),
    )
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),
    )


def _lock_document(conn: psycopg.Connection[Any], document_id: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (kb_common.KB_DOCUMENT_LOCK_CLASS, document_id),
    )


def _self_check(conn: psycopg.Connection[Any]) -> Outcome | None:
    """Hard refusal before any write. Each check is its own transaction."""
    timeout = kb_common.LOAD_SELF_CHECK_STATEMENT_TIMEOUT_MS
    try:
        limit = require_limit(timeout)
        if kb_privileges.running_with_app_or_owner_privileges(conn, statement_timeout_ms=limit):
            return FAILED_ROLE
        if kb_privileges.loader_privilege_problems(conn, statement_timeout_ms=limit):
            return FAILED_SELF_CHECK
    except kb_privileges.SelfCheckConnectionLost:
        return FAILED_CONNECTION
    except LimitNotSet:
        return FAILED_CONFIGURATION
    except kb_privileges.SelfCheckIncomplete:
        return FAILED_SELF_CHECK_INCOMPLETE
    return None


def _site_chunk_check(conn: psycopg.Connection[Any], corpus: LoadedCorpus) -> None:
    """Conservative per-site bound (decision 12), inside the global lock, after it is held."""
    rows = conn.execute(
        "SELECT d.document_id, d.version, d.site_id, d.all_sites, "
        "(SELECT count(*) FROM public.kb_chunks c WHERE c.document_id = d.document_id "
        " AND c.version = d.version), "
        "EXISTS (SELECT 1 FROM public.kb_status_events e WHERE e.document_id = d.document_id "
        " AND e.version = d.version AND e.status = 'APPROVED'), "
        "EXISTS (SELECT 1 FROM public.kb_status_events e WHERE e.document_id = d.document_id "
        " AND e.version = d.version AND e.status = 'WITHDRAWN') FROM public.kb_documents d"
    ).fetchall()
    versions: list[tuple[str, str | None, bool, int]] = []
    for document_id, _v, site_id, all_sites, count, approved, withdrawn in rows:
        if withdrawn and not approved:  # withdrawn without approval cannot govern
            continue
        versions.append((document_id, site_id, bool(all_sites), int(count)))
    for doc in corpus.documents:
        e = doc.entry
        versions.append((e.document_id, e.site_id, e.all_sites, len(doc.chunks)))
    for site in set(SITES) | {s for _, s, _, _ in versions if s is not None}:
        largest: dict[str, int] = {}
        for document_id, site_id, all_sites, count in versions:
            if all_sites or site_id == site:
                largest[document_id] = max(largest.get(document_id, 0), count)
        if sum(largest.values()) > kb_common.MAX_CHUNKS_PER_RUN:
            raise LimitExceeded("a site would exceed the per-run chunk bound")


def _write_load(
    conn: psycopg.Connection[Any],
    corpus: LoadedCorpus,
    deadline: Deadline,
    on_send: Callable[[], None],
) -> None:
    docs = sorted(corpus.documents, key=lambda d: d.entry.document_id)
    for doc in docs:  # sorted: a fixed lock order
        _lock_document(conn, doc.entry.document_id)
        deadline.check()
    # load's own reads get the approved 10 s (Q8); the 30 s of the other statements is restored.
    _set_local(conn, "statement_timeout", require_limit(kb_common.LOAD_READ_STATEMENT_TIMEOUT_MS))
    _site_chunk_check(conn, corpus)
    _set_local(conn, "statement_timeout", require_limit(kb_common.LOAD_STATEMENT_TIMEOUT_MS))
    deadline.check()
    on_send()  # the first INSERT follows: from here a failure may have written something
    for doc in docs:
        e = doc.entry
        conn.execute(
            "INSERT INTO public.kb_documents (document_id, version, title, origin, author_role, "
            "approver_role, effective_date, review_by_date, licence, licence_reference_present, "
            "attribution, site_id, all_sites, display_permitted, supersedes, manifest_sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, true, %s, %s)",
            (
                e.document_id,
                e.version,
                e.title,
                e.origin,
                e.author_role,
                kb_manifest.APPROVER_ROLE,
                e.effective_date,
                e.review_by_date,
                e.licence,
                e.attribution,
                e.site_id,
                e.all_sites,
                e.supersedes,
                corpus.manifest_sha256,
            ),
        )
        deadline.check()
        for c in doc.chunks:
            conn.execute(
                "INSERT INTO public.kb_chunks (document_id, version, chunk, section, chunk_text, "
                "content_sha256) VALUES (%s, %s, %s, %s, %s, %s)",
                (e.document_id, e.version, c.chunk, c.section, c.text, c.sha256),
            )
            deadline.check()
        conn.execute(
            "INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) "
            "VALUES (%s, %s, 'DRAFT', false)",
            (e.document_id, e.version),
        )
        deadline.check()


def _write_approve(
    conn: psycopg.Connection[Any],
    request: Request,
    checked: bool,
    deadline: Deadline,
    on_send: Callable[[], None],
) -> datetime:
    assert request.document_id is not None and request.version is not None
    _lock_document(conn, request.document_id)
    deadline.check()
    row = conn.execute(
        "SELECT manifest_sha256 FROM public.kb_documents WHERE document_id = %s AND version = %s",
        (request.document_id, request.version),
    ).fetchone()
    if row is None:
        raise _NotFound
    status = conn.execute(
        "SELECT status FROM public.kb_status_events WHERE document_id = %s AND version = %s "
        "ORDER BY event_order DESC LIMIT 1",
        (request.document_id, request.version),
    ).fetchone()
    if status is None or status[0] != "DRAFT":
        raise _WrongStatus
    if checked and row[0] != request.supplied_hash:
        raise _HashMismatch
    deadline.check()
    on_send()  # the INSERT follows
    inserted = conn.execute(
        "INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) "
        "VALUES (%s, %s, 'APPROVED', %s) RETURNING recorded_at",
        (request.document_id, request.version, checked),
    ).fetchone()
    if inserted is None or not isinstance(inserted[0], datetime):
        raise psycopg.DatabaseError("no recorded_at")
    return inserted[0]


def _discard(conn: psycopg.Connection[Any]) -> None:
    with contextlib.suppress(Exception):
        conn.rollback()


def _uncertain(exc: psycopg.Error) -> bool:
    state = exc.sqlstate
    return state is None or state.startswith(("08", "57P"))


def _emit(stream: Any, text: str) -> None:
    stream.write(text + "\n")
    stream.flush()


def _run_on(
    conn: psycopg.Connection[Any],
    request: Request,
    corpus: LoadedCorpus | None,
    checked: bool,
    deadline: Deadline,
) -> Outcome:
    early = _self_check(conn)
    if early is not None:
        return early
    sent = False
    recorded: datetime | None = None

    def mark_sent() -> None:
        nonlocal sent
        sent = True

    try:
        deadline.check()
        _begin(conn)
        deadline.check()
        if request.command == "load":
            assert corpus is not None
            _write_load(conn, corpus, deadline, mark_sent)
        else:
            recorded = _write_approve(conn, request, checked, deadline, mark_sent)
        deadline.check()
    except DeadlineExceeded:
        _discard(conn)
        return FAILED_DEADLINE
    except LimitExceeded:
        _discard(conn)
        return FAILED_LIMIT
    except LimitNotSet:
        _discard(conn)
        return FAILED_CONFIGURATION
    except _NotFound:
        _discard(conn)
        return FAILED_NOT_FOUND
    except _WrongStatus:
        _discard(conn)
        return FAILED_WRONG_STATUS
    except _HashMismatch:
        _discard(conn)
        return FAILED_HASH_MISMATCH
    except psycopg.Error as exc:
        _discard(conn)
        if _uncertain(exc):  # before the first INSERT: a lost connection wrote nothing
            return OUTCOME_UNKNOWN if sent else FAILED_CONNECTION
        return _BY_SQLSTATE.get(exc.sqlstate or "", FAILED_OTHER)
    except Exception:  # noqa: BLE001 - an unexpected failure after a send has an unknown fate
        _discard(conn)
        return OUTCOME_UNKNOWN if sent else FAILED_OTHER
    try:
        conn.commit()
    except Exception:  # noqa: BLE001
        return OUTCOME_UNKNOWN
    if recorded is not None:
        with contextlib.suppress(OSError):
            _emit(sys.stdout, f"recorded_at={recorded.isoformat()}")
    return OK_LOAD if request.command == "load" else OK_APPROVE


def run(request: Request, settings: LoaderSettings) -> Outcome:
    seconds = (
        kb_common.LOAD_DEADLINE_SECONDS
        if request.command == "load"
        else kb_common.APPROVE_DEADLINE_SECONDS
    )
    deadline = Deadline(seconds)  # started before the connection
    try:
        kb_screen.require_reviewed_unicode()  # before any corpus read or connection (exit 3)
    except kb_screen.UnicodeNotReviewed:
        return FAILED_CONFIGURATION
    try:
        checked = hash_must_be_checked(settings.production, request)
        target = settings.require_corpus_target()
    except HashRequired:
        return FAILED_HASH_REQUIRED
    except LoaderConfigError:
        return FAILED_CONFIGURATION
    corpus: LoadedCorpus | None = None
    if request.command == "load":
        try:
            corpus = _read(target)
            if checked and corpus.manifest_sha256 != request.supplied_hash:
                return FAILED_HASH_MISMATCH
        except LoaderRefusal as exc:
            return next((o for t, o in _BY_REFUSAL if isinstance(exc, t)), FAILED_MANIFEST_INVALID)
    try:
        conn = kb_common.connect(settings.loader_database_url.get_secret_value())
    except LimitNotSet:
        return FAILED_CONFIGURATION
    except psycopg.Error:
        return FAILED_CONNECTION
    try:
        return _run_on(conn, request, corpus, checked, deadline)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _read(target: Path) -> LoadedCorpus:
    return kb_manifest.read_corpus(target)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    outcome = _main(args)
    with contextlib.suppress(OSError):
        _emit(sys.stdout if outcome.status == 0 else sys.stderr, outcome.message)
    return outcome.status


def _main(args: list[str]) -> Outcome:
    try:
        request = parse_arguments(args)
    except LoaderRefusal:
        return FAILED_ARGUMENTS
    try:
        settings = load_loader_settings()
    except LoaderConfigError:
        return FAILED_CONFIGURATION
    return run(request, settings)


if __name__ == "__main__":
    raise SystemExit(main())
