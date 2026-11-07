"""`load` and `approve` against a real PostgreSQL as a real loader role (ADR 0024 A3; ADR 0022
stage 1b).

Needs DIVESAFE_TEST_DATABASE_URL (a database named *_test); skipped otherwise. Offline these only
collect and skip, so they prove nothing until a live run.

Every test gets its OWN scratch database (named *_test, created and force-dropped here), because
the per-site chunk bound counts every document in the database and the knowledge tables are
append-only: a shared database would make results depend on test order. Scratch roles end in
_test and are dropped in a finally. The lock timeout is PATCHED to 0.5 s (statement timeout 5 s,
still above it) for speed; the approved 10 s / 30 s values are pinned by the offline tests. The DSN
is never printed or stored: results are assigned to variables before they are asserted."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr
from tests.integration.test_knowledge_store import PASSWORD, _drop_role, _fresh_id
from tests.safety.kb_corpus import doc_spec, sha, write_corpus
from tests.safety.kb_fakes import mutated_module
from tests.unit.test_repository_contract import TEST_DSN as DSN

from divesafe.config.loader_settings import LoaderSettings
from divesafe.services import kb_common, kb_load, kb_withdraw
from divesafe.services.postgres import install_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
]

APP = "divesafe_kb_ldapp_test"
LOADER = "divesafe_kb_ldloader_test"
EXTRA = "divesafe_kb_ldextra_test"
SHORT_LOCK_MS = 500
SHORT_STATEMENT_MS = 5_000
OK = kb_load.OK_LOAD.status  # exactly 0: the scratch loader is least-privilege; 19 is a finding


def _dsn_for(database: str, user: str | None = None) -> str:
    assert DSN is not None
    parts = psycopg.conninfo.conninfo_to_dict(DSN)
    parts["dbname"] = database
    if user is not None:
        parts.update(user=user, password=PASSWORD)
    return psycopg.conninfo.make_conninfo(**parts)


@dataclass
class Scratch:
    database: str
    admin_dsn: str
    loader_dsn: str

    def admin(self, statement: str) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(statement)  # type: ignore[call-overload]

    def rows(self, query: str, params: Any = None) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.admin_dsn) as conn:
            cur = conn.execute(query, params)  # type: ignore[call-overload]
            return cur.fetchall() if cur.description else []

    def events(self, document_id: str) -> list[tuple[Any, ...]]:
        return self.rows(
            "SELECT status, hash_checked, recorded_at FROM public.kb_status_events "
            "WHERE document_id = %s ORDER BY event_order",
            (document_id,),
        )

    def counts(self) -> tuple[int, int, int]:
        found = self.rows(
            "SELECT (SELECT count(*) FROM public.kb_documents), "
            "(SELECT count(*) FROM public.kb_chunks), "
            "(SELECT count(*) FROM public.kb_status_events)"
        )
        return (int(found[0][0]), int(found[0][1]), int(found[0][2]))


@pytest.fixture(scope="module")
def _roles() -> Iterator[None]:
    assert DSN is not None
    name = str(psycopg.conninfo.conninfo_to_dict(DSN).get("dbname", ""))
    if not name.endswith("_test"):
        raise RuntimeError("DIVESAFE_TEST_DATABASE_URL must point at a database named *_test")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for role in (APP, LOADER, EXTRA):
            _drop_role(conn, role)
            conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'")  # type: ignore[call-overload]
    try:
        yield
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for role in (APP, LOADER, EXTRA):
                _drop_role(conn, role)


@pytest.fixture
def scratch(_roles: None) -> Iterator[Scratch]:
    assert DSN is not None
    database = f"divesafe_ld_{uuid.uuid4().hex[:10]}_test"
    assert database.endswith("_test")
    base = DSN
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database}"')  # type: ignore[call-overload]
    admin_dsn = _dsn_for(database)
    try:
        install_schema(admin_dsn, APP, loader_role=LOADER)
        yield Scratch(database, admin_dsn, _dsn_for(database, LOADER))
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')  # type: ignore[call-overload]
            # cluster-level memberships granted by a test must not leak into the next one
            for member, role in ((LOADER, APP), (LOADER, EXTRA)):
                conn.execute(f"REVOKE {role} FROM {member}")  # type: ignore[call-overload]


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kb_common, "LOAD_LOCK_TIMEOUT_MS", SHORT_LOCK_MS)
    monkeypatch.setattr(kb_common, "LOAD_STATEMENT_TIMEOUT_MS", SHORT_STATEMENT_MS)
    monkeypatch.setattr(kb_common, "WITHDRAW_LOCK_TIMEOUT_MS", SHORT_LOCK_MS)
    monkeypatch.setattr(kb_common, "WITHDRAW_STATEMENT_TIMEOUT_MS", SHORT_STATEMENT_MS)


def _settings(dsn: str, corpus: Path | None) -> LoaderSettings:
    return LoaderSettings(
        environment="test", loader_database_url=SecretStr(dsn), corpus_target=corpus
    )


def _go(args: list[str], dsn: str, corpus: Path | None) -> kb_load.Outcome:
    result = kb_load.run(kb_load.parse_arguments(args), _settings(dsn, corpus))
    return kb_load.Outcome(result.status, result.message)


def _load_args(manifest_hash: str) -> list[str]:
    return ["load", "--approved-manifest-sha256", manifest_hash]


def _corpus(tmp_path: Path, specs: list[dict[str, Any]] | None = None) -> tuple[Path, str]:
    root = tmp_path / f"corpus-{uuid.uuid4().hex[:8]}"
    return root, write_corpus(root, specs)


def _stored_hash(scratch: Scratch, document_id: str) -> str:
    found = scratch.rows(
        "SELECT manifest_sha256 FROM public.kb_documents WHERE document_id = %s", (document_id,)
    )
    return str(found[0][0])


# --- load ----------------------------------------------------------------------------------------


def test_load_as_the_loader_writes_documents_chunks_and_a_draft_event(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path, [doc_spec("alpha-guide"), doc_spec("beta-guide")])
    outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome.status == OK, outcome.message
    assert scratch.counts() == (2, 4, 2)
    for document_id in ("alpha-guide", "beta-guide"):
        events = scratch.events(document_id)
        assert [(e[0], e[1]) for e in events] == [("DRAFT", False)]  # never claims a hash check
        assert _stored_hash(scratch, document_id) == digest
    texts = scratch.rows(
        "SELECT chunk, chunk_text FROM public.kb_chunks WHERE document_id = 'alpha-guide' "
        "ORDER BY chunk"
    )
    assert texts == [(1, "Alpha text.\n"), (2, "Beta text.\n")]


def test_a_second_load_of_an_existing_version_exits_11_and_writes_nothing_more(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK
    before = scratch.counts()
    outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_OTHER
    assert scratch.counts() == before


def test_a_load_that_fails_part_way_rolls_back_every_document(
    scratch: Scratch, tmp_path: Path
) -> None:
    """Two documents in one transaction: the second collides with an existing version, so the
    first must not remain."""
    first, digest = _corpus(tmp_path / "first", [doc_spec("beta-guide")])
    assert _go(_load_args(digest), scratch.loader_dsn, first).status == OK
    before = scratch.counts()
    second, digest2 = _corpus(
        tmp_path / "second", [doc_spec("alpha-guide"), doc_spec("beta-guide")]
    )
    outcome = _go(_load_args(digest2), scratch.loader_dsn, second)
    assert outcome == kb_load.FAILED_OTHER
    assert scratch.counts() == before
    assert scratch.events("alpha-guide") == []


def test_a_zero_chunk_document_is_refused_and_nothing_is_written(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(
        tmp_path, [doc_spec("alpha-guide"), doc_spec("beta-guide", chunks=(), gap="Heading\n")]
    )
    outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_ZERO_CHUNKS
    assert scratch.counts() == (0, 0, 0)


def test_a_wrong_manifest_hash_is_refused_and_nothing_is_written(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, _ = _corpus(tmp_path)
    outcome = _go(_load_args(sha(b"another manifest")), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_HASH_MISMATCH
    assert scratch.counts() == (0, 0, 0)


# --- approve -------------------------------------------------------------------------------------


def test_approve_records_an_approved_event_with_a_checked_hash_and_prints_recorded_at(
    scratch: Scratch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK
    capsys.readouterr()
    outcome = _go(
        ["approve", "site-guide", "1", "--approved-manifest-sha256", digest],
        scratch.loader_dsn,
        root,
    )
    assert outcome.status == 0, outcome.message
    events = scratch.events("site-guide")
    assert [(e[0], e[1]) for e in events] == [("DRAFT", False), ("APPROVED", True)]
    printed = [x for x in capsys.readouterr().out.splitlines() if x.startswith("recorded_at=")]
    assert printed == [f"recorded_at={events[1][2].isoformat()}"]


def test_approve_with_a_hash_that_is_not_the_stored_one_exits_14_and_writes_nothing(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK
    outcome = _go(
        ["approve", "site-guide", "1", "--approved-manifest-sha256", sha(b"other")],
        scratch.loader_dsn,
        root,
    )
    assert outcome == kb_load.FAILED_HASH_MISMATCH
    assert [e[0] for e in scratch.events("site-guide")] == ["DRAFT"]


def test_approve_of_a_version_that_is_not_draft_exits_21(scratch: Scratch, tmp_path: Path) -> None:
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK
    args = ["approve", "site-guide", "1", "--approved-manifest-sha256", digest]
    assert _go(args, scratch.loader_dsn, root).status == 0
    outcome = _go(args, scratch.loader_dsn, root)  # now APPROVED
    assert outcome == kb_load.FAILED_WRONG_STATUS
    assert [e[0] for e in scratch.events("site-guide")] == ["DRAFT", "APPROVED"]
    withdrawn = kb_withdraw.run_withdraw(["site-guide", "1"], scratch.loader_dsn)
    assert withdrawn.status == 0
    outcome = _go(args, scratch.loader_dsn, root)  # now WITHDRAWN
    assert outcome == kb_load.FAILED_WRONG_STATUS


def test_approve_of_a_missing_version_exits_10(scratch: Scratch, tmp_path: Path) -> None:
    root, digest = _corpus(tmp_path)
    args = ["approve", "no-such-guide", "1", "--approved-manifest-sha256", digest]
    assert _go(args, scratch.loader_dsn, root) == kb_load.FAILED_NOT_FOUND


def test_approve_in_development_without_a_hash_records_an_unchecked_approval(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK
    args = ["approve", "site-guide", "1", "--development-no-manifest-hash"]
    assert _go(args, scratch.loader_dsn, root).status == 0
    assert [(e[0], e[1]) for e in scratch.events("site-guide")] == [
        ("DRAFT", False),
        ("APPROVED", False),
    ]


# --- who may run it ------------------------------------------------------------------------------


def test_the_owner_is_refused_with_5_before_anything_is_written(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    outcome = _go(_load_args(digest), scratch.admin_dsn, root)
    assert outcome == kb_load.FAILED_ROLE
    assert scratch.counts() == (0, 0, 0)


def test_a_loader_that_is_a_member_of_a_custom_named_app_role_is_refused_with_5(
    scratch: Scratch, tmp_path: Path
) -> None:
    """Closes the gap that `running_as_privileged_role` (names only) leaves open for withdraw:
    `running_with_app_or_owner_privileges` reads privileges. The role name here is not reserved."""
    assert APP not in ("divesafe_app", "divesafe_owner", "divesafe_bootstrap")
    root, digest = _corpus(tmp_path)
    scratch.admin(f"GRANT {APP} TO {LOADER}")
    outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_ROLE
    assert scratch.counts() == (0, 0, 0)


def test_a_loader_with_a_custom_role_that_can_write_an_app_table_is_refused_with_5(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    scratch.admin(f"GRANT INSERT ON public.pending_runs TO {EXTRA}")
    scratch.admin(f"GRANT {EXTRA} TO {LOADER}")
    outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_ROLE
    assert scratch.counts() == (0, 0, 0)


def test_the_approve_command_refuses_the_same_roles(scratch: Scratch, tmp_path: Path) -> None:
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK
    args = ["approve", "site-guide", "1", "--approved-manifest-sha256", digest]
    assert _go(args, scratch.admin_dsn, root) == kb_load.FAILED_ROLE
    scratch.admin(f"GRANT {APP} TO {LOADER}")
    assert _go(args, scratch.loader_dsn, root) == kb_load.FAILED_ROLE
    assert [e[0] for e in scratch.events("site-guide")] == ["DRAFT"]


def test_an_over_privileged_loader_is_refused_with_19_before_a_write(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    scratch.admin(f"GRANT UPDATE ON public.kb_documents TO {LOADER}")
    outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_SELF_CHECK
    assert scratch.counts() == (0, 0, 0)


def test_a_correctly_provisioned_loader_passes_both_checks(scratch: Scratch) -> None:
    conn = psycopg.connect(scratch.loader_dsn)
    try:
        from divesafe.services import kb_privileges

        app_or_owner = kb_privileges.running_with_app_or_owner_privileges(
            conn, statement_timeout_ms=15_000
        )
        problems = kb_privileges.loader_privilege_problems(conn, statement_timeout_ms=15_000)
    finally:
        conn.close()
    assert app_or_owner is False
    assert problems == []


# --- locks ---------------------------------------------------------------------------------------


def test_load_behind_a_held_global_lock_refuses_with_6_within_the_patched_timeout(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    with psycopg.connect(scratch.admin_dsn) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),
        )
        started = time.monotonic()
        outcome = _go(_load_args(digest), scratch.loader_dsn, root)
        elapsed = time.monotonic() - started
    assert outcome == kb_load.FAILED_LOCK_BUSY
    assert elapsed < SHORT_LOCK_MS / 1000 + 3
    assert scratch.counts() == (0, 0, 0)


def test_load_behind_a_held_per_document_lock_refuses_with_6(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    with psycopg.connect(scratch.admin_dsn) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (kb_common.KB_DOCUMENT_LOCK_CLASS, "site-guide"),
        )
        outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_LOCK_BUSY
    assert scratch.counts() == (0, 0, 0)


def test_load_behind_a_lock_on_another_document_is_not_blocked(
    scratch: Scratch, tmp_path: Path
) -> None:
    root, digest = _corpus(tmp_path)
    with psycopg.connect(scratch.admin_dsn) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (kb_common.KB_DOCUMENT_LOCK_CLASS, "unrelated-guide"),
        )
        outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome.status == OK, outcome.message


def test_approve_behind_a_held_global_lock_refuses_with_6(scratch: Scratch, tmp_path: Path) -> None:
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK
    args = ["approve", "site-guide", "1", "--approved-manifest-sha256", digest]
    with psycopg.connect(scratch.admin_dsn) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),
        )
        outcome = _go(args, scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_LOCK_BUSY
    assert [e[0] for e in scratch.events("site-guide")] == ["DRAFT"]


def test_withdraw_is_not_blocked_while_a_load_holds_the_global_lock(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A load takes the global lock and then waits on a held per-document lock. A withdraw of a
    different document must still finish at once: withdraw never takes the global lock (a
    withdraw that did would wait here and exit 6)."""
    monkeypatch.setattr(kb_common, "LOAD_LOCK_TIMEOUT_MS", 8_000)
    monkeypatch.setattr(kb_common, "LOAD_STATEMENT_TIMEOUT_MS", 20_000)
    seed, seed_hash = _corpus(tmp_path / "seed", [doc_spec("other-guide")])
    assert _go(_load_args(seed_hash), scratch.loader_dsn, seed).status == OK
    root, digest = _corpus(tmp_path / "main")
    results: list[kb_load.Outcome] = []
    with psycopg.connect(scratch.admin_dsn) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (kb_common.KB_DOCUMENT_LOCK_CLASS, "site-guide"),
        )
        worker = threading.Thread(
            target=lambda: results.append(_go(_load_args(digest), scratch.loader_dsn, root))
        )
        worker.start()
        held = False
        for _ in range(100):  # wait until the load really holds the global lock
            granted = scratch.rows(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND classid = %s AND objid = %s",
                (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),
            )
            held = int(granted[0][0]) >= 1
            if held:
                break
            time.sleep(0.1)
        assert held, "the load never took the global lock, so this test would prove nothing"
        started = time.monotonic()
        withdrawn = kb_withdraw.run_withdraw(["other-guide", "1"], scratch.loader_dsn)
        elapsed = time.monotonic() - started
    worker.join(30)
    assert withdrawn.status == 0, withdrawn.message
    assert elapsed < SHORT_LOCK_MS / 1000 + 2
    assert [e[0] for e in scratch.events("other-guide")] == ["DRAFT", "WITHDRAWN"]
    assert len(results) == 1 and results[0].status == OK  # the load finished once released


