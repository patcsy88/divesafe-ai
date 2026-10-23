"""`load`/`approve` approved limits, the Deadline, the argument grammar and the hash rule
(ADR 0024 A3, P1-P6, Q5, Q8). Offline. Mutations run on COPIES of the source, never on `src`."""

from __future__ import annotations

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_common, kb_load, kb_withdraw
from divesafe.services.kb_common import ArgumentRefused, Deadline, DeadlineExceeded
from divesafe.services.kb_load import Request

pytestmark = pytest.mark.safety

HASH = "b" * 64
CAUGHT = (AssertionError, Exception, pytest.fail.Exception)  # a failed check or a crash


# --- approved constants --------------------------------------------------------------------------


def check_constants(common: object) -> None:
    c = common
    assert c.WITHDRAW_IDLE_IN_TRANSACTION_TIMEOUT_MS == 10_000  # type: ignore[attr-defined]
    assert c.LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS == 30_000  # type: ignore[attr-defined]
    assert c.LOAD_LOCK_TIMEOUT_MS == 10_000  # type: ignore[attr-defined]
    assert c.LOAD_STATEMENT_TIMEOUT_MS == 30_000  # type: ignore[attr-defined]
    assert c.LOAD_DEADLINE_SECONDS == 120.0  # type: ignore[attr-defined]
    assert c.APPROVE_DEADLINE_SECONDS == 60.0  # type: ignore[attr-defined]
    assert c.LOAD_SELF_CHECK_STATEMENT_TIMEOUT_MS == 15_000  # type: ignore[attr-defined]
    assert c.LOAD_READ_STATEMENT_TIMEOUT_MS == 10_000  # type: ignore[attr-defined]
    assert c.MAX_MANIFEST_TOTAL_BYTES == 1_048_576  # type: ignore[attr-defined]
    assert c.MAX_DOCUMENTS_PER_LOAD == 50  # type: ignore[attr-defined]
    assert c.MAX_CHUNKS_PER_LOAD == 200  # type: ignore[attr-defined]
    assert c.MAX_CHUNK_CHARACTERS == 1500  # type: ignore[attr-defined]
    assert c.MAX_CHUNKS_PER_RUN == 20  # type: ignore[attr-defined]
    assert c.KB_DOCUMENT_LOCK_CLASS == 1220001  # type: ignore[attr-defined]
    assert c.KB_LOAD_GLOBAL_LOCK_CLASS == 1220002  # type: ignore[attr-defined]
    assert c.KB_LOAD_GLOBAL_LOCK_OBJECT == 0  # type: ignore[attr-defined]
    # the relation: a lock wait always ends before the statement budget does
    assert c.LOAD_LOCK_TIMEOUT_MS < c.LOAD_STATEMENT_TIMEOUT_MS  # type: ignore[attr-defined]
    assert c.LOAD_READ_STATEMENT_TIMEOUT_MS <= c.LOAD_STATEMENT_TIMEOUT_MS  # type: ignore[attr-defined]
    assert c.KB_DOCUMENT_LOCK_CLASS != c.KB_LOAD_GLOBAL_LOCK_CLASS  # type: ignore[attr-defined]


def test_the_approved_constants_are_pinned() -> None:
    check_constants(kb_common)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "WITHDRAW_IDLE_IN_TRANSACTION_TIMEOUT_MS = 10_000",
            "WITHDRAW_IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000",
        ),
        (
            "LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000",
            "LOAD_IDLE_IN_TRANSACTION_TIMEOUT_MS = 90_000",
        ),
        ("LOAD_LOCK_TIMEOUT_MS = 10_000", "LOAD_LOCK_TIMEOUT_MS = 40_000"),
        ("LOAD_STATEMENT_TIMEOUT_MS = 30_000", "LOAD_STATEMENT_TIMEOUT_MS = 5_000"),
        ("LOAD_DEADLINE_SECONDS = 120.0", "LOAD_DEADLINE_SECONDS = 600.0"),
        ("APPROVE_DEADLINE_SECONDS = 60.0", "APPROVE_DEADLINE_SECONDS = 600.0"),
        (
            "LOAD_SELF_CHECK_STATEMENT_TIMEOUT_MS: int | None = 15_000",
            "LOAD_SELF_CHECK_STATEMENT_TIMEOUT_MS: int | None = 1",
        ),
        (
            "LOAD_READ_STATEMENT_TIMEOUT_MS: int | None = 10_000",
            "LOAD_READ_STATEMENT_TIMEOUT_MS: int | None = 99_000",
        ),
        ("MAX_MANIFEST_TOTAL_BYTES = 1_048_576", "MAX_MANIFEST_TOTAL_BYTES = 9_999_999"),
        ("MAX_DOCUMENTS_PER_LOAD = 50", "MAX_DOCUMENTS_PER_LOAD = 500"),
        ("MAX_CHUNKS_PER_LOAD = 200", "MAX_CHUNKS_PER_LOAD = 2000"),
        ("MAX_CHUNK_CHARACTERS = 1500", "MAX_CHUNK_CHARACTERS = 15000"),
        ("MAX_CHUNKS_PER_RUN = 20", "MAX_CHUNKS_PER_RUN = 200"),
        ("KB_LOAD_GLOBAL_LOCK_CLASS = 1220002", "KB_LOAD_GLOBAL_LOCK_CLASS = 1220001"),
        ("KB_LOAD_GLOBAL_LOCK_OBJECT = 0", "KB_LOAD_GLOBAL_LOCK_OBJECT = 7"),
    ],
)
def test_mutation_changing_an_approved_constant_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_common, (old, new))
    with pytest.raises(AssertionError):
        check_constants(mutant)


