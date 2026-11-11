"""tests/hand_run/a7_seed_knowledge.sh refuses anything but a throwaway compose project, and
refuses unless exactly one db container of that project runs. A fake `docker` on PATH records
every call; a refusal must make none (or, for the container count, only the `ps`)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.safety

SCRIPT = Path(__file__).parents[1] / "hand_run" / "a7_seed_knowledge.sh"
FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" | tr '\\n' ' ' >>"$DOCKER_LOG"
printf '\\n' >>"$DOCKER_LOG"
case "$1" in
  ps) printf '%s' "$FAKE_PS" ;;
  exec) cat >/dev/null ;;
esac
"""


def _run(tmp_path: Path, project: str | None, ps: str = "abc123\\n") -> tuple[int, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "DOCKER_LOG": str(log),
        "FAKE_PS": ps.replace("\\n", "\n"),
        "HOME": os.environ.get("HOME", "/"),
    }
    if project is not None:
        env["DIVESAFE_COMPOSE_PROJECT"] = project
    result = subprocess.run(  # noqa: S603
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    return result.returncode, log.read_text().splitlines()


@pytest.mark.parametrize(
    "project",
    [
        None,
        "",
        "divesafe-prod",
        "divesafe",
        "throwaway-",
        "throwaway-divesafe-prod",
        "Throwaway-a7",
        "throwaway-a7_x",
        "throwaway-a7 x",
        "throwaway-a7;ls",
        "xthrowaway-a7",
        "-p throwaway-a7",
    ],
)
def test_a_project_that_is_not_a_throwaway_one_is_refused_before_docker(
    tmp_path: Path, project: str | None
) -> None:
    status, calls = _run(tmp_path, project)
    assert status == 2 and calls == [], project


@pytest.mark.parametrize("ps", ["", "abc123\\ndef456\\n"])
def test_not_exactly_one_db_container_is_refused_without_exec(tmp_path: Path, ps: str) -> None:
    status, calls = _run(tmp_path, "throwaway-a7", ps)
    assert status == 2
    assert len(calls) == 1 and calls[0].startswith("ps ")
    assert "label=com.docker.compose.project=throwaway-a7" in calls[0]


def test_a_throwaway_project_execs_psql_as_the_owner_in_its_db_container(tmp_path: Path) -> None:
    status, calls = _run(tmp_path, "throwaway-a7")
    assert status == 0
    assert len(calls) == 2 and calls[1].startswith("exec -i abc123 sh -c ")
    assert "-U divesafe_owner" in calls[1] and "ON_ERROR_STOP=1" in calls[1]


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("any project", "  throwaway-*) ;;", "  *) ;;"),
        ("prod name allowed", "| *divesafe-prod*)", ")"),
        ("count not checked", 'if [ "$count" -ne 1 ]; then', "if false; then"),
    ],
)
def test_mutation_a_weaker_script_is_caught(
    label: str, old: str, new: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = SCRIPT.read_text()
    assert source.count(old) == 1, label
    mutant = tmp_path / "hand_run" / SCRIPT.name
    mutant.parent.mkdir()
    mutant.write_text(source.replace(old, new))
    (mutant.parent / "a7_seed_knowledge.sql").write_text("-- empty\n")
    monkeypatch.setattr(f"{__name__}.SCRIPT", mutant)
    with pytest.raises(AssertionError):
        for project in (None, "divesafe-prod", "throwaway-divesafe-prod"):
            test_a_project_that_is_not_a_throwaway_one_is_refused_before_docker(
                tmp_path / project if project else tmp_path / "none", project
            )
        test_not_exactly_one_db_container_is_refused_without_exec(tmp_path / "two", "a\\nb\\n")