# --- the per-site chunk bound --------------------------------------------------------------------


def _seed_chunks(scratch: Scratch, document_id: str, count: int, *, status: str | None) -> None:
    """An existing all-sites version with `count` chunks, written as the owner."""
    base = {
        "document_id": document_id,
        "version": 1,
        "title": "Synthetic existing document",
        "origin": "dive_professional",
        "author_role": "dive_professional",
        "approver_role": "dive_professional",
        "effective_date": "2026-01-01",
        "review_by_date": "2026-12-01",
        "licence": "Synthetic test licence",
        "licence_reference_present": True,
        "attribution": None,
        "site_id": None,
        "all_sites": True,
        "display_permitted": True,
        "supersedes": None,
        "manifest_sha256": "a" * 64,
    }
    columns = ", ".join(base)
    marks = ", ".join(f"%({c})s" for c in base)
    with psycopg.connect(scratch.admin_dsn) as conn:
        conn.execute(
            f"INSERT INTO public.kb_documents ({columns}) VALUES ({marks})",  # type: ignore[call-overload]
            base,
        )
        for number in range(1, count + 1):
            text = f"Existing chunk {number}."
            conn.execute(
                "INSERT INTO public.kb_chunks (document_id, version, chunk, section, chunk_text, "
                "content_sha256) VALUES (%s, 1, %s, 'S', %s, %s)",
                (document_id, number, text, sha(text.encode())),
            )
        if status is not None:
            conn.execute(
                "INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) "
                "VALUES (%s, 1, %s, false)",
                (document_id, status),
            )


