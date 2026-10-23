"""`withdraw` exit statuses (ADR 0024 A3): code -> message -> status, pinned row by row, with the
precedence order. A mutation that swaps two statuses must fail. Database-free."""

from __future__ import annotations

import pytest

from divesafe.services import kb_withdraw
from divesafe.services.kb_withdraw import EXIT_TABLE, Outcome

pytestmark = pytest.mark.safety

# name -> (status, fixed message). Written out independently of the source on purpose.
PINNED: dict[str, tuple[int, str]] = {
    "FAILED_ARGUMENTS": (2, "withdraw refused: expected <document_id> <version>; nothing written"),
    "FAILED_CONFIGURATION": (3, "withdraw refused: loader configuration; nothing written"),
    "FAILED_CONNECTION": (4, "withdraw failed: database connection; nothing written"),
    "FAILED_ROLE": (5, "withdraw refused: running as an owner or app role; nothing written"),
    "FAILED_LOCK_BUSY": (6, "withdraw failed: lock busy: see the runbook; nothing written"),
    "FAILED_STATEMENT_TIMEOUT": (
        7,
        "withdraw failed: statement timeout: see the runbook; nothing written",
    ),
    "FAILED_ISOLATION": (8, "withdraw refused: isolation level; nothing written"),
    "FAILED_DEADLOCK": (9, "withdraw failed: deadlock; nothing written"),
    "FAILED_NOT_FOUND": (10, "withdraw refused: version not found; nothing written"),
    "FAILED_OTHER": (11, "withdraw refused: the database refused the event; nothing written"),
    "OUTCOME_UNKNOWN": (12, "outcome unknown: re-run the same command"),
    "WITHDRAWN_OVER_PRIVILEGED": (
        13,
        "withdrawn; loader role over-privileged, fix before any load",
    ),
    "WITHDRAWN_SELF_CHECK_INCOMPLETE": (14, "withdrawn; self-check incomplete"),
    "ALREADY_WITHDRAWN_NO_TIME": (15, "already withdrawn; recorded_at unavailable"),
    "ALREADY_WITHDRAWN": (16, "already withdrawn"),
    "OK": (0, "withdrawn"),
}
# Precedence, highest first (the order is the decision): failed-nothing-written (2-11), outcome
# unknown, withdrawn but over-privileged / self-check incomplete, already withdrawn (15 between
# the self-check rows and plain "already withdrawn"), ok.
PRECEDENCE = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 0]
SQLSTATES = {
    "55P03": "FAILED_LOCK_BUSY",
    "57014": "FAILED_STATEMENT_TIMEOUT",
    "40P01": "FAILED_DEADLOCK",
    "25001": "FAILED_ISOLATION",
}


def check_table(table: tuple[Outcome, ...]) -> None:
    pinned = [PINNED[name] for name in PINNED]
    assert [(o.status, o.message) for o in table] == pinned
    assert [o.status for o in table] == PRECEDENCE


def check_by_sqlstate(mapping: dict[str, Outcome]) -> None:
    assert {code: PINNED_NAME[o] for code, o in mapping.items()} == SQLSTATES


PINNED_NAME = {getattr(kb_withdraw, name): name for name in PINNED}


def test_the_exit_table_matches_the_pinned_rows_and_precedence() -> None:
    check_table(EXIT_TABLE)


def test_every_named_outcome_is_in_the_table_once() -> None:
    assert len(set(map(id, EXIT_TABLE))) == len(EXIT_TABLE) == len(PINNED)
    for name, (status, message) in PINNED.items():
        outcome = getattr(kb_withdraw, name)
        assert (outcome.status, outcome.message) == (status, message)


def test_only_ok_exits_zero_and_statuses_and_messages_are_unique() -> None:
    assert [o.status for o in EXIT_TABLE if o.status == 0] == [0]
    assert kb_withdraw.OK.status == 0
    assert len({o.status for o in EXIT_TABLE}) == len(EXIT_TABLE)
    assert len({o.message for o in EXIT_TABLE}) == len(EXIT_TABLE)


def test_every_message_is_fixed_text_with_no_format_field() -> None:
    for outcome in EXIT_TABLE:
        assert not any(mark in outcome.message for mark in ("{", "}", "%"))
        assert "recorded_at=" not in outcome.message


def test_only_the_over_privileged_messages_say_over_privileged() -> None:
    texts = [o.message for o in EXIT_TABLE if "over-privileged" in o.message]
    assert texts == [kb_withdraw.WITHDRAWN_OVER_PRIVILEGED.message]
    assert kb_withdraw.WITHDRAWN_OVER_PRIVILEGED.message.startswith("withdrawn;")


def test_the_sqlstate_map_sends_each_code_to_its_own_outcome() -> None:
    check_by_sqlstate(kb_withdraw._BY_SQLSTATE)


def test_the_already_withdrawn_code_is_not_in_the_generic_map_and_is_kb001() -> None:
    assert kb_withdraw.SQLSTATE_ALREADY_WITHDRAWN == "KB001"
    assert "KB001" not in kb_withdraw._BY_SQLSTATE and "P0001" not in kb_withdraw._BY_SQLSTATE


def _swap(table: tuple[Outcome, ...], a: int, b: int) -> tuple[Outcome, ...]:
    rows = list(table)
    first, second = rows[a], rows[b]
    rows[a] = Outcome(second.status, first.message)  # the messages stay, the statuses swap
    rows[b] = Outcome(first.status, second.message)
    return tuple(rows)


@pytest.mark.parametrize(("a", "b"), [(4, 5), (12, 13), (13, 14), (14, 15), (0, 15)])
def test_mutation_swapping_two_statuses_is_caught(a: int, b: int) -> None:
    with pytest.raises(AssertionError):
        check_table(_swap(EXIT_TABLE, a, b))


def test_mutation_swapping_two_sqlstate_targets_is_caught() -> None:
    swapped = dict(kb_withdraw._BY_SQLSTATE)
    swapped["55P03"], swapped["57014"] = swapped["57014"], swapped["55P03"]
    with pytest.raises(AssertionError):
        check_by_sqlstate(swapped)


def test_mutation_reordering_the_precedence_is_caught() -> None:
    rows = list(EXIT_TABLE)
    rows[13], rows[14] = (
        rows[14],
        rows[13],
    )  # "already withdrawn" ahead of "recorded_at unavailable"
    with pytest.raises(AssertionError):
        check_table(tuple(rows))
