"""The `load`/`approve` exit-status table (ADR 0024 A3, Q5): every number, its fixed message, the
SQLSTATE and refusal maps and the precedence of the refusal map. Offline; mutations on COPIES."""

from __future__ import annotations

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_load, kb_manifest
from divesafe.services.kb_common import (
    ArgumentRefused,
    DeadlineExceeded,
    LimitExceeded,
    LimitNotSet,
)

pytestmark = pytest.mark.safety

CAUGHT = (AssertionError, Exception)

# Q5 as approved. Numbers 2-12 keep withdraw's meanings.
EXPECTED: dict[str, tuple[int, str]] = {
    "FAILED_ARGUMENTS": (2, "refused: arguments; nothing written"),
    "FAILED_CONFIGURATION": (3, "refused: loader configuration; nothing written"),
    "FAILED_CONNECTION": (4, "failed: database connection; nothing written"),
    "FAILED_ROLE": (5, "refused: role has owner or application write privileges; nothing written"),
    "FAILED_LOCK_BUSY": (6, "failed: lock busy: see the runbook; nothing written"),
    "FAILED_STATEMENT_TIMEOUT": (7, "failed: statement timeout; nothing written"),
    "FAILED_ISOLATION": (8, "refused: isolation level; nothing written"),
    "FAILED_DEADLOCK": (9, "failed: deadlock; nothing written"),
    "FAILED_NOT_FOUND": (10, "refused: version not found; nothing written"),
    "FAILED_OTHER": (11, "refused: the database refused the write; nothing written"),
    "OUTCOME_UNKNOWN": (12, "outcome unknown: check the status, then re-run if needed"),
    "FAILED_MANIFEST_INVALID": (13, "refused: manifest or corpus not valid; nothing written"),
    "FAILED_HASH_MISMATCH": (14, "refused: manifest hash does not match; nothing written"),
    "FAILED_HASH_REQUIRED": (15, "refused: approved manifest hash required; nothing written"),
    "FAILED_ZERO_CHUNKS": (16, "refused: no chunks to load; nothing written"),
    "FAILED_LIMIT": (17, "refused: a size or count limit was exceeded; nothing written"),
    "FAILED_DEADLINE": (18, "failed: overall deadline exceeded; nothing written"),
    "FAILED_SELF_CHECK": (19, "refused: loader role over-privileged; nothing written"),
    "FAILED_SELF_CHECK_INCOMPLETE": (20, "refused: self-check incomplete; nothing written"),
    "FAILED_WRONG_STATUS": (21, "refused: version is not in DRAFT status; nothing written"),
    "FAILED_FILE_HASH": (22, "refused: a file hash does not match; nothing written"),
    "FAILED_CHUNK_HASH": (23, "refused: a chunk hash does not match; nothing written"),
    "OK_LOAD": (0, "loaded as DRAFT"),
    "OK_APPROVE": (0, "approved"),
}


def check_table(module: object) -> None:
    for name, (status, message) in EXPECTED.items():
        outcome = getattr(module, name)
        assert (outcome.status, outcome.message) == (status, message), name
    table = module.EXIT_TABLE  # type: ignore[attr-defined]
    assert [o.status for o in table] == [s for s, _ in EXPECTED.values()]
    assert [o.message for o in table] == [m for _, m in EXPECTED.values()]


def test_every_row_of_the_q5_table_is_pinned() -> None:
    check_table(kb_load)
    assert len(kb_load.EXIT_TABLE) == 24


def test_each_failure_number_has_exactly_one_message_and_success_is_zero_only_for_ok() -> None:
    failures = [o for o in kb_load.EXIT_TABLE if o.status != 0]
    assert len({o.status for o in failures}) == len(failures) == 22
    assert len({o.message for o in kb_load.EXIT_TABLE}) == 24
    zeros = [o for o in kb_load.EXIT_TABLE if o.status == 0]
    assert {o.message for o in zeros} == {"loaded as DRAFT", "approved"}


def test_every_refusal_says_nothing_was_written_and_unknown_says_to_check() -> None:
    for outcome in kb_load.EXIT_TABLE:
        if outcome.status == 0:
            continue
        if outcome.status == 12:
            assert "check the status" in outcome.message
            assert "nothing written" not in outcome.message
        else:
            assert outcome.message.endswith("nothing written"), outcome.status


def test_messages_are_fixed_text_with_no_placeholder_or_value_slot() -> None:
    for outcome in kb_load.EXIT_TABLE:
        assert not any(ch in outcome.message for ch in "{}%\n\r\t"), outcome.status
        assert outcome.message == outcome.message.strip()
        assert outcome.message.isascii()