def test_the_per_site_bound_allows_exactly_twenty_chunks(scratch: Scratch, tmp_path: Path) -> None:
    _seed_chunks(scratch, _fresh_id(), 18, status="DRAFT")
    root, digest = _corpus(tmp_path)  # 2 new chunks: 20 in all
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK


def test_the_per_site_bound_refuses_twenty_one_chunks_with_17_and_writes_nothing(
    scratch: Scratch, tmp_path: Path
) -> None:
    _seed_chunks(scratch, _fresh_id(), 19, status="DRAFT")
    before = scratch.counts()
    root, digest = _corpus(tmp_path)  # 2 new chunks: 21 in all
    outcome = _go(_load_args(digest), scratch.loader_dsn, root)
    assert outcome == kb_load.FAILED_LIMIT
    assert scratch.counts() == before


def test_a_version_withdrawn_without_approval_does_not_count_against_the_bound(
    scratch: Scratch, tmp_path: Path
) -> None:
    did = _fresh_id()
    _seed_chunks(scratch, did, 19, status="DRAFT")
    with psycopg.connect(scratch.loader_dsn) as conn:
        conn.execute(
            "INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) "
            "VALUES (%s, 1, 'WITHDRAWN', false)",
            (did,),
        )
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root).status == OK


def test_an_approved_then_withdrawn_version_still_counts_against_the_bound(
    scratch: Scratch, tmp_path: Path
) -> None:
    did = _fresh_id()
    _seed_chunks(scratch, did, 19, status="DRAFT")
    with psycopg.connect(scratch.admin_dsn) as conn:
        # the CHECK allows hash_checked = true only on APPROVED; WITHDRAWN must carry false
        for status, checked in (("APPROVED", True), ("WITHDRAWN", False)):
            conn.execute(
                "INSERT INTO public.kb_status_events (document_id, version, status, "
                "hash_checked) VALUES (%s, 1, %s, %s)",
                (did, status, checked),
            )
    root, digest = _corpus(tmp_path)
    assert _go(_load_args(digest), scratch.loader_dsn, root) == kb_load.FAILED_LIMIT


