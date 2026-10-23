"""`load` and `approve` against a fake connection (ADR 0024 A3): statement order, transaction
boundaries, every exit status, the privilege refusals before any write, and commit-first output.
Offline: locks, triggers and SQLSTATE values on a real server are covered only by the live tests.
Mutations run on COPIES of the module source, never on `src`."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr
from tests.safety.kb_corpus import doc_spec, sha, write_corpus
from tests.safety.kb_fakes import (
    STORED_HASH,
    LoadFakeConnection,
    Out,
    mutated_module,
)

from divesafe.config.loader_settings import LoaderSettings
from divesafe.services import kb_common, kb_load
from divesafe.services.kb_load import Outcome

pytestmark = pytest.mark.safety

DSN = "postgresql://u:never-echoed@127.0.0.1:1/x_test"
CAUGHT = (AssertionError, Exception)


def db_error(code: str | None, message: str = "refused") -> psycopg.Error:
    return type("Refusal", (psycopg.DatabaseError,), {"sqlstate": code})(message)


class Env:
    """One corpus on disk plus the settings that point at it."""

    def __init__(self, root: Path, environment: str = "test") -> None:
        self.root = root
        self.environment = environment
        self.manifest_hash = ""

    def corpus(self, specs: list[dict[str, Any]] | None = None) -> str:
        self.manifest_hash = write_corpus(self.root, specs)
        return self.manifest_hash

    def settings(self, *, target: bool = True) -> LoaderSettings:
        return LoaderSettings(
            environment=self.environment,  # type: ignore[arg-type]
            loader_database_url=SecretStr(DSN),
            corpus_target=self.root if target else None,
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path / "corpus")


Runner = Callable[..., Any]


def go(
    monkeypatch: pytest.MonkeyPatch,
    fake: LoadFakeConnection,
    args: list[str],
    settings: LoaderSettings,
    module: Any = None,
) -> Outcome:
    monkeypatch.setattr(kb_common, "connect", lambda dsn: fake)
    monkeypatch.setattr(sys, "stdout", Out(fake))
    target = module or kb_load
    request = target.parse_arguments(args)
    result = target.run(request, settings)
    return Outcome(result.status, result.message)  # a mutant has its own Outcome class


def load_args(env: Env) -> list[str]:
    return ["load", "--approved-manifest-sha256", env.manifest_hash]


def approve_args(supplied: str = STORED_HASH) -> list[str]:
    return ["approve", "site-guide", "1", "--approved-manifest-sha256", supplied]


# --- load: statement order and transaction boundaries --------------------------------------------


SEARCH_PATH = 'SET LOCAL search_path = "pg_catalog", "pg_temp"'  # quoted: not the self-check's text


def check_load_order(fake: LoadFakeConnection) -> None:
    iso = fake.position("ISOLATION LEVEL READ COMMITTED")
    path = fake.position(SEARCH_PATH)
    lock_t = fake.position("SET LOCAL lock_timeout = 10000")
    stmt_t = fake.position("SET LOCAL statement_timeout = 30000", lock_t)
    idle_t = fake.position("SET LOCAL idle_in_transaction_session_timeout = 30000")
    glob = fake.position("(1220002, 0)")
    doc_lock = fake.position("(1220001, 'site-guide')")
    read_t = fake.position("SET LOCAL statement_timeout = 10000")
    sites = fake.position("FROM public.kb_documents d")
    restored = fake.position("SET LOCAL statement_timeout = 30000", read_t)
    ins_doc = fake.position("INSERT INTO public.kb_documents")
    ins_chunk = fake.position("INSERT INTO public.kb_chunks")
    ins_draft = fake.position("'DRAFT', false")
    commit = fake.position("COMMIT", ins_draft)
    assert iso < path < lock_t < stmt_t < idle_t < glob < doc_lock < read_t < sites < restored
    assert restored < ins_doc < ins_chunk < ins_draft < commit
    txns = {fake.log[i][0] for i in (iso, glob, doc_lock, sites, ins_doc, ins_chunk, ins_draft)}
    assert len(txns) == 1  # documents, chunks and DRAFT in ONE transaction with the locks
    assert fake.log[commit][1] == "COMMIT"


def test_load_statement_order_and_one_write_transaction(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection()
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome == Outcome(0, "loaded as DRAFT")
    check_load_order(fake)
    assert fake.committed_inserts == 4  # one document, two chunks, one DRAFT event
    assert fake.out_lines() == []  # load prints nothing but its final line


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "    _set_local(\n"
            "        conn,\n"
            '        "idle_in_transaction_session_timeout",\n'
            "        require_limit(kb_common.LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS),\n"
            "    )\n",
            "",
        ),
        ('sql.Identifier("pg_temp")', 'sql.Identifier("public")'),
        ('sql.Identifier("pg_catalog"), sql.Identifier("pg_temp")', 'sql.Identifier("pg_temp")'),
    ],
    ids=["no-idle-timeout", "public-not-pg_temp", "no-pg_catalog"],
)
def test_mutation_in_begin_is_caught_by_the_load_statement_order(
    old: str, new: str, monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    mutant = mutated_module(kb_load, (old, new))
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings(), mutant)
    with pytest.raises(AssertionError):
        check_load_order(fake)


def test_the_global_lock_is_taken_before_any_per_document_lock(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus([doc_spec("beta-guide"), doc_spec("alpha-guide")])
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings())
    locks = [t for _, t in fake.log if "pg_advisory_xact_lock" in t]
    assert locks[0].endswith("(1220002, 0)")
    assert [t.rsplit("-- ", 1)[1] for t in locks[1:]] == [
        "(1220001, 'alpha-guide')",
        "(1220001, 'beta-guide')",
    ]  # sorted by document id, whatever the manifest order


def test_documents_are_written_in_sorted_order_with_their_own_chunks(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus([doc_spec("beta-guide"), doc_spec("alpha-guide")])
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings())
    inserts = [t for _, t in fake.log if "INSERT INTO public.kb_documents" in t]
    assert "'alpha-guide'" in inserts[0] and "'beta-guide'" in inserts[1]


def test_the_draft_event_never_claims_a_hash_check(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings())
    drafts = [t for _, t in fake.log if "INSERT INTO public.kb_status_events" in t]
    assert drafts and all("'DRAFT', false)" in t for t in drafts)


def test_the_manifest_hash_is_stored_with_every_document(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings())
    insert = next(t for _, t in fake.log if "INSERT INTO public.kb_documents" in t)
    assert env.manifest_hash in insert


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            '    conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")\n',
            "",
        ),
        (
            '    _set_local(conn, "lock_timeout", require_limit(kb_common.LOAD_LOCK_TIMEOUT_MS))\n',
            "",
        ),
        (
            "kb_common.KB_LOAD_GLOBAL_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT",
            "kb_common.KB_DOCUMENT_LOCK_CLASS, kb_common.KB_LOAD_GLOBAL_LOCK_OBJECT",
        ),
        (
            "    docs = sorted(corpus.documents, key=lambda d: d.entry.document_id)",
            "    docs = list(reversed(sorted(corpus.documents, key=lambda d: d.entry.document_id)))",  # noqa: E501
        ),
        (
            '    _set_local(conn, "statement_timeout", require_limit(kb_common.LOAD_READ_STATEMENT_TIMEOUT_MS))\n',  # noqa: E501
            "",
        ),
        (
            '    _site_chunk_check(conn, corpus)\n    _set_local(conn, "statement_timeout", require_limit(kb_common.LOAD_STATEMENT_TIMEOUT_MS))\n',  # noqa: E501
            "    _site_chunk_check(conn, corpus)\n",
        ),
    ],
)
def test_mutation_a_changed_load_sequence_is_caught(
    old: str, new: str, monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus([doc_spec("beta-guide"), doc_spec("alpha-guide")])
    mutant = mutated_module(kb_load, (old, new))
    fake = LoadFakeConnection()
    try:
        go(monkeypatch, fake, load_args(env), env.settings(), mutant)
    except Exception:  # noqa: BLE001 - a crashing mutant is also caught
        return
    with pytest.raises(CAUGHT):
        check_load_order_two(fake)


def check_load_order_two(fake: LoadFakeConnection) -> None:
    iso = fake.position("ISOLATION LEVEL READ COMMITTED")
    lock_t = fake.position("SET LOCAL lock_timeout = 10000")
    glob = fake.position("(1220002, 0)")
    first = fake.position("(1220001, 'alpha-guide')")
    second = fake.position("(1220001, 'beta-guide')")
    read_t = fake.position("SET LOCAL statement_timeout = 10000")
    restored = fake.position("SET LOCAL statement_timeout = 30000", read_t)
    ins = fake.position("INSERT INTO public.kb_documents")
    assert iso < lock_t < glob < first < second < read_t < restored < ins


def test_the_ordering_check_passes_on_the_real_module(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus([doc_spec("beta-guide"), doc_spec("alpha-guide")])
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings())
    check_load_order_two(fake)


# --- self-checks: their own transactions, before any write ---------------------------------------


def test_the_self_checks_run_in_their_own_transactions_before_the_write_transaction(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings())
    role = fake.position("has_table_privilege")
    problems = fake.position("session_replication_role")
    begin = fake.position("ISOLATION LEVEL READ COMMITTED")
    first_write = fake.position("INSERT INTO public.kb_")
    assert role < problems < begin < first_write
    assert len({fake.log[role][0], fake.log[problems][0], fake.log[begin][0]}) == 3
    ends = [i for i, (_, t) in enumerate(fake.log) if t in ("ROLLBACK", "COMMIT")]
    assert role < ends[0] < problems < ends[1] < begin  # each check ended its own transaction
    assert fake.log[ends[0]][1] == fake.log[ends[1]][1] == "ROLLBACK"


def test_the_self_checks_use_the_approved_15_second_statement_timeout(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection()
    go(monkeypatch, fake, load_args(env), env.settings())
    before_write = fake.log[: fake.position("ISOLATION LEVEL READ COMMITTED")]
    assert sum("statement_timeout = 15000" in t for _, t in before_write) == 2


REFUSALS: list[tuple[str, dict[str, Any], int]] = [
    ("the app or owner role", {"app_privileged": True}, 5),
    ("an unknown app-privilege answer", {"app_privileged": None}, 5),
    ("an over-privileged loader", {"selfcheck_flags": {0: True}}, 19),
    ("a self-check that could not finish", {"selfcheck_error": db_error("57014")}, 20),
    (
        "a lost connection in the role check",
        {"role_check_error": psycopg.OperationalError("lost")},
        4,
    ),
]


@pytest.mark.parametrize(("label", "options", "status"), REFUSALS, ids=[r[0] for r in REFUSALS])
@pytest.mark.parametrize("command", ["load", "approve"])
def test_a_privilege_problem_refuses_before_any_write_or_lock(
    label: str,
    options: dict[str, Any],
    status: int,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    env: Env,
) -> None:
    env.corpus()
    fake = LoadFakeConnection(**options)
    args = load_args(env) if command == "load" else approve_args()
    outcome = go(monkeypatch, fake, args, env.settings())
    assert outcome.status == status, label
    assert not fake.writes_sent()
    assert not fake.has("pg_advisory_xact_lock")
    assert not fake.has("ISOLATION LEVEL")
    assert not fake.committed()
    assert fake.closed


def test_the_app_or_owner_refusal_wins_over_the_over_privileged_one(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(app_privileged=True, selfcheck_flags={0: True})
    assert go(monkeypatch, fake, load_args(env), env.settings()).status == 5
    assert not fake.has("session_replication_role")  # the second check never ran


@pytest.mark.parametrize(
    ("old", "new", "options"),
    [
        (
            "        if kb_privileges.running_with_app_or_owner_privileges(conn, statement_timeout_ms=limit):\n            return FAILED_ROLE\n",  # noqa: E501
            "",
            {"app_privileged": True},
        ),
        (
            "        if kb_privileges.loader_privilege_problems(conn, statement_timeout_ms=limit):\n            return FAILED_SELF_CHECK\n",  # noqa: E501
            "",
            {"selfcheck_flags": {0: True}},
        ),
        (
            "    except kb_privileges.SelfCheckIncomplete:\n        return FAILED_SELF_CHECK_INCOMPLETE\n",  # noqa: E501
            "    except kb_privileges.SelfCheckIncomplete:\n        return None\n",
            {"selfcheck_error": db_error("57014")},
        ),
    ],
)
def test_mutation_a_removed_privilege_refusal_lets_the_write_through(
    old: str,
    new: str,
    options: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    env: Env,
) -> None:
    env.corpus()
    mutant = mutated_module(kb_load, (old, new))
    fake = LoadFakeConnection(**options)
    go(monkeypatch, fake, load_args(env), env.settings(), mutant)
    assert fake.writes_sent()  # the mutant writes ...
    real = LoadFakeConnection(**options)
    go(monkeypatch, real, load_args(env), env.settings())
    assert not real.writes_sent()  # ... and the real module does not


def test_mutation_the_role_check_moved_after_the_write_is_caught(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    mutant = mutated_module(
        kb_load,
        (
            "    early = _self_check(conn)\n    if early is not None:\n        return early\n    sent = False",  # noqa: E501
            "    early = None\n    sent = False",
        ),
        ("        deadline.check()\n        _begin(conn)", "        _begin(conn)"),
    )
    fake = LoadFakeConnection(app_privileged=True)
    go(monkeypatch, fake, load_args(env), env.settings(), mutant)
    assert fake.writes_sent() and fake.committed()


# --- zero chunks and the corpus gate, before any connection --------------------------------------


def test_a_zero_chunk_document_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus([doc_spec("alpha-guide"), doc_spec("beta-guide", chunks=(), gap="Heading only\n")])
    called: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: called.append(dsn))
    request = kb_load.parse_arguments(load_args(env))
    result = kb_load.run(request, env.settings())
    assert result.status == 16
    assert called == []


def test_a_manifest_with_no_chunks_at_all_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus([doc_spec("alpha-guide", chunks=(), gap="Heading only\n")])
    called: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: called.append(dsn))
    result = kb_load.run(kb_load.parse_arguments(load_args(env)), env.settings())
    assert result.status == 16
    assert called == []


def test_mutation_checking_only_the_total_lets_a_zero_chunk_document_through(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    from divesafe.services import kb_manifest

    env.corpus([doc_spec("alpha-guide"), doc_spec("beta-guide", chunks=(), gap="Heading only\n")])
    mutant = mutated_module(
        kb_manifest, ("if total == 0 or any(not d.chunks for d in docs):", "if total == 0:")
    )
    monkeypatch.setattr(kb_load, "_read", mutant.read_corpus)
    fake = LoadFakeConnection()
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == 0  # the mutant loads an unapprovable version
    monkeypatch.undo()
    real = go_real_refusal(monkeypatch, env)
    assert real == 16


def go_real_refusal(monkeypatch: pytest.MonkeyPatch, env: Env) -> int:
    fake = LoadFakeConnection()
    return go(monkeypatch, fake, load_args(env), env.settings()).status


def test_an_invalid_corpus_never_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    (env.root / "site-guide-v1.txt").write_bytes(b"tampered\n")
    called: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: called.append(dsn))
    result = kb_load.run(kb_load.parse_arguments(load_args(env)), env.settings())
    assert result.status == 22
    assert called == []


def test_a_supplied_hash_that_is_not_the_manifest_hash_is_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    called: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: called.append(dsn))
    args = ["load", "--approved-manifest-sha256", sha(b"another manifest")]
    result = kb_load.run(kb_load.parse_arguments(args), env.settings())
    assert result.status == 14
    assert called == []


def test_mutation_accepting_a_mismatched_manifest_hash_is_caught(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    mutant = mutated_module(
        kb_load,
        (
            "if checked and corpus.manifest_sha256 != request.supplied_hash:",
            "if False:",
        ),
    )
    fake = LoadFakeConnection()
    args = ["load", "--approved-manifest-sha256", sha(b"another manifest")]
    assert go(monkeypatch, fake, args, env.settings(), mutant).status == 0
    real = LoadFakeConnection()
    assert go(monkeypatch, real, args, env.settings()).status == 14
    assert not real.writes_sent()


def test_missing_hash_or_flag_in_production_is_refused_with_15(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    env.environment = "production"
    called: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: called.append(dsn))
    for args in (["load"], ["load", "--development-no-manifest-hash"]):
        result = kb_load.run(kb_load.parse_arguments(args), env.settings())
        assert result.status == 15
    assert called == []


def test_the_development_flag_loads_without_a_hash_outside_production(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection()
    outcome = go(monkeypatch, fake, ["load", "--development-no-manifest-hash"], env.settings())
    assert outcome.status == 0


def test_a_missing_corpus_target_is_a_configuration_refusal(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    called: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: called.append(dsn))
    for args in (load_args(env), approve_args()):
        result = kb_load.run(kb_load.parse_arguments(args), env.settings(target=False))
        assert result.status == 3
    assert called == []


# --- failures: no retry, nothing hidden ----------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status"),
    [("55P03", 6), ("57014", 7), ("40P01", 9), ("25001", 8), ("23505", 11), ("42501", 11)],
)
@pytest.mark.parametrize("at", ["pg_advisory_xact_lock(%s, %s)", "INSERT INTO public.kb_documents"])
def test_sqlstates_map_to_their_exit_and_are_never_retried(
    code: str,
    status: int,
    at: str,
    monkeypatch: pytest.MonkeyPatch,
    env: Env,
) -> None:
    env.corpus()
    fake = LoadFakeConnection(fail_on={at: db_error(code)})
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == status  # a classed SQLSTATE is a definite failure, sent or not
    assert sum(at in t for _, t in fake.log) == 1  # sent once: no retry
    assert not fake.committed()
    assert fake.log[-1][1] == "ROLLBACK"


def test_a_lock_failure_is_not_retried_and_the_run_stops_there(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(fail_on={"(%s, hashtext(%s))": db_error("55P03")})
    assert go(monkeypatch, fake, load_args(env), env.settings()).status == 6
    assert sum("hashtext" in t for _, t in fake.log) == 1
    assert not fake.writes_sent()


@pytest.mark.parametrize("code", [None, "08006", "57P01", "08000"])
def test_an_uncertain_error_after_the_first_send_is_outcome_unknown(
    code: str | None, monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(fail_on={"INSERT INTO public.kb_chunks": db_error(code)})
    assert go(monkeypatch, fake, load_args(env), env.settings()).status == 12
    assert not fake.committed()


def test_an_uncertain_error_before_the_first_send_is_not_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(fail_on={"SET LOCAL lock_timeout": db_error("08006")})
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == 4  # nothing was sent: a plain connection failure
    assert not fake.writes_sent()


@pytest.mark.parametrize("code", [None, "08006", "57P01"])
@pytest.mark.parametrize(
    "at",
    [
        "SET TRANSACTION ISOLATION LEVEL",
        SEARCH_PATH,
        "SET LOCAL idle_in_transaction_session_timeout",
        "pg_advisory_xact_lock(%s, %s)",
        "(%s, hashtext(%s))",
    ],
)
def test_every_uncertain_error_before_the_first_insert_is_a_connection_failure(
    code: str | None, at: str, monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(fail_on={at: db_error(code)})
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == 4
    assert not fake.writes_sent()
    assert not fake.committed()


def test_a_non_database_error_before_the_first_send_is_a_plain_failure(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(fail_on={"SET LOCAL lock_timeout": RuntimeError("boom")})
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == 11
    assert "boom" not in outcome.message
    assert not fake.writes_sent()


def test_mutation_always_outcome_unknown_or_never_is_caught(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    """Mutants: `sent` starts True (pre-send looks post-send) and `sent` never set."""
    env.corpus()
    always = mutated_module(kb_load, ("    sent = False\n", "    sent = True\n"))
    never = mutated_module(kb_load, ("        sent = True\n", "        sent = False\n"))
    pre = {"SET LOCAL lock_timeout": db_error("08006")}
    post = {"INSERT INTO public.kb_chunks": db_error("08006")}
    results = []
    for module, fail in ((None, pre), (always, pre), (None, post), (never, post)):
        fake = LoadFakeConnection(fail_on=fail)
        results.append(go(monkeypatch, fake, load_args(env), env.settings(), module).status)
    # real pre-send 4, mutant pre-send 12; real post-send 12, mutant post-send 4
    assert results == [4, 12, 12, 4]


def test_a_commit_failure_is_outcome_unknown(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    env.corpus()
    fake = LoadFakeConnection(commit_error=psycopg.OperationalError("gone"))
    assert go(monkeypatch, fake, load_args(env), env.settings()).status == 12


def test_a_non_database_error_after_a_send_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(fail_on={"INSERT INTO public.kb_chunks": RuntimeError("boom")})
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == 12
    assert "boom" not in outcome.message


def test_the_connection_is_always_closed(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    env.corpus()
    for options in ({}, {"app_privileged": True}, {"fail_on": {"INSERT": db_error("23505")}}):
        fake = LoadFakeConnection(**options)
        go(monkeypatch, fake, load_args(env), env.settings())
        assert fake.closed


def test_a_failed_connection_is_exit_4(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    env.corpus()

    def refuse(dsn: str) -> Any:
        raise psycopg.OperationalError("password=never-echoed host=db")

    monkeypatch.setattr(kb_common, "connect", refuse)
    result = kb_load.run(kb_load.parse_arguments(load_args(env)), env.settings())
    assert result == Outcome(4, "failed: database connection; nothing written")
    assert "never-echoed" not in result.message


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            '    "55P03": FAILED_LOCK_BUSY,  # never retried (Q9)',
            '    "55P03": FAILED_OTHER,  # never retried (Q9)',
        ),
        (
            "        if _uncertain(exc):"
            "  # before the first INSERT: a lost connection wrote nothing\n"
            "            return OUTCOME_UNKNOWN if sent else FAILED_CONNECTION\n",
            "",
        ),
        (
            "            return OUTCOME_UNKNOWN if sent else FAILED_CONNECTION",
            "            return FAILED_CONNECTION",
        ),
        (
            "        return OUTCOME_UNKNOWN if sent else FAILED_OTHER",
            "        return FAILED_OTHER",
        ),
        (
            "        conn.commit()\n    except Exception:  # noqa: BLE001\n        return OUTCOME_UNKNOWN",  # noqa: E501
            "        conn.commit()\n    except Exception:  # noqa: BLE001\n        return FAILED_OTHER",  # noqa: E501
        ),
    ],
)
def test_mutation_a_failure_mapped_to_a_definite_outcome_is_caught(
    old: str, new: str, monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    mutant = mutated_module(kb_load, (old, new))
    scenarios = [
        {"fail_on": {"pg_advisory_xact_lock(%s, %s)": db_error("55P03")}},
        {"fail_on": {"INSERT INTO public.kb_chunks": db_error(None)}},
        {"fail_on": {"INSERT INTO public.kb_chunks": RuntimeError("x")}},
        {"fail_on": {"SET LOCAL lock_timeout": db_error("08006")}},
        {"fail_on": {"SET LOCAL lock_timeout": RuntimeError("x")}},
        {"commit_error": psycopg.OperationalError("gone")},
    ]
    differs = 0
    for options in scenarios:
        real = LoadFakeConnection(**options)
        mutated = LoadFakeConnection(**options)
        a = go(monkeypatch, real, load_args(env), env.settings()).status
        b = go(monkeypatch, mutated, load_args(env), env.settings(), mutant).status
        differs += a != b
    assert differs >= 1


# --- the deadline --------------------------------------------------------------------------------


class Ticker:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_deadline_is_checked_between_statements_and_gives_18(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    clock = Ticker()
    monkeypatch.setattr(
        kb_load,
        "Deadline",
        lambda seconds: kb_common.Deadline(seconds, clock),
    )

    def advance(text: str) -> None:
        if "INSERT INTO public.kb_documents" in text:
            clock.now += 10_000.0

    fake = LoadFakeConnection(on_statement=advance)
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome == Outcome(18, "failed: overall deadline exceeded; nothing written")
    assert not fake.committed()
    assert not fake.has("INSERT INTO public.kb_chunks")  # stopped between statements
    assert fake.log[-1][1] == "ROLLBACK"


def test_the_deadline_is_the_approved_120_seconds_for_load_and_60_for_approve(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    seen: list[float] = []

    def spy(seconds: float) -> kb_common.Deadline:
        seen.append(seconds)
        return kb_common.Deadline(seconds)

    monkeypatch.setattr(kb_load, "Deadline", spy)
    go(monkeypatch, LoadFakeConnection(), load_args(env), env.settings())
    go(monkeypatch, LoadFakeConnection(), approve_args(), env.settings())
    assert seen == [120.0, 60.0]


def test_the_deadline_starts_before_the_connection(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    order: list[str] = []
    monkeypatch.setattr(
        kb_load, "Deadline", lambda seconds: order.append("deadline") or kb_common.Deadline(seconds)
    )
    fake = LoadFakeConnection()

    def connect(dsn: str) -> LoadFakeConnection:
        order.append("connect")
        return fake

    monkeypatch.setattr(kb_common, "connect", connect)
    monkeypatch.setattr(sys, "stdout", Out(fake))
    kb_load.run(kb_load.parse_arguments(load_args(env)), env.settings())
    assert order == ["deadline", "connect"]


DEADLINE_MUTATIONS = [
    # (old, new, the statement after which the clock jumps, what must not be sent after it)
    (
        "    deadline.check()\n    on_send()  # the first INSERT follows: from here a failure may"
        " have written something\n",
        "    on_send()  # the first INSERT follows:"
        " from here a failure may have written something\n",
        "RESTORED",
        "INSERT INTO public.kb_documents",
    ),
    (
        "                (e.document_id, e.version, c.chunk, c.section, c.text, c.sha256),\n"
        "            )\n            deadline.check()",
        "                (e.document_id, e.version, c.chunk, c.section, c.text, c.sha256),\n"
        "            )",
        "INSERT INTO public.kb_chunks",
        "INSERT INTO public.kb_chunks",
    ),
]


@pytest.mark.parametrize(
    ("old", "new", "trigger", "forbidden"),
    DEADLINE_MUTATIONS,
    ids=["before-documents", "after-each-chunk"],
)
def test_mutation_a_removed_deadline_check_is_caught(
    old: str,
    new: str,
    trigger: str,
    forbidden: str,
    monkeypatch: pytest.MonkeyPatch,
    env: Env,
) -> None:
    env.corpus()
    mutant = mutated_module(kb_load, (old, new))
    sent: list[int] = []
    for module in (kb_load, mutant):
        clock = Ticker()
        monkeypatch.setattr(module, "Deadline", lambda s, c=clock: kb_common.Deadline(s, c))
        seen_read = [False]

        def advance(text: str, c: Ticker = clock, seen: list[bool] = seen_read) -> None:
            if text == "SET LOCAL statement_timeout = 10000":
                seen[0] = True
            hit = (
                trigger == "RESTORED" and seen[0] and text == "SET LOCAL statement_timeout = 30000"
            ) or (trigger != "RESTORED" and trigger in text)
            if hit:
                c.now += 10_000.0

        fake = LoadFakeConnection(on_statement=advance)
        go(monkeypatch, fake, load_args(env), env.settings(), module)
        sent.append(sum(forbidden in t for _, t in fake.log))
    real_count, mutant_count = sent
    assert mutant_count > real_count
    assert real_count == (0 if forbidden.endswith("documents") else 1)


# --- approve -------------------------------------------------------------------------------------


def test_approve_writes_approved_with_a_checked_hash_then_commits_then_prints(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    fake = LoadFakeConnection()
    outcome = go(monkeypatch, fake, approve_args(), env.settings())
    assert outcome == Outcome(0, "approved")
    insert = fake.position("INSERT INTO public.kb_status_events")
    commit = fake.position("COMMIT", insert)
    printed = fake.position("OUT:recorded_at=")
    assert insert < commit < printed
    assert "'APPROVED', %s) RETURNING recorded_at -- ('site-guide', 1, True)" in fake.log[insert][1]
    assert fake.out_lines() == ["recorded_at=2026-10-23T01:02:03+00:00"]


def test_approve_statement_order(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    fake = LoadFakeConnection()
    go(monkeypatch, fake, approve_args(), env.settings())
    iso = fake.position("ISOLATION LEVEL READ COMMITTED")
    glob = fake.position("(1220002, 0)")
    doc = fake.position("(1220001, 'site-guide')")
    stored = fake.position("SELECT manifest_sha256")
    status = fake.position("SELECT status FROM public.kb_status_events")
    insert = fake.position("INSERT INTO public.kb_status_events")
    assert iso < glob < doc < stored < status < insert
    assert len({fake.log[i][0] for i in (iso, glob, doc, stored, status, insert)}) == 1
    assert not fake.has("INSERT INTO public.kb_documents")
    assert not fake.has("INSERT INTO public.kb_chunks")


def test_approve_with_a_hash_that_is_not_the_stored_one_is_refused_with_14(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    fake = LoadFakeConnection()
    outcome = go(monkeypatch, fake, approve_args("c" * 64), env.settings())
    assert outcome.status == 14
    assert not fake.has("INSERT INTO public.kb_status_events")
    assert fake.out_lines() == []
    assert fake.log[-1][1] == "ROLLBACK"


@pytest.mark.parametrize("status", ["APPROVED", "WITHDRAWN", "REJECTED", "draft", ""])
def test_approve_needs_the_latest_status_to_be_exactly_draft(
    status: str, monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    fake = LoadFakeConnection(latest_status=status)
    assert go(monkeypatch, fake, approve_args(), env.settings()).status == 21
    assert not fake.has("'APPROVED'")
    assert fake.out_lines() == []


def test_approve_of_a_version_with_no_events_is_refused_with_21(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    fake = LoadFakeConnection(latest_status=None)
    assert go(monkeypatch, fake, approve_args(), env.settings()).status == 21


def test_approve_of_a_missing_version_is_refused_with_10(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    fake = LoadFakeConnection(doc_exists=False)
    assert go(monkeypatch, fake, approve_args(), env.settings()).status == 10
    assert not fake.has("SELECT status FROM")
    assert fake.out_lines() == []


def test_approve_does_not_read_the_corpus(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    called: list[Path] = []
    monkeypatch.setattr(kb_load, "_read", lambda target: called.append(target))
    go(monkeypatch, LoadFakeConnection(), approve_args(), env.settings())
    assert called == []


def test_approve_in_development_without_a_hash_records_an_unchecked_approval(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    fake = LoadFakeConnection(stored_hash="d" * 64)
    args = ["approve", "site-guide", "1", "--development-no-manifest-hash"]
    assert go(monkeypatch, fake, args, env.settings()).status == 0
    insert = next(t for _, t in fake.log if "'APPROVED', %s)" in t)
    assert insert.endswith("('site-guide', 1, False)")


def test_approve_in_production_without_a_hash_is_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.environment = "production"
    called: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: called.append(dsn))
    for args in (
        ["approve", "site-guide", "1"],
        ["approve", "site-guide", "1", "--development-no-manifest-hash"],
    ):
        assert kb_load.run(kb_load.parse_arguments(args), env.settings()).status == 15
    assert called == []


def test_an_approve_that_cannot_commit_prints_nothing_and_is_unknown(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    fake = LoadFakeConnection(commit_error=psycopg.OperationalError("gone"))
    assert go(monkeypatch, fake, approve_args(), env.settings()).status == 12
    assert fake.out_lines() == []


def test_a_broken_stdout_after_the_approve_commit_does_not_change_the_committed_outcome(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    class Broken:
        def write(self, text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    fake = LoadFakeConnection()
    monkeypatch.setattr(kb_common, "connect", lambda dsn: fake)
    monkeypatch.setattr(sys, "stdout", Broken())
    result = kb_load.run(kb_load.parse_arguments(approve_args()), env.settings())
    assert result == Outcome(0, "approved")  # run() itself; the write is suppressed
    assert fake.committed()


@pytest.mark.parametrize(
    ("old", "new", "args", "options"),
    [
        (
            "    if checked and row[0] != request.supplied_hash:\n        raise _HashMismatch\n",
            "",
            approve_args("c" * 64),
            {},
        ),
        (
            '    if status is None or status[0] != "DRAFT":',
            "    if False:",
            approve_args(),
            {"latest_status": "APPROVED"},
        ),
        (
            "    if row is None:\n        raise _NotFound",
            "    if False:\n        raise _NotFound",
            approve_args(),
            {"doc_exists": False},
        ),
    ],
)
def test_mutation_a_removed_approve_guard_writes_an_approval(
    old: str,
    new: str,
    args: list[str],
    options: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    env: Env,
) -> None:
    mutant = mutated_module(kb_load, (old, new))
    mutated = LoadFakeConnection(**options)
    after = go(monkeypatch, mutated, args, env.settings(), mutant)
    real = LoadFakeConnection(**options)
    before = go(monkeypatch, real, args, env.settings())
    assert before.status != 0
    assert not real.has("'APPROVED', %s)")
    # the mutant either approves, or ends differently (a crash after the send is 12)
    assert mutated.has("'APPROVED', %s)") or after.status != before.status


def test_mutation_a_hash_checked_flag_that_is_always_true_is_caught(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    mutant = mutated_module(
        kb_load,
        (
            "        (request.document_id, request.version, checked),",
            "        (request.document_id, request.version, True),",
        ),
    )
    args = ["approve", "site-guide", "1", "--development-no-manifest-hash"]
    real = LoadFakeConnection()
    go(monkeypatch, real, args, env.settings())
    bad = LoadFakeConnection()
    go(monkeypatch, bad, args, env.settings(), mutant)
    real_insert = next(t for _, t in real.log if "'APPROVED', %s)" in t)
    bad_insert = next(t for _, t in bad.log if "'APPROVED', %s)" in t)
    assert real_insert.endswith("False)")
    assert bad_insert.endswith("True)")


def test_mutation_printing_before_the_commit_is_caught(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    mutant = mutated_module(
        kb_load,
        (
            '        conn.commit()\n    except Exception:  # noqa: BLE001\n        return OUTCOME_UNKNOWN\n    if recorded is not None:\n        with contextlib.suppress(OSError):\n            _emit(sys.stdout, f"recorded_at={recorded.isoformat()}")\n',  # noqa: E501
            '        if recorded is not None:\n            _emit(sys.stdout, f"recorded_at={recorded.isoformat()}")\n        conn.commit()\n    except Exception:  # noqa: BLE001\n        return OUTCOME_UNKNOWN\n',  # noqa: E501
        ),
    )
    fake = LoadFakeConnection(commit_error=psycopg.OperationalError("gone"))
    go(monkeypatch, fake, approve_args(), env.settings(), mutant)
    assert fake.out_lines() != []  # the mutant printed a time for an approval that did not commit
    real = LoadFakeConnection(commit_error=psycopg.OperationalError("gone"))
    go(monkeypatch, real, approve_args(), env.settings())
    assert real.out_lines() == []


# --- the per-site chunk bound --------------------------------------------------------------------


def row(
    document_id: str,
    count: int,
    *,
    site: str | None = None,
    all_sites: bool = True,
    approved: bool = True,
    withdrawn: bool = False,
    version: int = 1,
) -> tuple[Any, ...]:
    return (document_id, version, site, all_sites, count, approved, withdrawn)


def two_chunks(name: str) -> dict[str, Any]:
    return doc_spec(name)


def test_the_per_site_bound_allows_exactly_twenty_chunks(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()  # 2 new chunks
    fake = LoadFakeConnection(site_rows=[row("other-guide", 18)])
    assert go(monkeypatch, fake, load_args(env), env.settings()).status == 0


def test_the_per_site_bound_refuses_twenty_one_chunks_with_17_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    fake = LoadFakeConnection(site_rows=[row("other-guide", 19)])
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == 17
    assert not fake.writes_sent()
    assert not fake.committed()


def test_the_bound_takes_the_largest_version_of_each_document_not_the_sum(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    rows = [row("other-guide", 10, version=1), row("other-guide", 18, version=2)]
    fake = LoadFakeConnection(site_rows=rows)
    assert go(monkeypatch, fake, load_args(env), env.settings()).status == 0  # 18 + 2
    fake = LoadFakeConnection(site_rows=rows + [row("third-guide", 1)])
    assert go(monkeypatch, fake, load_args(env), env.settings()).status == 17


def test_a_version_withdrawn_without_approval_does_not_count(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    env.corpus()
    skipped = row("other-guide", 50, approved=False, withdrawn=True)
    assert (
        go(
            monkeypatch, LoadFakeConnection(site_rows=[skipped]), load_args(env), env.settings()
        ).status
        == 0
    )
    counted = row("other-guide", 50, approved=True, withdrawn=True)
    assert (
        go(
            monkeypatch, LoadFakeConnection(site_rows=[counted]), load_args(env), env.settings()
        ).status
        == 17
    )


def test_a_site_specific_document_counts_for_its_own_site(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    from tests.safety.kb_corpus import SITE

    env.corpus()
    here = row("near-guide", 50, site=SITE, all_sites=False)
    outcome = go(monkeypatch, LoadFakeConnection(site_rows=[here]), load_args(env), env.settings())
    assert outcome.status == 17  # the new all-sites document also counts for this site


def test_the_new_documents_count_with_their_own_chunks(
    monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    big = tuple(f"Line {i}.\n" for i in range(21))
    env.corpus([doc_spec("big-guide", chunks=big)])
    fake = LoadFakeConnection()
    outcome = go(monkeypatch, fake, load_args(env), env.settings())
    assert outcome.status == 17
    assert not fake.writes_sent()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("        if sum(largest.values()) > kb_common.MAX_CHUNKS_PER_RUN:", "        if False:"),
        (
            "        if sum(largest.values()) > kb_common.MAX_CHUNKS_PER_RUN:",
            "        if sum(largest.values()) >= kb_common.MAX_CHUNKS_PER_RUN:",
        ),
        (
            "        if withdrawn and not approved:  # withdrawn without approval cannot govern\n            continue\n",  # noqa: E501
            "",
        ),
        (
            "            largest[document_id] = max(largest.get(document_id, 0), count)",
            "            largest[document_id] = largest.get(document_id, 0) + count",
        ),
    ],
)
def test_mutation_a_changed_per_site_bound_is_caught(
    old: str, new: str, monkeypatch: pytest.MonkeyPatch, env: Env
) -> None:
    mutant = mutated_module(kb_load, (old, new))
    scenarios = [
        [row("other-guide", 18)],  # exactly 20: allowed
        [row("other-guide", 19)],  # 21: refused
        [row("other-guide", 50, approved=False, withdrawn=True)],  # skipped
        [row("other-guide", 10, version=1), row("other-guide", 18, version=2)],  # max not sum
    ]
    env.corpus()
    differs = 0
    for rows in scenarios:
        a = go(monkeypatch, LoadFakeConnection(site_rows=rows), load_args(env), env.settings())
        b = go(
            monkeypatch, LoadFakeConnection(site_rows=rows), load_args(env), env.settings(), mutant
        )
        differs += a.status != b.status
    assert differs >= 1


# --- no echo -------------------------------------------------------------------------------------


def test_no_outcome_message_echoes_the_dsn_a_path_or_a_hash(
    monkeypatch: pytest.MonkeyPatch, env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.corpus()
    cases = [
        LoadFakeConnection(),
        LoadFakeConnection(app_privileged=True),
        LoadFakeConnection(
            fail_on={"INSERT INTO public.kb_chunks": db_error("23505", "never-echoed")}
        ),
        LoadFakeConnection(site_rows=[row("other-guide", 19)]),
    ]
    for fake in cases:
        outcome = go(monkeypatch, fake, load_args(env), env.settings())
        for secret in ("never-echoed", str(env.root), env.manifest_hash, "site-guide"):
            assert secret not in outcome.message
    seen = capsys.readouterr()
    assert "never-echoed" not in seen.out + seen.err
