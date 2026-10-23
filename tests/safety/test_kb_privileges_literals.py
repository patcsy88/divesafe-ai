"""`kb_privileges._lit` builds SQL text from names; only fixed lower-case names may reach it
(round 8). Offline. The mutation runs on a COPY of the module source."""

from __future__ import annotations

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_privileges

pytestmark = pytest.mark.safety

HOSTILE = [
    "a'b",
    "a'; DROP TABLE x; --",
    "A",
    "Kb_documents",
    "a b",
    "a-b",
    "a1",
    "a.b",
    "",
    "é",
    "a\n",
    "a\x00",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_a_name_outside_lower_case_and_underscore_is_refused(value: str) -> None:
    with pytest.raises(AssertionError):
        kb_privileges._lit(("kb_documents", value))  # noqa: SLF001


def test_one_bad_value_among_good_ones_is_enough_to_refuse() -> None:
    with pytest.raises(AssertionError):
        kb_privileges._lit(("a", "b", "c", "d'"))  # noqa: SLF001


def test_good_names_are_quoted_and_joined() -> None:
    result = kb_privileges._lit(("kb_documents", "kb_chunks"))  # noqa: SLF001
    assert result == "'kb_documents', 'kb_chunks'"
    assert kb_privileges._lit(()) == ""  # noqa: SLF001


def test_every_name_the_module_really_passes_is_accepted() -> None:
    for names in (
        kb_privileges.RESERVED_ROLE_NAMES,
        kb_privileges.KNOWLEDGE_TABLES,
        kb_privileges.KB_FUNCTIONS,
        tuple(kb_privileges._LOADER_EVENT_COLUMNS),  # noqa: SLF001
    ):
        text = kb_privileges._lit(names)  # noqa: SLF001
        assert text.count("'") == 2 * len(names)


def test_the_checks_build_without_tripping_the_assertion() -> None:
    assert kb_privileges._checks()  # noqa: SLF001


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    assert all(_NAME.fullmatch(v) for v in values)\n", ""),
        (
            "    assert all(_NAME.fullmatch(v) for v in values)",
            "    assert all(_NAME.match(v) for v in values)",
        ),
        ('_NAME = re.compile(r"[a-z_]+")', '_NAME = re.compile(r"[a-zA-Z0-9_\' ;-]*")'),
    ],
    ids=["assert-removed", "match-not-fullmatch", "pattern-widened"],
)
def test_mutation_a_weaker_literal_guard_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_privileges, (old, new))
    survived = []
    for value in ("a'; DROP TABLE x; --", "a'b", "ok'"):
        try:
            mutant._lit(("kb_documents", value))  # noqa: SLF001
        except AssertionError:
            continue
        survived.append(value)
    assert survived, "the weaker guard still refused everything"
    for value in survived:
        with pytest.raises(AssertionError):
            kb_privileges._lit(("kb_documents", value))  # noqa: SLF001
