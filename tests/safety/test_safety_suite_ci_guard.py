"""AGENTS.md section 4 (owner, 2026-11-12): `pytest -m safety` never skips, with two documented
exceptions: a safety test that needs a test database skips without DIVESAFE_TEST_DATABASE_URL
(never in CI, which has a database), and the case-only path check skips on a case-sensitive
volume (always on Linux CI). CI fails on any other skip. These tests pin that CI step and run the
step's own check against sample logs, so a weakened pattern is caught. They also pin the two
`pip-audit` steps (ADR 0027 "Dependencies": runtime, and runtime with the `[eval]` extra)."""

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


AUDIT_RUNTIME = "- name: Known vulnerabilities in Python dependencies (pip-audit, CI only)"
AUDIT_EVAL = (
    "- name: Known vulnerabilities in the evaluation extra (pip-audit, CI only; never in the image)"
)
_EVAL_REQUIREMENTS = (
    """/tmp/audit/bin/python -c 'import tomllib; p = tomllib.load(open("pyproject.toml", "rb"))"""
    """["project"]; print("\\n".join([*p["dependencies"], *p["optional-dependencies"]["eval"]]))'"""
    " > /tmp/eval-requirements.txt"
)
# Each audit step, exactly: any change (a flag such as --ignore-vuln, a pipe, `set +e`, `exit 0`,
# a `shell:`, `if:` or `continue-on-error` key) fails the test and needs a reviewed test change.
AUDIT_STEPS = {
    AUDIT_RUNTIME: (
        "        run: |\n"
        "          python -m venv --clear /tmp/audit\n"
        '          /tmp/audit/bin/pip install --quiet "pip-audit==2.10.1"\n'
        "          /tmp/audit/bin/pip-audit --progress-spinner off .\n"
    ),
    AUDIT_EVAL: (
        "        run: |\n"
        f"          {_EVAL_REQUIREMENTS}\n"
        "          grep -qx 'langfuse==[0-9.]*' /tmp/eval-requirements.txt\n"
        "          /tmp/audit/bin/pip-audit --progress-spinner off -r /tmp/eval-requirements.txt\n"
    ),
}
# A pip setting, or an environment a step writes for later steps, could change what is audited.
_PIP_SETTINGS = re.compile(
    r"\bPIP_[A-Z0-9_]+\b|GITHUB_ENV|GITHUB_PATH|pip\.conf|pip\.ini|\bpip3? config\b"
)
# Job-level keys that could make the `test` job's steps non-blocking or change their shell.
_JOB_KEYS = re.compile(r"^    (if|continue-on-error|defaults):", re.MULTILINE)


