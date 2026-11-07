"""The application's connections pass their own `options`, and psycopg lets that keyword replace
any `options` in the connection string and `PGOPTIONS` in the environment. So a `-c role=...` or
`-c session_authorization=...` put there never reaches the server (ADR 0022 Known gaps; checked
live in tests/integration/test_postgres_guard.py). Offline: psycopg.connect is replaced."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import psycopg.conninfo
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


def test_the_repository_connection_passes_its_own_options(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_the_options_keyword_replaces_options_in_the_connection_string() -> None:
    """The connection-string half of the claim, pinned across psycopg upgrades. `PGOPTIONS` is
    read by libpq, so that half is live-only."""
    merged = psycopg.conninfo.make_conninfo(
        "postgresql://u@db.invalid/x_test?options=-c%20role%3Dother", options=postgres._OPTIONS
    )
    assert psycopg.conninfo.conninfo_to_dict(merged)["options"] == postgres._OPTIONS


SRC = Path(postgres.__file__).resolve().parents[1]
# Connections allowed without their own `options`, each with its reason (ADR 0022 Known gaps).
NO_OPTIONS_ALLOWED = {
    # The owner's admin connection: the owner needs no protection from itself.
    ("services/postgres.py", "install_schema"),
    # The loader: a fixed `options` is ADR 0024 A3 work; its self-check refuses memberships.
    ("services/kb_common.py", "connect"),
}
_POOLS = {"ConnectionPool", "AsyncConnectionPool", "NullConnectionPool", "AsyncNullConnectionPool"}


def _is_connection_call(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id in _POOLS
    if not isinstance(f, ast.Attribute):
        return False
    if f.attr in _POOLS:
        return True
    if f.attr != "connect":
        return False
    owner = f.value
    if isinstance(owner, ast.Name):
        return owner.id in {"psycopg", "Connection", "AsyncConnection"}
    return isinstance(owner, ast.Attribute) and owner.attr in {"Connection", "AsyncConnection"}


def connections_without_options(relative: str, source: str) -> list[str]:
    """Every database connection call in `source` that passes no `options` and is not allowed,
    plus any import of a connection pool (no ADR covers one; it would need `kwargs` options)."""
    found: list[str] = []
    tree = ast.parse(source)

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Import | ast.ImportFrom):
                modules = [a.name for a in child.names] if isinstance(child, ast.Import) else []
                if isinstance(child, ast.ImportFrom) and child.module:
                    modules.append(child.module)
                if any(m.split(".")[0] == "psycopg_pool" for m in modules):
                    found.append(f"{relative}:{child.lineno} imports psycopg_pool")
            if isinstance(child, ast.Call) and _is_connection_call(child):
                passes = any(k.arg in {"options", "kwargs"} for k in child.keywords)
                if not passes and (relative, name) not in NO_OPTIONS_ALLOWED:
                    found.append(f"{relative}:{child.lineno} in {name}")
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_every_database_connection_in_src_passes_its_own_options() -> None:
    """The owner's "no config refusal" decision rests on every app connection overriding
    `options`; a new connection path or a pool would quietly bring DSN and PGOPTIONS back."""
    found = [
        problem
        for path in sorted(SRC.rglob("*.py"))
        for problem in connections_without_options(
            path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8")
        )
    ]
    assert found == []


def test_the_scan_sees_the_known_connections() -> None:
    """Guards the scan itself: it must find the calls it is meant to judge."""
    source = (SRC / "services/postgres.py").read_text(encoding="utf-8")
    calls = [
        n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and _is_connection_call(n)
    ]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\ndef helper(dsn):\n    return psycopg.connect(dsn)\n",
        "import psycopg\ndef helper(dsn):\n    return psycopg.Connection.connect(dsn)\n",
        "import psycopg\nasync def h(d):\n    return await psycopg.AsyncConnection.connect(d)\n",
        "from psycopg_pool import ConnectionPool\n",
        "import psycopg_pool\npool = psycopg_pool.ConnectionPool(conninfo='x')\n",
        "import psycopg\ndef install_schema_helper(d):\n    return psycopg.connect(d)\n",
    ],
    ids=["bare-connect", "connection-classmethod", "async", "pool-import", "pool", "near-name"],
)
def test_mutation_a_connection_without_options_is_found(source: str) -> None:
    assert connections_without_options("services/postgres.py", source) != []


def test_mutation_the_repository_connect_without_options_is_found() -> None:
    source = (SRC / "services/postgres.py").read_text(encoding="utf-8")
    assert source.count(CONNECT) == 1
    mutant = source.replace(
        CONNECT,
        "        return psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS)\n",
    )
    assert connections_without_options("services/postgres.py", mutant) == [
        "services/postgres.py:441 in _connect"
    ]
