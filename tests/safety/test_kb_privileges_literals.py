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
    with pytest.raises(ValueError):
        kb_privileges._lit(("kb_documents", value))  # noqa: SLF001


def test_one_bad_value_among_good_ones_is_enough_to_refuse() -> None:
    with pytest.raises(ValueError):
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


def test_the_checks_build_without_tripping_the_name_check() -> None:
    assert kb_privileges._checks()  # noqa: SLF001


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    if not all(_NAME.fullmatch(v) for v in values):\n", "    if False:\n"),
        (
            "    if not all(_NAME.fullmatch(v) for v in values):",
            "    if not all(_NAME.match(v) for v in values):",
        ),
        ('_NAME = re.compile(r"[a-z_]+")', '_NAME = re.compile(r"[a-zA-Z0-9_\' ;-]*")'),
    ],
    ids=["check-removed", "match-not-fullmatch", "pattern-widened"],
)
def test_mutation_a_weaker_literal_guard_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_privileges, (old, new))
    survived = []
    for value in ("a'; DROP TABLE x; --", "a'b", "ok'"):
        try:
            mutant._lit(("kb_documents", value))  # noqa: SLF001
        except ValueError:
            continue
        survived.append(value)
    assert survived, "the weaker guard still refused everything"
    for value in survived:
        with pytest.raises(ValueError):
            kb_privileges._lit(("kb_documents", value))  # noqa: SLF001


def test_the_name_check_survives_python_optimisation() -> None:
    """An assert would vanish under `python -O`; the check must not."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "from divesafe.services import kb_privileges\n"
        "try:\n"
        "    kb_privileges._lit(('kb_documents', \"a'b\"))\n"
        "except ValueError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    src = str(Path(kb_privileges.__file__).resolve().parents[2])
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-O", "-c", code],
        check=False,
        capture_output=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": src},
    )
    assert result.returncode == 0, result.stderr[-500:]