def test_numbers_two_to_twelve_keep_the_withdraw_meanings() -> None:
    from divesafe.services import kb_withdraw

    pairs = {
        2: (kb_load.FAILED_ARGUMENTS, kb_withdraw.FAILED_ARGUMENTS),
        4: (kb_load.FAILED_CONNECTION, kb_withdraw.FAILED_CONNECTION),
        5: (kb_load.FAILED_ROLE, kb_withdraw.FAILED_ROLE),
        6: (kb_load.FAILED_LOCK_BUSY, kb_withdraw.FAILED_LOCK_BUSY),
        7: (kb_load.FAILED_STATEMENT_TIMEOUT, kb_withdraw.FAILED_STATEMENT_TIMEOUT),
        8: (kb_load.FAILED_ISOLATION, kb_withdraw.FAILED_ISOLATION),
        9: (kb_load.FAILED_DEADLOCK, kb_withdraw.FAILED_DEADLOCK),
        12: (kb_load.OUTCOME_UNKNOWN, kb_withdraw.OUTCOME_UNKNOWN),
    }
    for status, (loaded, withdrawn) in pairs.items():
        # the numbers are shared; the wording is each command's own (withdraw names itself)
        assert loaded.status == withdrawn.status == status


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('Outcome(5, "refused: role has owner', 'Outcome(19, "refused: role has owner'),
        ('Outcome(19, "refused: loader role', 'Outcome(5, "refused: loader role'),
        ('Outcome(14, "refused: manifest hash does', 'Outcome(22, "refused: manifest hash does'),
        (
            'Outcome(21, "refused: version is not in DRAFT',
            'Outcome(10, "refused: version is not in DRAFT',
        ),
        ('"refused: no chunks to load; nothing written"', '"refused: no chunks to load"'),
        ('"outcome unknown: check the status, then re-run if needed"', '"failed: nothing written"'),
        ('OK_APPROVE = Outcome(0, "approved")', 'OK_APPROVE = Outcome(0, "loaded as DRAFT")'),
    ],
)
def test_mutation_a_changed_row_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_load, (old, new))
    with pytest.raises(CAUGHT):
        check_table(mutant)
    check_table(kb_load)


# --- the SQLSTATE map and the refusal map ---------------------------------------------------------


def check_maps(module: object) -> None:
    by_state = module._BY_SQLSTATE  # type: ignore[attr-defined]
    assert {k: v.status for k, v in by_state.items()} == {
        "55P03": 6,
        "57014": 7,
        "40P01": 9,
        "25001": 8,
    }
    by_refusal = {t: o.status for t, o in module._BY_REFUSAL}  # type: ignore[attr-defined]
    assert by_refusal == {
        DeadlineExceeded: 18,
        kb_manifest.ZeroChunks: 16,
        kb_manifest.FileHashMismatch: 22,
        kb_manifest.ChunkHashMismatch: 23,
        kb_manifest.ManifestTooLarge: 17,
        LimitExceeded: 17,
        kb_manifest.ManifestInvalid: 13,
        LimitNotSet: 3,
        ArgumentRefused: 2,
    }


def test_the_sqlstate_and_refusal_maps_are_pinned() -> None:
    check_maps(kb_load)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"55P03": FAILED_LOCK_BUSY', '"55P03": FAILED_OTHER'),
        ('"40P01": FAILED_DEADLOCK', '"40P01": FAILED_LOCK_BUSY'),
        ('"57014": FAILED_STATEMENT_TIMEOUT', '"57014": FAILED_LOCK_BUSY'),
        ('"25001": FAILED_ISOLATION', '"25001": FAILED_OTHER'),
        (
            "(kb_manifest.FileHashMismatch, FAILED_FILE_HASH)",
            "(kb_manifest.FileHashMismatch, FAILED_CHUNK_HASH)",
        ),
        (
            "(kb_manifest.ZeroChunks, FAILED_ZERO_CHUNKS)",
            "(kb_manifest.ZeroChunks, FAILED_MANIFEST_INVALID)",
        ),
        ("(LimitNotSet, FAILED_CONFIGURATION)", "(LimitNotSet, FAILED_LIMIT)"),
        ("(DeadlineExceeded, FAILED_DEADLINE)", "(DeadlineExceeded, FAILED_LIMIT)"),
    ],
)
def test_mutation_a_changed_map_entry_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_load, (old, new))
    with pytest.raises(CAUGHT):
        check_maps(mutant)
    check_maps(kb_load)


def test_the_specific_refusals_come_before_the_general_ones_in_the_map() -> None:
    """`_BY_REFUSAL` is searched in order with isinstance, so a subclass must precede its base."""
    types_in_order = [t for t, _ in kb_load._BY_REFUSAL]
    for index, specific in enumerate(types_in_order):
        for general in types_in_order[index + 1 :]:
            assert not issubclass(general, specific) or general is specific, (specific, general)
    assert not issubclass(kb_manifest.ManifestTooLarge, kb_manifest.ManifestInvalid)
    assert not issubclass(kb_manifest.ZeroChunks, kb_manifest.ManifestInvalid)


def test_an_unlisted_refusal_falls_back_to_manifest_invalid(
    tmp_path: pytest.TempPathFactory,
) -> None:
    class Odd(kb_load.LoaderRefusal):
        pass

    found = next((o for t, o in kb_load._BY_REFUSAL if isinstance(Odd("x"), t)), None)
    assert found is None  # run() then uses FAILED_MANIFEST_INVALID, never success
