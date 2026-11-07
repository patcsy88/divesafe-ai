"""The application's connections pass their own `options`, and psycopg lets that keyword replace
any `options` in the connection string and `PGOPTIONS` in the environment. So a `-c role=...` or
`-c session_authorization=...` put there never reaches the server (ADR 0022 Known gaps; checked
live in tests/integration/test_postgres_guard.py). Offline: psycopg.connect is replaced for the
repository, and a source scan of src/ finds every other connection and requires a fixed `options`
string outside a two-entry allow-list.

Not seen by the scan (deliberate evasion, left to code review): `getattr`, `importlib`, low-level
`psycopg.pq`, and anything outside src/ such as the `psql` calls in docker/ (owner role)."""

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
# Connections allowed without their own `options`, by file and qualified function name, each with
# its reason (ADR 0022 Known gaps). Each must hold exactly one connection call.
NO_OPTIONS_ALLOWED = {
    # The owner's admin connection: the owner needs no protection from itself.
    ("services/postgres.py", "install_schema"),
    # The loader: a fixed `options` is ADR 0024 A3 work; its self-check refuses memberships.
    ("services/kb_common.py", "connect"),
}
# The loader's connection (no `options`) may be used only by the loader's own commands.
LOADER_CONNECT_USERS = {"services/kb_load.py", "services/kb_withdraw.py"}
_POOLS = {"ConnectionPool", "AsyncConnectionPool", "NullConnectionPool", "AsyncNullConnectionPool"}
_CONNECTION_CLASSES = {"Connection", "AsyncConnection", "CrdbConnection", "AsyncCrdbConnection"}
_PSYCOPG_MODULES = {"psycopg", "psycopg.connection", "psycopg.connection_async", "psycopg.crdb"}


def _is_connection_call(call: ast.Call) -> bool:
    """`psycopg.connect(...)`, `psycopg.crdb.connect(...)`, `<...>.Connection.connect(...)` (and
    the async and CockroachDB classes), or a pool. Other spellings are refused at import, or by
    the bare-reference rule, so they cannot reach a call this misses."""
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
        return owner.id in {"psycopg", *_CONNECTION_CLASSES}
    return isinstance(owner, ast.Attribute) and owner.attr in {"crdb", *_CONNECTION_CLASSES}


def _string_constants(tree: ast.Module) -> set[str]:
    """Module-level names bound once to a string literal or an f-string."""
    bound: dict[str, int] = {}
    strings: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        for target in targets:
            if isinstance(target, ast.Name):
                bound[target.id] = bound.get(target.id, 0) + 1
                if _is_fixed_string(value, set()):
                    strings.add(target.id)
    return {name for name in strings if bound[name] == 1}


def _is_fixed_string(value: ast.expr | None, constants: set[str]) -> bool:
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str) and value.value.strip() != ""
    if isinstance(value, ast.JoinedStr):
        return True
    return isinstance(value, ast.Name) and value.id in constants


def _refused_import(node: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root == "psycopg_pool":
                return "imports psycopg_pool"
            if alias.name == "psycopg.crdb":
                return "imports psycopg.crdb"
            if alias.name == "psycopg" and alias.asname not in (None, "psycopg"):
                return "imports psycopg under another name"
        return None
    module = node.module or ""
    if module.split(".")[0] == "psycopg_pool":
        return "imports psycopg_pool"
    names = {alias.name for alias in node.names}
    if module in _PSYCOPG_MODULES and names & {"connect", "crdb", "*", *_CONNECTION_CLASSES}:
        return f"imports a connection from {module}"
    if module == "psycopg" and "crdb" in names:
        return "imports psycopg.crdb"
    if module == "divesafe.services.kb_common" and names & {"connect", "*"}:
        return "imports the loader's connect"
    return None


def connections_without_options(relative: str, source: str) -> list[str]:
    """Every database connection in `source` that does not pass a fixed `options` string (a
    non-empty literal, an f-string, or a module-level string constant) and is not allowed; any
    import that would let a connection escape the call shapes above; any use of
    `psycopg.connect` other than calling it; and any use of the loader's connection outside the
    loader's commands."""
    found: list[str] = []
    tree = ast.parse(source)
    constants = _string_constants(tree)
    called: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(id(node.func))

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = child.name if scope == "<module>" else f"{scope}.{child.name}"
            elif isinstance(child, ast.Lambda):
                name = f"{scope}.<lambda>"
            if isinstance(child, ast.Import | ast.ImportFrom):
                refused = _refused_import(child)
                if refused:
                    found.append(f"{relative}:{child.lineno} {refused}")
            if (
                isinstance(child, ast.Attribute)
                and child.attr == "connect"
                and isinstance(child.value, ast.Name)
                and child.value.id == "psycopg"
                and id(child) not in called
            ):
                found.append(f"{relative}:{child.lineno} uses psycopg.connect without calling it")
            if (
                isinstance(child, ast.Attribute)
                and child.attr == "connect"
                and isinstance(child.value, ast.Name)
                and child.value.id == "kb_common"
                and relative not in LOADER_CONNECT_USERS
            ):
                found.append(f"{relative}:{child.lineno} uses the loader's connect")
            if isinstance(child, ast.Call) and _is_connection_call(child):
                options = [k.value for k in child.keywords if k.arg == "options"]
                fixed = len(options) == 1 and _is_fixed_string(options[0], constants)
                if not fixed and (relative, name) not in NO_OPTIONS_ALLOWED:
                    found.append(f"{relative}:{child.lineno} in {name}")
            visit(child, name)

    visit(tree, "<module>")
    return found


def _sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
    ]