def _step(ci: str, name: str) -> str:
    """The step's body after its name line, up to the next step or the next job, with blank lines
    dropped and YAML comment lines dropped (a key after either is still part of the step)."""
    start = ci.index(name) + len(name) + 1
    ends = [ci.find("\n      - ", start - 1)]
    job = re.search(r"\n {0,2}[^\s#]", ci[start - 1 :])
    if job is not None:
        ends.append(start - 1 + job.start())
    ends = [e for e in ends if e != -1]
    body = ci[start : min(ends)] if ends else ci[start:]
    kept = [
        line for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    return "".join(f"{line}\n" for line in kept)


def _test_job(ci: str) -> str:
    start = ci.index("\n  test:\n")
    return ci[start : ci.index("\n  image:\n", start)]


def _audit_failures(ci: str) -> list[str]:
    """ADR 0027 "Dependencies": CI audits the runtime dependencies, and them together with the
    `[eval]` extra. Neither step may be removed, changed or made unable to fail."""
    problems = []
    if "continue-on-error" in ci:
        problems.append("continue-on-error in the workflow")
    if _JOB_KEYS.search(_test_job(ci)):
        problems.append("the test job has an if:, continue-on-error or defaults: key")
    if re.search(r"^defaults:", ci, re.MULTILINE):
        problems.append("the workflow has a defaults: key")
    if _PIP_SETTINGS.search(ci):
        problems.append("a pip setting or a step-written environment could change the audit")
    for name, expected in AUDIT_STEPS.items():
        if ci.count(name) != 1:
            problems.append(f"missing or repeated: {name}")
        elif ci.index(name) > ci.index("\n  image:\n"):
            problems.append(f"not in the test job: {name}")
        elif _step(ci, name) != expected:
            problems.append(f"changed: {name}")
    if AUDIT_RUNTIME in ci and AUDIT_EVAL in ci and ci.index(AUDIT_EVAL) < ci.index(AUDIT_RUNTIME):
        problems.append("the eval audit runs before the runtime audit creates /tmp/audit")
    if AUDIT_RUNTIME in ci and AUDIT_EVAL in ci:
        runtime_end = ci.index(AUDIT_RUNTIME) + len(AUDIT_RUNTIME)
        if ci.find("\n      - ", runtime_end) != ci.index("      " + AUDIT_EVAL) - 1:
            problems.append("the eval audit does not directly follow the runtime audit")
    return problems


def test_ci_audits_the_runtime_and_the_eval_extra() -> None:
    assert _audit_failures(_ci()) == []


_EVAL_PIP = "/tmp/audit/bin/pip-audit --progress-spinner off -r /tmp/eval-requirements.txt"
_RUN_PIP = "/tmp/audit/bin/pip-audit --progress-spinner off .\n"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (AUDIT_EVAL, "- name: something else"),
        (AUDIT_RUNTIME, "- name: something else"),
        (_EVAL_PIP, _EVAL_PIP + " || true"),
        (_RUN_PIP, _RUN_PIP.replace(" .\n", " . || true\n")),
        (_EVAL_PIP, _EVAL_PIP + " | cat"),
        (_EVAL_PIP, _EVAL_PIP + "; exit 0"),
        (_RUN_PIP, _RUN_PIP.replace(" .\n", " . --ignore-vuln GHSA-0000-0000-0000\n")),
        ("          grep -qx 'langfuse==[0-9.]*' /tmp/eval-requirements.txt\n", ""),
        ('*p["optional-dependencies"]["eval"]', '*p["optional-dependencies"]["dev"]'),
        (AUDIT_EVAL + "\n", AUDIT_EVAL + "\n        continue-on-error: true\n"),
        (AUDIT_EVAL + "\n", AUDIT_EVAL + "\n        shell: bash {0}\n"),
        (AUDIT_EVAL + "\n", AUDIT_EVAL + "\n        if: false\n"),
        (AUDIT_EVAL + "\n        run: |\n", AUDIT_EVAL + "\n        run: |\n          set +e\n"),
        ("    runs-on: ubuntu-24.04\n    timeout-minutes: 15\n    strategy:", None),
        ("  web:\n    runs-on:", "  web:\n    continue-on-error: true\n    runs-on:"),
        ("\n  test:\n", "\n  test:\n    if: false\n"),
        (_RUN_PIP, _RUN_PIP + "\n        if: false\n"),
        (_EVAL_PIP + "\n", _EVAL_PIP + "\n\n        if: false\n"),
        (_EVAL_PIP + "\n", _EVAL_PIP + "\n\n        shell: bash {0}\n"),
        ("\njobs:\n", "\ndefaults:\n  run:\n    shell: true {0}\n\njobs:\n"),
        ("\njobs:\n", "\nenv:\n  PIP_INDEX_URL: https://example.invalid/simple\n\njobs:\n"),
        (_RUN_PIP, _RUN_PIP + "# note\n        if: false\n"),
        (
            "      - run: ruff check .\n",
            '      - run: echo "PIP_INDEX_URL=https://example.invalid/simple" >> "$GITHUB_ENV"\n'
            "      - run: ruff check .\n",
        ),
        (
            "      - run: ruff check .\n",
            "      - run: printf '[global]\\nindex-url = x\\n' > ~/.config/pip/pip.conf\n"
            "      - run: ruff check .\n",
        ),
        (
            "      " + AUDIT_EVAL,
            "      - run: cp /bin/true /tmp/audit/bin/pip-audit\n      " + AUDIT_EVAL,
        ),
        (
            "      - run: ruff check .\n",
            '      - run: echo "PYTHONPATH=/tmp/x" >> "$GITHUB_ENV"\n      - run: ruff check .\n',
        ),
        (
            "      - run: ruff check .\n",
            '      - run: echo /tmp/x >> "$GITHUB_PATH"\n      - run: ruff check .\n',
        ),
        ("python -m venv --clear /tmp/audit", "python -m venv /tmp/audit"),
        ("\n  test:\n", "\n  test:\n    defaults:\n      run:\n        shell: bash {0}\n"),
    ],
)
def test_mutation_a_removed_changed_or_ignorable_audit_is_caught(old: str, new: str | None) -> None:
    ci = _ci()
    assert ci.count(old) == 1, old
    if new is None:  # a job-level continue-on-error
        new = old.replace("    strategy:", "    continue-on-error: true\n    strategy:")
    assert _audit_failures(ci.replace(old, new)) != []


def test_mutation_the_eval_audit_moved_before_the_runtime_audit_is_caught() -> None:
    ci = _ci()
    runtime = AUDIT_RUNTIME + "\n" + AUDIT_STEPS[AUDIT_RUNTIME]
    evaluation = "      " + AUDIT_EVAL + "\n" + AUDIT_STEPS[AUDIT_EVAL]
    assert ci.count(runtime) == 1 and ci.count(evaluation) == 1
    swapped = ci.replace(evaluation, "").replace(
        "      " + runtime, evaluation + "      " + runtime
    )
    assert swapped != ci
    assert _audit_failures(swapped) != []
