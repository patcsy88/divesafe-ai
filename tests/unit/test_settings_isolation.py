"""The autouse fixture in tests/conftest.py keeps a developer's shell and .env out of every test."""

from __future__ import annotations

from pathlib import Path

import pytest

from divesafe.config import Settings


def test_a_dotenv_file_in_the_working_directory_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("DIVESAFE_LOG_LEVEL=not-a-level\n")
    monkeypatch.chdir(tmp_path)
    assert Settings().log_level != "not-a-level"


def test_a_shell_variable_in_any_case_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    assert not [n for n in os.environ if n.upper().startswith("DIVESAFE_LOG")]