def test_every_database_connection_in_src_passes_its_own_options() -> None:
    """The owner's "no config refusal" decision rests on every app connection overriding
    `options`; a new connection path or a pool would quietly bring DSN and PGOPTIONS back."""
    found = [
        p for relative, source in _sources() for p in connections_without_options(relative, source)
    ]
    assert found == []


def _calls_in(relative: str, qualified: str) -> int:
    tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
    count = 0

    def visit(node: ast.AST, scope: str) -> None:
        nonlocal count
        for child in ast.iter_child_nodes(node):
            name = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = child.name if scope == "<module>" else f"{scope}.{child.name}"
            elif isinstance(child, ast.Lambda):
                name = f"{scope}.<lambda>"
            if isinstance(child, ast.Call) and _is_connection_call(child) and name == qualified:
                count += 1
            visit(child, name)

    visit(tree, "<module>")
    return count


@pytest.mark.parametrize(
    ("relative", "qualified"),
    [
        *sorted(NO_OPTIONS_ALLOWED),
        ("services/postgres.py", "PostgresAssessmentRepository._connect"),
    ],
)
def test_the_scan_sees_exactly_one_connection_where_it_expects_one(
    relative: str, qualified: str
) -> None:
    """Guards the scan and the allow-list: a second connection in an allowed function, or a
    rewrite the scan cannot see, fails here instead of leaving a stale exemption."""
    assert _calls_in(relative, qualified) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\ndef helper(dsn):\n    return psycopg.connect(dsn)\n",
        "import psycopg\ndef helper(dsn):\n    return psycopg.Connection.connect(dsn)\n",
        "import psycopg\nasync def h(d):\n    return await psycopg.AsyncConnection.connect(d)\n",
        "from psycopg_pool import ConnectionPool\n",
        "import psycopg_pool\npool = psycopg_pool.ConnectionPool(conninfo='x')\n",
        "import psycopg\ndef install_schema_helper(d):\n    return psycopg.connect(d)\n",
        "from psycopg import connect\ndef h(d):\n    return connect(d)\n",
        "from psycopg import connect as c\n",
        "from psycopg import Connection\n",
        "import psycopg as pg\ndef h(d):\n    return pg.connect(d)\n",
        "import psycopg\nc = psycopg.connect\n",
        "import functools, psycopg\nc = functools.partial(psycopg.connect, 'x')\n",
        "import psycopg.crdb\n",
        "from psycopg import crdb\n",
        "import psycopg\ndef h(d):\n    return psycopg.crdb.connect(d, options=None)\n",
        "import psycopg\ndef h(d):\n    return psycopg.connect(d, options=None)\n",
        "import psycopg\ndef h(d):\n    return psycopg.connect(d, options='')\n",
        "import psycopg\ndef h(d, cfg):\n    return psycopg.connect(d, options=cfg.x)\n",
        "import psycopg\nO = None\ndef h(d):\n    return psycopg.connect(d, options=O)\n",
        (
            "import psycopg\nO = '-c a=1'\nO = None\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        "import psycopg\ndef h(d):\n    return psycopg.connect(d, kwargs={'options': 'x'})\n",
        "import psycopg\ndef h(d, **o):\n    return psycopg.connect(d, **o)\n",
        "from divesafe.services import kb_common\ndef h(d):\n    return kb_common.connect(d)\n",
        "from divesafe.services.kb_common import connect\n",
        (
            "import psycopg\ndef install_schema(d):\n    psycopg.connect(d)\n"
            "    def inner():\n        return psycopg.connect(d)\n"
        ),
        "import psycopg\ndef install_schema(d):\n    return lambda: psycopg.connect(d)\n",
        (
            "import psycopg\nclass X:\n"
            "    def install_schema(self, d):\n        return psycopg.connect(d)\n"
        ),
    ],
    ids=[
        "bare-connect",
        "connection-classmethod",
        "async",
        "pool-import",
        "pool",
        "near-name",
        "from-import-connect",
        "from-import-alias",
        "from-import-class",
        "module-alias",
        "reference",
        "partial",
        "crdb-import",
        "crdb-from-import",
        "crdb-call",
        "options-none",
        "options-empty",
        "options-expression",
        "options-none-constant",
        "options-rebound",
        "kwargs-not-options",
        "splat",
        "loader-connect-outside-loader",
        "loader-connect-import",
        "nested-in-allowed",
        "lambda-in-allowed",
        "method-named-like-allowed",
    ],
)
def test_mutation_a_connection_without_options_is_found(source: str) -> None:
    assert connections_without_options("services/postgres.py", source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\ndef h(d):\n    return psycopg.connect(d, options='-c a=1')\n",
        "import psycopg\nO = '-c a=1'\ndef h(d):\n    return psycopg.connect(d, options=O)\n",
        "import psycopg\nN = 5\ndef h(d):\n    return psycopg.connect(d, options=f'-c a={N}')\n",
        "import psycopg\ndef install_schema(d):\n    return psycopg.connect(d)\n",
        "from psycopg import sql\nimport psycopg\nx: psycopg.Connection[int]\n",
    ],
    ids=["literal", "constant", "f-string", "allowed", "sql-import-and-annotation"],
)
def test_the_scan_accepts_fixed_options_and_ordinary_code(source: str) -> None:
    assert connections_without_options("services/postgres.py", source) == []


def test_mutation_the_repository_connect_without_options_is_found() -> None:
    source = (SRC / "services/postgres.py").read_text(encoding="utf-8")
    assert source.count(CONNECT) == 1
    line = source[: source.index(CONNECT)].count("\n") + 1
    mutant = source.replace(
        CONNECT,
        "        return psycopg.connect(self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS)\n",
    )
    assert connections_without_options("services/postgres.py", mutant) == [
        f"services/postgres.py:{line} in PostgresAssessmentRepository._connect"
    ]