# --- round 8: a stalled transaction, a black hole, a race, an opposite lock order -----------------
# NOT RUN by the test engineer (the owner runs the live tests).

IDLE_MS = 1_000
STALL_SECONDS = 5.0
RACE_PAUSE_SECONDS = 2.0


def _go_with(module: Any, args: list[str], dsn: str, corpus: Path | None) -> kb_load.Outcome:
    """Like `_go`, for the real module or a mutated COPY; the outcome is compared by value."""
    result = module.run(module.parse_arguments(args), _settings(dsn, corpus))
    return kb_load.Outcome(result.status, result.message)


def _wait_for(predicate: Callable[[], bool], seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _global_lock_held(scratch: Scratch) -> bool:
    found = scratch.rows(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
        "AND classid = %s AND objid = %s",
        (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),
    )
    return int(found[0][0]) >= 1


def _global_lock_free(scratch: Scratch) -> bool:
    with psycopg.connect(scratch.admin_dsn) as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),
        ).fetchone()
    return bool(row and row[0])


def _stalled_load(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Any
) -> tuple[float | None, kb_load.Outcome | None, list[psycopg.Error]]:
    """A load that sleeps STALL_SECONDS inside its transaction (idle in transaction, holding the
    global lock). Returns the seconds until a second session could take the global lock, the
    load's outcome and the database errors the stalled connection met when it woke."""
    monkeypatch.setattr(kb_common, "LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS", IDLE_MS)
    root, digest = _corpus(tmp_path)
    original = module._site_chunk_check
    errors: list[psycopg.Error] = []

    def stalled(conn: Any, corpus: Any) -> None:
        time.sleep(STALL_SECONDS)  # no statement is running: the session is idle in transaction
        try:
            original(conn, corpus)
        except psycopg.Error as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(module, "_site_chunk_check", stalled)
    results: list[kb_load.Outcome] = []
    worker = threading.Thread(
        target=lambda: results.append(
            _go_with(module, _load_args(digest), scratch.loader_dsn, root)
        )
    )
    worker.start()
    freed: float | None = None
    try:
        held = _wait_for(lambda: _global_lock_held(scratch), 10)
        assert held, "the load never took the global lock, so this test would prove nothing"
        started = time.monotonic()
        if _wait_for(lambda: _global_lock_free(scratch), STALL_SECONDS + 10):
            freed = time.monotonic() - started
    finally:
        worker.join(STALL_SECONDS + 30)
    assert not worker.is_alive()
    return freed, (results[0] if results else None), errors


