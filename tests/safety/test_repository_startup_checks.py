"""Start-up asks the repository for the strict checks in production only (ADR 0024)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from divesafe.api import state as state_module
from divesafe.api.state import build_repository
from divesafe.config import Settings

pytestmark = pytest.mark.safety


class _Recorder:
    calls: list[dict[str, Any]] = []
    problems: list[str] = []

    def __init__(self, dsn: str) -> None:
        pass

    def verify(self, **kwargs: Any) -> list[str]:
        _Recorder.calls.append(kwargs)
        return list(_Recorder.problems)


@pytest.fixture(autouse=True)
def _recorder(monkeypatch: pytest.MonkeyPatch) -> type[_Recorder]:
    _Recorder.calls = []
    _Recorder.problems = []
    monkeypatch.setattr(state_module, "PostgresAssessmentRepository", _Recorder)
    return _Recorder


def _settings(environment: str) -> Settings:
    """Built without validation: production settings validation (a real model server, key hashes)
    is covered elsewhere; only `environment` and `database_url` matter to `build_repository`."""
    return Settings.model_construct(
        environment=environment,
        database_url=SecretStr("postgresql://u:p@db.invalid/x"),
    )


def test_production_asks_for_least_privilege_and_a_non_superuser_owner() -> None:
    build_repository(_settings("production"))
    assert _Recorder.calls == [{"least_privilege": True, "owner_not_superuser": True}]


@pytest.mark.parametrize("environment", ["development", "test"])
def test_other_environments_do_not_ask_for_the_strict_checks(environment: str) -> None:
    build_repository(_settings(environment))
    assert _Recorder.calls == [{"least_privilege": False, "owner_not_superuser": False}]


def test_a_problem_from_the_owner_check_stops_start_up() -> None:
    _Recorder.problems = ["the role that owns the assessments table is a superuser"]
    with pytest.raises(RuntimeError, match="unsafe database setup: .*superuser"):
        build_repository(_settings("production"))
