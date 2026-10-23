"""Knowledge loader constants and argument grammar (ADR 0024 A3: approved limits, lock classes,
decision 6 grammar). Database-free. Mutations run on COPIES of the source, never on `src`."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from divesafe.services import kb_common, kb_withdraw
from divesafe.services import migrations as migrations_module
from divesafe.services import postgres as postgres_module
from divesafe.services.kb_common import (
    ArgumentRefused,
    LimitNotSet,
    parse_document_version,
    require_limit,
)

pytestmark = pytest.mark.safety

APPROVED = {
    "CONNECT_TIMEOUT_SECONDS": 5,
    "WITHDRAW_LOCK_TIMEOUT_MS": 10_000,
    "WITHDRAW_STATEMENT_TIMEOUT_MS": 30_000,
    "RECORDED_AT_READ_STATEMENT_TIMEOUT_MS": 10_000,
    "SELF_CHECK_STATEMENT_TIMEOUT_MS": 15_000,
}


# --- the approved limits -------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "value"), APPROVED.items())
def test_each_limit_equals_the_owner_approved_value(name: str, value: int) -> None:
    assert getattr(kb_common, name) == value


def check_lock_below_statement(lock_ms: int | None, statement_ms: int | None) -> None:
    assert lock_ms is not None and statement_ms is not None
    assert lock_ms < statement_ms, "the lock wait must end before the statement timeout"


def test_lock_timeout_is_strictly_below_statement_timeout() -> None:
    check_lock_below_statement(
        kb_common.WITHDRAW_LOCK_TIMEOUT_MS, kb_common.WITHDRAW_STATEMENT_TIMEOUT_MS
    )


@pytest.mark.parametrize(("lock_ms", "statement_ms"), [(30_000, 30_000), (30_000, 10_000)])
def test_mutation_equal_or_reversed_timeouts_are_caught(lock_ms: int, statement_ms: int) -> None:
    with pytest.raises(AssertionError):
        check_lock_below_statement(lock_ms, statement_ms)


@pytest.mark.parametrize("value", [None, 0, -1])
def test_require_limit_refuses_an_unset_or_non_positive_limit(value: int | None) -> None:
    with pytest.raises(LimitNotSet):
        require_limit(value)


def test_require_limit_returns_a_set_limit() -> None:
    assert require_limit(5) == 5


def test_only_the_approved_idle_and_load_timeouts_exist() -> None:
    """P1/P2 were approved 2026-10-23; any other idle or kb_load timeout name is not approved."""
    approved = {
        "WITHDRAW_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        "LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        "LOAD_LOCK_TIMEOUT_MS",
        "LOAD_STATEMENT_TIMEOUT_MS",
        "LOAD_SELF_CHECK_STATEMENT_TIMEOUT_MS",
        "LOAD_READ_STATEMENT_TIMEOUT_MS",
    }
    names = {n for n in vars(kb_common) if n.isupper() and ("IDLE" in n or "TIMEOUT" in n)}
    extra = names - approved - set(APPROVED) - {"CONNECT_TIMEOUT_SECONDS"}
    assert not extra


class _Conn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, query: Any, params: Any = None) -> Any:
        self.statements.append(query if isinstance(query, str) else query.as_string())
        return self


def test_connect_passes_connect_timeout_and_no_autocommit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_connect(dsn: str, **kwargs: Any) -> str:
        seen.update(kwargs, dsn=dsn)
        return "connection"

    monkeypatch.setattr(kb_common.psycopg, "connect", fake_connect)
    assert kb_common.connect("postgresql://never-used/x_test") == "connection"
    assert seen["connect_timeout"] == 5 and seen["autocommit"] is False


def test_connect_with_an_unset_timeout_refuses_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(kb_common.psycopg, "connect", never)
    monkeypatch.setattr(kb_common, "CONNECT_TIMEOUT_SECONDS", None)
    with pytest.raises(LimitNotSet):
        kb_common.connect("postgresql://never-used/x_test")


def test_the_generated_set_local_statements_are_exact() -> None:
    conn = _Conn()
    kb_withdraw._timeout(conn, "lock_timeout", kb_common.WITHDRAW_LOCK_TIMEOUT_MS or 0)  # type: ignore[arg-type]
    kb_withdraw._timeout(conn, "statement_timeout", kb_common.WITHDRAW_STATEMENT_TIMEOUT_MS or 0)  # type: ignore[arg-type]
    assert conn.statements == [
        "SET LOCAL lock_timeout = 10000",
        "SET LOCAL statement_timeout = 30000",
    ]


# --- lock classes --------------------------------------------------------------------------------


def test_the_global_lock_class_is_distinct_from_every_other_advisory_key() -> None:
    assert kb_common.KB_LOAD_GLOBAL_LOCK_CLASS == 1220002
    assert kb_common.KB_DOCUMENT_LOCK_CLASS == 1220001
    others = {
        kb_common.KB_DOCUMENT_LOCK_CLASS,
        migrations_module._LOCK_KEY,
        postgres_module._RUNS_LOCK_KEY,
    }
    assert kb_common.KB_LOAD_GLOBAL_LOCK_CLASS not in others


def withdraw_never_takes_the_global_lock(source: str) -> None:
    for forbidden in ("KB_LOAD_GLOBAL_LOCK", "1220002", "pg_advisory", "advisory"):
        assert forbidden not in source, forbidden


def test_withdraw_does_not_reference_the_global_lock() -> None:
    withdraw_never_takes_the_global_lock(inspect.getsource(kb_withdraw))


@pytest.mark.parametrize(
    "injection",
    [
        "kb_common.KB_LOAD_GLOBAL_LOCK_CLASS",
        "conn.execute('SELECT pg_advisory_xact_lock(1220002, 0)')",
    ],
)
def test_mutation_a_withdraw_that_takes_the_global_lock_is_caught(injection: str) -> None:
    mutated = inspect.getsource(kb_withdraw) + f"\n_x = {injection}\n"
    with pytest.raises(AssertionError):
        withdraw_never_takes_the_global_lock(mutated)


# --- the argument grammar (decision 6) -----------------------------------------------------------

GOOD = [("site-guide", "1"), ("a", "999999"), ("a-b-c", "12"), ("x" * 40, "3")]
BAD_ARGS: list[list[str]] = [
    [],
    ["doc"],
    ["doc", "1", "extra"],
    ["doc\n", "1"],
    ["doc", "1\n"],
    ["Doc", "1"],
    ["doc_x", "1"],
    ["a-b-c-d", "1"],
    ["-a", "1"],
    ["a-", "1"],
    ["x" * 41, "1"],
    ["", "1"],
    ["doc", "0"],
    ["doc", "01"],
    ["doc", "1000000"],
    ["doc", "-1"],
    ["doc", "1.0"],
    ["doc", " 1"],
    ["doc", ""],
]


@pytest.mark.parametrize(("document_id", "version"), GOOD)
def test_good_arguments_are_accepted(document_id: str, version: str) -> None:
    assert parse_document_version([document_id, version]) == (document_id, int(version))


def bad_arguments_are_refused(parse: Callable[[list[str]], Any]) -> None:
    for args in BAD_ARGS:
        try:
            parse(args)
        except (IndexError, KeyError, TypeError):
            raise AssertionError("a crash is not a refusal") from None
        except Exception:  # noqa: BLE001 - a mutant's own refusal class still counts as refusal
            continue
        raise AssertionError("a bad argument list was accepted")


def test_every_bad_argument_list_is_refused_and_the_message_echoes_nothing() -> None:
    bad_arguments_are_refused(parse_document_version)
    for args in BAD_ARGS:
        with pytest.raises(ArgumentRefused) as caught:
            parse_document_version(args)
        assert str(caught.value) == "arguments refused: expected <document_id> <version>"


@pytest.mark.parametrize("marker", ["Zq9Marker\n", "zq9_marker", "9" * 12 + "\n"])
def test_a_distinctive_bad_value_is_never_echoed(marker: str) -> None:
    for args in ([marker, "1"], ["ok", marker]):
        with pytest.raises(ArgumentRefused) as caught:
            parse_document_version(args)
        assert marker.strip() not in str(caught.value)


def _copy_of_common(old: str, new: str) -> Any:
    source = inspect.getsource(kb_common)
    assert old in source
    namespace: dict[str, Any] = {"__name__": "kb_common_mutant"}
    exec(compile(source.replace(old, new), "<mutant>", "exec"), namespace)  # noqa: S102
    return namespace["parse_document_version"]


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("fullmatch -> match (a trailing newline passes)", ".fullmatch(", ".match("),
        ("one argument allowed", "len(args) != 2", "len(args) > 2"),
        ("version cap removed", "[1-9][0-9]{0,5}", "[1-9][0-9]*"),
    ],
)
def test_mutation_a_weaker_grammar_is_caught(label: str, old: str, new: str) -> None:
    mutated = _copy_of_common(old, new)
    with pytest.raises(AssertionError):
        bad_arguments_are_refused(mutated)


def test_the_patterns_are_not_anchored_with_dollar() -> None:
    source = inspect.getsource(kb_common)
    assert not re.search(r're\.compile\(r"[^"]*\$"\)', source)


# --- main(): bad arguments write nothing ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [[], ["load", "doc", "1"], ["withdraw"], ["withdraw", "doc"], ["withdraw", "Doc\n", "1"]],
)
def test_main_with_bad_arguments_exits_non_zero_and_touches_nothing(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("nothing may be read or opened")

    monkeypatch.setattr(kb_withdraw, "load_loader_settings", never)
    monkeypatch.setattr(psycopg, "connect", never)
    status = kb_withdraw.main(argv)
    out = capsys.readouterr()
    assert status == kb_withdraw.FAILED_ARGUMENTS.status != 0
    assert out.out == "" and out.err.strip() == kb_withdraw.FAILED_ARGUMENTS.message
    assert "Doc" not in out.err
