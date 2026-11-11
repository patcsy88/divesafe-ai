"""AGENTS.md section 4 (owner, 2026-11-12): `pytest -m safety` never skips, with two documented
exceptions that only apply off CI (no test database; a case-sensitive volume). CI runs the safety
suite with a database and fails on any other skip. These tests pin that CI step, and run the
step's own shell check against sample logs so a weakened pattern is caught."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

CI = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
ALLOWED = "this volume is case-sensitive"


def _steps() -> str:
    return CI.read_text(encoding="utf-8")


def _guard_command() -> str:
    match = re.search(
        r"- name: Fail if a safety test was skipped[^\n]*\n\s+run: \"(?P<cmd>[^\n]+)\"\n", _steps()
    )
    assert match is not None, "the safety skip guard step is missing from ci.yml"
    return match.group("cmd")


def _guard_passes(log: str, tmp_path: Path) -> bool:
    (tmp_path / "safety.log").write_text(log, encoding="utf-8")
    result = subprocess.run(
        ["sh", "-c", _guard_command()], cwd=tmp_path, capture_output=True, check=False
    )
    return result.returncode == 0


def test_ci_runs_the_safety_suite_with_a_database_and_keeps_its_log() -> None:
    ci = _steps()
    assert "DIVESAFE_TEST_DATABASE_URL: postgresql://" in ci
    assert "run: pytest -m safety -rs | tee safety.log" in ci
    run_line = ci.index("run: pytest -m safety -rs | tee safety.log")
    shell_line = ci.rfind("shell: bash", 0, run_line)
    assert shell_line != -1 and run_line - shell_line < 200  # pipefail: a failure is not hidden
    assert ci.index(_guard_command()) > run_line  # the guard runs after the suite


def test_the_guard_fails_on_any_skip_but_the_documented_one(tmp_path: Path) -> None:
    clean = "4757 passed, 1370 deselected, 30 xfailed\n"
    allowed = f"SKIPPED [1] tests/evaluation/x.py:269: {ALLOWED}; nothing to test\n" + clean
    db_skip = (
        "SKIPPED [1] tests/integration/test_kb_load_live.py:844: "
        "DIVESAFE_TEST_DATABASE_URL not set\n"
    )
    other = "SKIPPED [1] tests/safety/test_anything.py:1: some reason\n"
    assert _guard_passes(clean, tmp_path)
    assert _guard_passes(allowed, tmp_path)
    assert not _guard_passes(db_skip + clean, tmp_path)
    assert not _guard_passes(other + clean, tmp_path)
    assert not _guard_passes(allowed + other + clean, tmp_path)


def test_agents_md_records_the_two_exceptions_and_nothing_wider() -> None:
    text = (Path(__file__).parents[2] / "AGENTS.md").read_text(encoding="utf-8")
    section = text[text.index("## 4. Testing standards") : text.index("## 5. Safety principles")]
    assert "`pytest -m safety` MUST always pass and never skip." in section
    assert "Two documented exceptions" in section
    assert "fails on any other skip" in section