def test_mutation_a_lock_timeout_not_below_the_statement_timeout_is_caught() -> None:
    mutant = mutated_module(
        kb_common,
        ("LOAD_STATEMENT_TIMEOUT_MS = 30_000", "LOAD_STATEMENT_TIMEOUT_MS = 10_000"),
    )
    assert not mutant.LOAD_LOCK_TIMEOUT_MS < mutant.LOAD_STATEMENT_TIMEOUT_MS
    with pytest.raises(AssertionError):
        check_constants(mutant)


def test_the_withdraw_idle_timeout_is_the_pinned_constant_and_is_sent() -> None:
    source = __import__("inspect").getsource(kb_withdraw)
    assert "WITHDRAW_IDLE_IN_TRANSACTION_TIMEOUT_MS" in source
    assert "idle_in_transaction_session_timeout" in source


# --- Deadline ------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def check_deadline(deadline_class: type) -> None:
    clock = Clock()
    deadline = deadline_class(10.0, clock)
    deadline.check()
    clock.now += 10.0
    deadline.check()  # exactly at the limit is still inside it
    clock.now += 0.001
    with pytest.raises(DeadlineExceeded):
        deadline.check()
    with pytest.raises(DeadlineExceeded):  # and stays exceeded
        deadline.check()


def test_deadline_is_exceeded_only_after_its_seconds() -> None:
    check_deadline(Deadline)


def test_deadline_starts_when_it_is_built_not_when_first_checked() -> None:
    clock = Clock()
    deadline = Deadline(5.0, clock)
    clock.now += 6.0
    with pytest.raises(DeadlineExceeded):
        deadline.check()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("if self._clock() > self._end:", "if self._clock() > self._end + 1000:"),
        ("if self._clock() > self._end:", "if False:"),
        ("self._end = clock() + seconds", "self._end = clock() + seconds * 100"),
    ],
)
def test_mutation_a_deadline_that_never_fires_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_common, (old, new))
    with pytest.raises(CAUGHT):
        check_deadline(mutant.Deadline)


def test_mutation_a_deadline_started_late_is_caught() -> None:
    mutant = mutated_module(
        kb_common,
        (
            "        self._end = clock() + seconds",
            "        self._end = float('inf')",
        ),
    )
    with pytest.raises(CAUGHT):
        check_deadline(mutant.Deadline)


# --- argument grammar ----------------------------------------------------------------------------


def refused(args: list[str], module: object = kb_load) -> bool:
    try:
        module.parse_arguments(args)  # type: ignore[attr-defined]
    except ArgumentRefused:
        return True
    return False


def check_grammar(module: object) -> None:
    parse = module.parse_arguments  # type: ignore[attr-defined]
    assert parse(["load"]).command == "load"
    request = parse(["approve", "site-guide", "3", "--approved-manifest-sha256", HASH])
    assert (request.document_id, request.version, request.supplied_hash) == ("site-guide", 3, HASH)
    assert parse(["load", "--development-no-manifest-hash"]).development_flag is True
    for bad in (
        [],
        ["unload"],
        ["unload", "site-guide", "1"],
        ["withdraw", "site-guide", "1"],
        ["LOAD"],
        ["load", "extra"],
        ["load", "--no-such-option"],
        ["load", "-x"],
        ["load", "--approved-manifest-sha256"],
        ["load", "--approved-manifest-sha256", "abc"],
        ["load", "--approved-manifest-sha256", HASH.upper()],
        ["load", "--approved-manifest-sha256", HASH + "\n"],
        ["load", "--approved-manifest-sha256", HASH, "--approved-manifest-sha256", HASH],
        ["load", "--development-no-manifest-hash", "--development-no-manifest-hash"],
        ["load", "--development-no-manifest-hash", "--approved-manifest-sha256", HASH],
        ["load", "--approved-manifest-sha256", HASH, "--development-no-manifest-hash"],
        ["approve"],
        ["approve", "site-guide"],
        ["approve", "site-guide", "1", "2"],
        ["approve", "Site-Guide", "1"],
        ["approve", "site-guide", "0"],
        ["approve", "site-guide", "01"],
        ["approve", "site-guide", "1\n"],
        ["approve", "site-guide\n", "1"],
        ["approve", "site-guide", "1", "--bogus"],
    ):
        with pytest.raises(ArgumentRefused):
            parse(bad)


def test_the_argument_grammar_is_exact() -> None:
    check_grammar(kb_load)