def test_a_load_that_stalls_inside_its_transaction_is_ended_by_the_server(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server, not the client, must free the global lock: a stalled loader would otherwise
    block every other load until the client came back (or never)."""
    freed, outcome, errors = _stalled_load(scratch, tmp_path, monkeypatch, kb_load)
    assert freed is not None
    assert freed < STALL_SECONDS - 1.5  # freed by the server while the client was still asleep
    assert freed >= 0.3  # and not at once: the lock was held until the idle timeout
    # The live run raises psycopg.errors.IdleInTransactionSessionTimeout, which is not an
    # OperationalError subclass, so only the base class and the SQLSTATE are asserted.
    assert len(errors) == 1 and isinstance(errors[0], psycopg.Error)
    sqlstate = errors[0].sqlstate
    # 25P03 idle_in_transaction_session_timeout; 57P05 idle_session_timeout. Anything else
    # (including None) is a finding.
    assert sqlstate in {"25P03", "57P05"}
    assert outcome is not None
    assert outcome in (kb_load.FAILED_CONNECTION, kb_load.FAILED_OTHER)  # before the first INSERT
    assert scratch.counts() == (0, 0, 0)


def test_mutation_without_the_idle_timeout_the_lock_is_held_for_the_whole_stall(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live mutation on a COPY of kb_load: no idle SET LOCAL. The same stall now holds the global
    lock for its whole length, which the test above would call a failure."""
    idle = (
        "    _set_local(\n"
        "        conn,\n"
        '        "idle_in_transaction_session_timeout",\n'
        "        require_limit(kb_common.LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS),\n"
        "    )\n"
    )
    mutant = mutated_module(kb_load, (idle, ""))
    freed, outcome, errors = _stalled_load(scratch, tmp_path, monkeypatch, mutant)
    assert freed is not None
    assert freed > STALL_SECONDS - 1.0
    assert errors == []
    assert outcome is not None and outcome.status == OK


def test_connecting_to_a_black_holed_address_fails_within_the_connect_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """192.0.2.1 is TEST-NET-1 (RFC 5737), never routed. A hang here would be the OS TCP timeout
    (minutes). Skipped when this environment answers at once (no route, or a refusal), because
    then the timeout was never exercised."""
    monkeypatch.setattr(kb_common, "CONNECT_TIMEOUT_SECONDS", 1)
    root, digest = _corpus(tmp_path)
    started = time.monotonic()
    outcome = _go(_load_args(digest), "postgresql://nobody@192.0.2.1:5432/blackhole_test", root)
    elapsed = time.monotonic() - started
    if elapsed < 0.5:
        pytest.skip(f"the address failed at once ({elapsed:.2f}s): the timeout was not exercised")
    assert outcome == kb_load.FAILED_CONNECTION
    assert elapsed < 1 + 3


def _chunks(prefix: str, count: int) -> list[str]:
    return [f"{prefix} text number {n}.\n" for n in range(1, count + 1)]


def _race(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Any
) -> tuple[kb_load.Outcome, kb_load.Outcome, tuple[int, int, int]]:
    """Two loads of different all-sites documents with 11 chunks each: 11 passes the bound of 20
    alone, 22 does not. Load A passes its check and then pauses holding whatever it holds; load B
    starts during the pause. With the global lock B waits and then sees A's rows (refused, 17).
    Without it B checks against an empty database and both commit."""
    monkeypatch.setattr(kb_common, "LOAD_LOCK_TIMEOUT_MS", 8_000)
    monkeypatch.setattr(kb_common, "LOAD_STATEMENT_TIMEOUT_MS", 20_000)
    root_a, hash_a = _corpus(tmp_path / "a", [doc_spec("alpha-guide", chunks=_chunks("alpha", 11))])
    root_b, hash_b = _corpus(tmp_path / "b", [doc_spec("beta-guide", chunks=_chunks("beta", 11))])
    original = module._site_chunk_check
    entered = threading.Event()
    passed: list[int] = []

    def paused(conn: Any, corpus: Any) -> None:
        original(conn, corpus)  # raises for a load that would exceed the bound
        passed.append(1)
        if len(passed) == 1:
            entered.set()
            time.sleep(RACE_PAUSE_SECONDS)

    monkeypatch.setattr(module, "_site_chunk_check", paused)
    results: dict[str, kb_load.Outcome] = {}

    def run(name: str, root: Path, digest: str) -> None:
        results[name] = _go_with(module, _load_args(digest), scratch.loader_dsn, root)

    first = threading.Thread(target=run, args=("a", root_a, hash_a))
    second = threading.Thread(target=run, args=("b", root_b, hash_b))
    first.start()
    try:
        assert entered.wait(15), "load A never reached its pause, so the race was not run"
        second.start()
    finally:
        first.join(40)
        if second.is_alive() or second.ident is not None:
            second.join(40)
    assert not first.is_alive() and not second.is_alive()
    return results["a"], results["b"], scratch.counts()


def test_two_loads_that_pass_the_bound_alone_but_not_together_are_serialised(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, counts = _race(scratch, tmp_path, monkeypatch, kb_load)
    assert first.status == OK, first.message
    assert second == kb_load.FAILED_LIMIT  # 17: refused because A's rows are visible to B
    assert counts == (1, 11, 1)  # one document, never 22 chunks


def test_mutation_without_the_global_lock_both_loads_commit_and_the_bound_is_broken(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live mutation on a COPY of kb_load: the global lock statement removed. The other mutation
    to keep in mind (not run live): the check done BEFORE the lock is taken has the same result,
    because B's check then precedes A's commit."""
    lock = (
        "    conn.execute(\n"
        '        "SELECT pg_advisory_xact_lock(%s, %s)",\n'
        "        (kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT),\n"
        "    )\n"
    )
    mutant = mutated_module(kb_load, (lock, ""))
    first, second, counts = _race(scratch, tmp_path, monkeypatch, mutant)
    assert first.status == OK and second.status == OK
    assert counts == (2, 22, 2)  # 22 chunks for every site: what decision 12 forbids


@pytest.mark.parametrize("lock_ms", [SHORT_LOCK_MS, 8_000], ids=["lock-timeout-first", "deadlock"])
def test_a_raw_writer_locking_documents_in_the_opposite_order_ends_in_a_fixed_refusal(
    lock_ms: int, scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load locks alpha-guide then beta-guide. A raw session holds beta-guide and, once the
    load is waiting on it, asks for alpha-guide: a lock cycle. With the short lock timeout the
    load gives up first (6); with the long one the server's deadlock detector aborts the load,
    which has waited longest (9). Either way the load writes nothing and nothing hangs."""
    monkeypatch.setattr(kb_common, "LOAD_LOCK_TIMEOUT_MS", lock_ms)
    monkeypatch.setattr(kb_common, "LOAD_STATEMENT_TIMEOUT_MS", 20_000)
    root, digest = _corpus(
        tmp_path, [doc_spec("alpha-guide"), doc_spec("beta-guide", chunks=_chunks("beta", 2))]
    )
    holding = threading.Event()
    raw: list[str] = []

    def raw_writer() -> None:
        try:
            with psycopg.connect(scratch.admin_dsn) as conn:
                conn.execute("SET LOCAL lock_timeout = '20s'")
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (kb_common.KB_DOCUMENT_LOCK_CLASS, "beta-guide"),
                )
                holding.set()
                for _ in range(400):  # until the load is queued behind us
                    row = conn.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND NOT granted AND classid = %s",
                        (kb_common.KB_DOCUMENT_LOCK_CLASS,),
                    ).fetchone()
                    if row and int(row[0]) >= 1:
                        break
                    time.sleep(0.05)
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (kb_common.KB_DOCUMENT_LOCK_CLASS, "alpha-guide"),
                )
                raw.append("acquired")
        except psycopg.errors.DeadlockDetected:
            raw.append("deadlock victim")
        except psycopg.Error as exc:
            raw.append(f"error {exc.sqlstate}")
        finally:
            holding.set()

    writer = threading.Thread(target=raw_writer)
    writer.start()
    try:
        assert holding.wait(10)
        started = time.monotonic()
        outcome = _go(_load_args(digest), scratch.loader_dsn, root)
        elapsed = time.monotonic() - started
    finally:
        writer.join(40)
    assert not writer.is_alive()  # nothing hangs
    assert raw != [] and elapsed < 20
    assert outcome in (kb_load.FAILED_LOCK_BUSY, kb_load.FAILED_DEADLOCK), (outcome, raw)
    assert scratch.counts() == (0, 0, 0)
