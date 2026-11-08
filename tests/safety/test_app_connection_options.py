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


def _bindings(tree: ast.Module) -> dict[str, int]:
    """Every binding of every name anywhere in the module: assignment targets (any nesting,
    augmented, unpacked, walrus, loop and `with` targets), parameters, imports, `global`, and
    function and class names. A name counted once is bound once in the whole module."""
    count: dict[str, int] = {}

    def add(name: str) -> None:
        count[name] = count.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            add(node.id)
        elif isinstance(node, ast.arg):
            add(node.arg)
        elif isinstance(node, ast.alias):
            add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                add(name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) or (
            isinstance(node, ast.ExceptHandler) and node.name
        ):
            add(node.name)  # type: ignore[arg-type]
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            add(node.rest)
        elif isinstance(node, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple):
            add(node.name)
    return count


def _constants(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(strings, scalars): names bound exactly once in the whole module, by a top-level
    assignment to a fixed string, or to an int or str literal."""
    bindings = _bindings(tree)
    once: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and bindings.get(target.id) == 1:
            once[target.id] = value
    scalars = {
        name
        for name, value in once.items()
        if isinstance(value, ast.Constant)
        and isinstance(value.value, int | str)
        and not isinstance(value.value, bool)
    }
    strings = {name for name, value in once.items() if _is_fixed_string(value, set(), scalars)}
    return strings, scalars


def _is_fixed_string(value: ast.expr | None, strings: set[str], scalars: set[str]) -> bool:
    """A non-empty string literal; an f-string with a non-blank literal part whose every
    interpolation is a literal or a module constant bound once; or a string constant."""
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str) and value.value.strip() != ""
    if isinstance(value, ast.JoinedStr):
        literal = "".join(
            v.value
            for v in value.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        parts_ok = all(
            isinstance(v, ast.Constant)
            or (
                isinstance(v, ast.FormattedValue)
                and v.format_spec is None
                and (
                    (isinstance(v.value, ast.Constant) and isinstance(v.value.value, int | str))
                    or (isinstance(v.value, ast.Name) and v.value.id in scalars)
                )
            )
            for v in value.values
        )
        return literal.strip() != "" and parts_ok
    return isinstance(value, ast.Name) and value.id in strings


_LOADER_MODULE = "divesafe.services.kb_common"


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
            if alias.name == _LOADER_MODULE:
                return "imports kb_common by its dotted name"
        return None
    module = node.module or ""
    if module.split(".")[0] == "psycopg_pool":
        return "imports psycopg_pool"
    names = {alias.name for alias in node.names}
    if "*" in names:
        return "imports *"
    if module in _PSYCOPG_MODULES and names & {"connect", "crdb", "*", *_CONNECTION_CLASSES}:
        return f"imports a connection from {module}"
    if module == "psycopg" and "crdb" in names:
        return "imports psycopg.crdb"
    if module == _LOADER_MODULE and names & {"connect", "*"}:
        return "imports the loader's connect"
    if node.level and (module.split(".")[-1] == "kb_common" or "kb_common" in names):
        return "imports kb_common relatively"
    if module == "divesafe.services":
        for alias in node.names:
            if alias.name == "kb_common" and alias.asname not in (None, "kb_common"):
                return "imports kb_common under another name"
    return None


def _is_kb_common(owner: ast.expr) -> bool:
    return (isinstance(owner, ast.Name) and owner.id == "kb_common") or (
        isinstance(owner, ast.Attribute) and owner.attr == "kb_common"
    )


def _is_super_call(owner: ast.expr) -> bool:
    return (
        isinstance(owner, ast.Call)
        and isinstance(owner.func, ast.Name)
        and owner.func.id == "super"
    )


def connections_without_options(relative: str, source: str) -> list[str]:
    """Every database connection in `source` that does not pass a fixed `options` string (see
    `_is_fixed_string`) and is not allowed; any import that would let a connection escape the
    call shapes above; any connection function used other than by calling it; a connection
    class anywhere but an annotation or its own `.connect(...)` call; a `.connect` on any other
    owner but `super()` (called) and the loader's `kb_common`; and any use of the loader's
    connection outside the loader's commands. Any fixed text is accepted as `options`: the scan
    stops settings injected from outside, while `_connect`'s own value is pinned above."""
    found: list[str] = []
    tree = ast.parse(source)
    strings, scalars = _constants(tree)
    called: set[int] = set()
    annotation: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(id(node.func))
        roots: list[ast.expr | None] = []
        if isinstance(node, ast.arg):
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        for root in roots:
            if root is not None:
                annotation.update(id(n) for n in ast.walk(root))

    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]

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
                and _is_connection_call(ast.Call(func=child, args=[], keywords=[]))
                and id(child) not in called
            ):
                found.append(f"{relative}:{child.lineno} uses a connection without calling it")
            if (
                isinstance(child, ast.Attribute)
                and child.attr in _CONNECTION_CLASSES
                and id(child) not in annotation
                and not (
                    isinstance(parent := getattr(child, "_parent", None), ast.Attribute)
                    and parent.attr == "connect"
                    and id(parent) in called
                )
            ):
                found.append(f"{relative}:{child.lineno} uses a connection class as a value")
            if (
                isinstance(child, ast.Attribute)
                and child.attr == "connect"
                and not _is_connection_call(ast.Call(func=child, args=[], keywords=[]))
                and not _is_kb_common(child.value)
                and not (_is_super_call(child.value) and id(child) in called)
            ):
                found.append(f"{relative}:{child.lineno} calls connect on an unrecognised owner")
            if (
                isinstance(child, ast.Attribute)
                and child.attr == "connect"
                and _is_kb_common(child.value)
                and relative not in LOADER_CONNECT_USERS
            ):
                found.append(f"{relative}:{child.lineno} uses the loader's connect")
            if isinstance(child, ast.Call) and _is_connection_call(child):
                options = [k.value for k in child.keywords if k.arg == "options"]
                fixed = len(options) == 1 and _is_fixed_string(options[0], strings, scalars)
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
        "import psycopg\ndef h(d, cfg):\n    return psycopg.connect(d, options=f'{cfg}')\n",
        "import psycopg\ndef h(d, c):\n    return psycopg.connect(d, options=f'-c {c.x}')\n",
        "import psycopg\ndef h(d):\n    return psycopg.connect(d, options=f'')\n",
        (
            "import os, psycopg\nO = f\"-c {os.environ['X']}\"\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\nO += ' -c role=x'\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\nif X:\n    O = None\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\nA, O = 1, None\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\ndef s():\n    global O\n    O = None\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\n"
            "def h(d, O=None):\n    return psycopg.connect(d, options=O)\n"
        ),
        "import psycopg\nC = psycopg.Connection\ndef h(d):\n    return C.connect(d)\n",
        "import psycopg\nc = psycopg.Connection.connect\n",
        "from divesafe.services import kb_common as k\n",
        "import divesafe.services.kb_common\n",
        "from .kb_common import SYNCHRONOUS_COMMIT\n",
        "from . import kb_common\n",
        "import psycopg\nC, D = psycopg.Connection, 1\ndef h(d):\n    return C.connect(d)\n",
        "import psycopg\ndef h(d, C=psycopg.Connection):\n    return C.connect(d)\n",
        "import psycopg\ndef h(d):\n    return g(psycopg.Connection)\n",
        "def h(conn, d):\n    return type(conn).connect(d)\n",
        "def h(conn, d):\n    return conn.__class__.connect(d)\n",
        (
            "from divesafe.services.kb_load import kb_common as k\n"
            "def h(d):\n    return k.connect(d)\n"
        ),
        "import psycopg\ndef h():\n    return psycopg.Connection\n",
        "import psycopg\ndef h(d):\n    return [psycopg.Connection][0].connect(d)\n",
        (
            "import psycopg\nO = '-c a=1'\nmatch X:\n    case O:\n        pass\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\nmatch X:\n    case [*O]:\n        pass\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\nmatch X:\n    case {**O}:\n        pass\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        (
            "import psycopg\nO = '-c a=1'\ndef f[O]():\n    pass\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        "from divesafe.config import *\n",
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
        "fstring-parameter",
        "fstring-attribute",
        "fstring-empty",
        "fstring-constant-from-config",
        "augmented-constant",
        "constant-rebound-in-if",
        "constant-rebound-by-unpacking",
        "constant-rebound-by-global",
        "constant-shadowed-by-parameter",
        "class-reference",
        "classmethod-reference",
        "kb_common-alias",
        "kb_common-dotted",
        "kb_common-relative",
        "kb_common-relative-module",
        "class-in-tuple",
        "class-as-default",
        "class-as-argument",
        "type-of-connection",
        "dunder-class",
        "kb_common-re-export",
        "class-returned",
        "class-in-list",
        "match-capture-rebinds",
        "match-star-rebinds",
        "match-rest-rebinds",
        "type-parameter-rebinds",
        "star-import",
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
        (
            "import psycopg\nN = 'x'\nO = f'-c a={N}'\n"
            "def h(d):\n    return psycopg.connect(d, options=O)\n"
        ),
        "import psycopg\ndef install_schema(d):\n    return psycopg.connect(d)\n",
        "from psycopg import sql\nimport psycopg\nx: psycopg.Connection[int]\n",
        "import http.client\nclass C(http.client.HTTPConnection):\n    def connect(self):\n"
        "        super().connect()\n",
        (
            "import psycopg\ndef h(c: psycopg.Connection[Any]) -> psycopg.AsyncConnection[Any]:\n"
            "    x: psycopg.Connection[Any] = c\n    return x\n"
        ),
        "import psycopg\ndef h(d):\n    return psycopg.Connection.connect(d, options='-c a=1')\n",
    ],
    ids=[
        "literal",
        "constant",
        "f-string",
        "f-string-constant",
        "allowed",
        "sql-and-annotation",
        "super-connect",
        "annotations",
        "classmethod-connect-with-options",
    ],
)
def test_the_scan_accepts_fixed_options_and_ordinary_code(source: str) -> None:
    assert connections_without_options("services/postgres.py", source) == []


def test_an_unloadable_timestamp_is_storage_unavailable_never_a_runtime_error() -> None:
    """psycopg raises NotImplementedError (a RuntimeError, not a psycopg.Error) for a timestamp
    in a non-ISO DateStyle, and PGDATESTYLE reaches the app over `options`. The app must see a
    typed storage error with no driver text (a 503), never an error it may read as shutdown."""
    from divesafe.services import RepositoryUnavailableError

    with pytest.raises(RepositoryUnavailableError) as raised, postgres._storage_errors():
        raise NotImplementedError("can't parse timestamptz with DateStyle 'SQL, DMY': '07/11'")
    assert str(raised.value) == "storage unavailable"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


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
