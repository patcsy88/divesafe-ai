"""The application's connections pass their own `options`, and psycopg lets that keyword replace
any `options` in the connection string and `PGOPTIONS` in the environment. So a `-c role=...` or
`-c session_authorization=...` put there never reaches the server (ADR 0022 Known gaps; checked
live in tests/integration/test_postgres_guard.py). Offline: psycopg.connect is replaced."""

from __future__ import annotations

from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import postgres

pytestmark = pytest.mark.safety

CONNECT = (
    "        return psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, "
    "options=_OPTIONS)\n"
)


def options_used(module: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    seen: list[dict[str, Any]] = []

    def connect(dsn: str, **kwargs: Any) -> object:
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(module.psycopg, "connect", connect)
    module.PostgresAssessmentRepository(
        "postgresql://u@db.invalid/x_test?options=-c%20role%3Dother"
    )._connect()
    assert len(seen) == 1
    return seen[0].get("options")


def test_every_app_connection_passes_its_own_options(monkeypatch: pytest.MonkeyPatch) -> None:
    options = options_used(postgres, monkeypatch)
    assert options == postgres._OPTIONS
    # Only the two timeouts: nothing that could set a role or another setting.
    assert options.split() == [
        "-c",
        f"statement_timeout={postgres.STATEMENT_TIMEOUT_MS}",
        "-c",
        f"lock_timeout={postgres.LOCK_TIMEOUT_MS}",
    ]


@pytest.mark.parametrize(
    "new",
    [
        "        return psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS)\n",
        "        return psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, "
        "options=None)\n",
    ],
    ids=["options-dropped", "options-none"],
)
def test_mutation_a_connection_without_its_own_options_is_caught(
    monkeypatch: pytest.MonkeyPatch, new: str
) -> None:
    mutant = mutated_module(postgres, (CONNECT, new))
    assert options_used(mutant, monkeypatch) != postgres._OPTIONS
