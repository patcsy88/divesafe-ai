"""AGENTS.md section 4 (owner, 2026-11-12): `pytest -m safety` never skips, with two documented
exceptions: a safety test that needs a test database skips without DIVESAFE_TEST_DATABASE_URL
(never in CI, which has a database), and the case-only path check skips on a case-sensitive
volume (always on Linux CI). CI fails on any other skip. These tests pin that CI step and run the
step's own check against sample logs, so a weakened pattern is caught."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

ROOT = Path(__file__).parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SUITE = "run: pytest -m safety -rs --color=no | tee safety.log"
GUARD_NAME = "- name: Fail if a safety test was skipped"
ALLOWED = (
    "SKIPPED [1] tests/evaluation/test_capture_item6_offline.py:269: "
    "this volume is case-sensitive; the case-only check has nothing to test\n"
)
CLEAN = "4757 passed, 1370 deselected, 30 xfailed\n"


def _ci() -> str:
    return CI.read_text(encoding="utf-8")


def _guard_step() -> str:
    ci = _ci()
    start = ci.index(GUARD_NAME)
    end = ci.index("\n      - ", start + 1)
    return ci[start:end]


def _guard_command() -> str:
    match = re.search(r"run: \|\n\s+(?P<cmd>[^\n]+)$", _guard_step())
    assert match is not None, "the safety skip guard must be a single-line `run: |` command"
    return match.group("cmd")


def _guard_passes(log: str, tmp_path: Path) -> bool:
    (tmp_path / "safety.log").write_text(log, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-o", "pipefail", "-c", _guard_command()],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def test_ci_runs_the_safety_suite_with_a_database_and_keeps_a_plain_log() -> None:
    ci = _ci()
    assert "DIVESAFE_TEST_DATABASE_URL: postgresql://" in ci
    assert SUITE in ci  # -rs lists skips; --color=no keeps escape codes out of the log
    run_line = ci.index(SUITE)
    shell_line = ci.rfind("shell: bash", 0, run_line)
    assert shell_line != -1 and run_line - shell_line < 200  # pipefail: a failure is not hidden
    assert ci.index(GUARD_NAME) > run_line  # the guard runs after the suite


def test_the_guard_step_is_one_command_that_cannot_be_ignored() -> None:
    step = _guard_step()
    command = _guard_command()
    assert "continue-on-error" not in step
    assert (
        "|" not in command
    )  # no pipeline: SIGPIPE under pipefail cannot turn a failure into a pass
    assert command.startswith("awk ") and command.endswith(" safety.log")
    assert (
        "tests\\/evaluation\\/test_capture_item6_offline\\.py:" in command
    )  # anchored to one test


def test_the_guard_fails_on_any_skip_but_the_documented_one(tmp_path: Path) -> None:
    db_skip = (
        "SKIPPED [1] tests/integration/test_kb_load_live.py:844: "
        "DIVESAFE_TEST_DATABASE_URL not set\n"
    )
    other = "SKIPPED [1] tests/safety/test_anything.py:1: some reason\n"
    copied = "SKIPPED [1] tests/safety/test_anything.py:1: this volume is case-sensitive; copied\n"
    coloured = "\x1b[33mSKIPPED\x1b[0m [1] tests/safety/test_anything.py:1: some reason\n"
    coloured_allowed = "\x1b[33m" + ALLOWED
    assert _guard_passes(CLEAN, tmp_path)
    assert _guard_passes(ALLOWED + CLEAN, tmp_path)
    assert not _guard_passes(db_skip + CLEAN, tmp_path)
    assert not _guard_passes(other + CLEAN, tmp_path)
    assert not _guard_passes(ALLOWED + other + CLEAN, tmp_path)
    assert not _guard_passes(copied + CLEAN, tmp_path)  # the reason alone is not enough
    assert not _guard_passes(coloured + CLEAN, tmp_path)  # colour fails closed
    assert not _guard_passes(coloured_allowed + CLEAN, tmp_path)


def test_agents_md_section_4_states_the_rule_and_its_two_exceptions() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = " ".join(
        text[text.index("## 4. Testing standards") : text.index("## 5. Safety principles")].split()
    )
    assert "`pytest -m safety` MUST always pass and never skip." in section
    assert "Two documented exceptions" in section
    assert "fails on any skip except the case-sensitive check" in section