# Not mutated: `elif item.startswith("-")` -> `elif False` is an equivalent mutant. A stray option
# then lands in the positional list, which both `load` and `approve` refuse.
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "    if dev and supplied is not None:\n        raise ArgumentRefused",
            "    if False:\n        raise ArgumentRefused",
        ),
        (
            "        if positional:\n            raise ArgumentRefused",
            "        if False:\n            raise ArgumentRefused",
        ),
        (
            "if supplied is not None or not rest or not valid_manifest_sha256(rest[0])",
            "if not rest or not valid_manifest_sha256(rest[0])",
        ),
        (
            "            if dev:\n                raise ArgumentRefused",
            "            if False:\n                raise ArgumentRefused",
        ),
        ('if not args or args[0] not in ("load", "approve"):', "if not args:"),
        (
            'if not args or args[0] not in ("load", "approve"):',
            'if not args or args[0] not in ("load", "approve", "unload"):',
        ),
    ],
)
def test_mutation_a_looser_argument_parser_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_load, (old, new))
    with pytest.raises(CAUGHT):
        check_grammar(mutant)
    # and it was not caught for the wrong reason: the real parser still passes
    check_grammar(kb_load)


def test_mutation_a_prefix_match_on_the_version_accepts_a_trailing_newline() -> None:
    mutant = mutated_module(kb_common, ("_VERSION.fullmatch(value)", "_VERSION.match(value)"))
    with pytest.raises(ArgumentRefused):
        kb_common.parse_document_version(["site-guide", "1\n"])
    assert mutant.parse_document_version(["site-guide", "1\n"]) == ("site-guide", 1)


def test_mutation_a_prefix_match_on_the_hash_accepts_trailing_text() -> None:
    mutant = mutated_module(
        kb_common, ("_MANIFEST_SHA256.fullmatch(value)", "_MANIFEST_SHA256.match(value)")
    )
    assert not kb_common.valid_manifest_sha256(HASH + "\n")
    assert mutant.valid_manifest_sha256(HASH + "\n")


def test_a_refusal_carries_no_input() -> None:
    secret = "Token_" + "X" * 30
    for args in (["load", secret], ["approve", secret, "1"], ["load", "--" + secret]):
        with pytest.raises(ArgumentRefused) as caught:
            kb_load.parse_arguments(args)
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)


def test_main_maps_every_argument_refusal_to_exit_2_with_the_fixed_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "zz-private-value"
    status = kb_load.main(["approve", secret])
    seen = capsys.readouterr()
    assert status == 2
    assert seen.err.strip() == kb_load.FAILED_ARGUMENTS.message
    assert secret not in seen.out + seen.err


# --- the hash rule -------------------------------------------------------------------------------


def request(*, supplied: str | None = None, dev: bool = False) -> Request:
    return Request("load", None, None, supplied, dev)


def check_hash_rule(module: object, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = module.hash_must_be_checked  # type: ignore[attr-defined]
    required = module.HashRequired  # type: ignore[attr-defined]
    # the flag outside production skips; with a supplied hash it is checked
    assert rule(False, request(dev=True)) is False
    assert rule(True, request(supplied=HASH)) is True
    assert rule(False, request(supplied=HASH)) is True
    # the flag in production is refused
    with pytest.raises(required):
        rule(True, request(dev=True))
    # no flag and no hash is refused everywhere
    for production in (True, False):
        with pytest.raises(required):
            rule(production, request())
    # the environment alone never skips it: neither name nor value is read by the rule
    for value in ("development", "test", "dev", "1", "true"):
        monkeypatch.setenv("DIVESAFE_ENVIRONMENT", value)
        monkeypatch.setenv("DIVESAFE_DEVELOPMENT_NO_MANIFEST_HASH", "1")
        with pytest.raises(required):
            rule(False, request())


def test_the_hash_is_skipped_only_with_the_flag_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_hash_rule(kb_load, monkeypatch)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "        if production:\n            raise HashRequired(",
            "        if False:\n            raise HashRequired(",
        ),
        (
            "    if request.supplied_hash is None:\n        raise HashRequired",
            "    if request.supplied_hash is None and __import__('os').environ.get(\n"
            "        'DIVESAFE_ENVIRONMENT'\n"
            "    ) != 'development':\n"
            "        raise HashRequired",
        ),
        (
            "    if request.supplied_hash is None:\n        raise HashRequired",
            "    if False:\n        raise HashRequired",
        ),
        (
            "    if request.development_flag:\n        if production:",
            "    if request.development_flag or not production:\n        if production:",
        ),
    ],
)
def test_mutation_a_weaker_hash_rule_is_caught(
    old: str, new: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = mutated_module(kb_load, (old, new))
    with pytest.raises(CAUGHT):
        check_hash_rule(mutant, monkeypatch)
    check_hash_rule(kb_load, monkeypatch)


def test_the_development_flag_is_a_command_line_argument_only() -> None:
    import inspect

    source = inspect.getsource(kb_load)
    assert "os.environ" not in source
    assert "getenv" not in source
