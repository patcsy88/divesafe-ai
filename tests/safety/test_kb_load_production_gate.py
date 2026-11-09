"""Owner decision (2026-11-09): in production, `kb_load load` and `kb_load approve` refuse before
any write until the ADR 0024 A6 gates are closed. Lifted only by a reviewed change of the literal
`PRODUCTION_LOAD_GATES_CLOSED`; no setting, environment variable or file can lift it. `withdraw`
keeps working in production. Outside production nothing changes. Offline: a fake connection;
the live database check is in tests/integration/test_kb_load_live.py. Mutations on COPIES."""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from tests.safety.kb_corpus import write_corpus
from tests.safety.kb_fakes import (
    STORED_HASH,
    FakeConnection,
    LoadFakeConnection,
    Out,
    install,
    mutated_module,
)

from divesafe.config import loader_settings
from divesafe.config.loader_settings import LoaderSettings
from divesafe.config.secret_files import secret_file_name
from divesafe.services import kb_common, kb_load, kb_withdraw

pytestmark = pytest.mark.safety

DSN = "postgresql://u:never-echoed@127.0.0.1:1/x_test"
REFUSAL = (24, "refused: production load gates (ADR 0024 A6) are not closed; nothing written")
SRC = Path(inspect.getfile(kb_load))


def _settings(environment: str, corpus: Path) -> LoaderSettings:
    return LoaderSettings(
        environment=environment,  # type: ignore[arg-type]
        loader_database_url=SecretStr(DSN),
        corpus_target=corpus,
    )


def _argvs(manifest_hash: str) -> list[list[str]]:
    return [
        ["load", "--approved-manifest-sha256", manifest_hash],
        ["approve", "site-guide", "1", "--approved-manifest-sha256", STORED_HASH],
    ]


def _run(module: Any, argv: list[str], settings: LoaderSettings) -> tuple[int, str]:
    result = module.run(module.parse_arguments(argv), settings)
    return (result.status, result.message)


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "corpus"
    return root, write_corpus(root)


@pytest.fixture
def no_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any connection or corpus read is recorded: the refusal must come before both."""
    seen: list[str] = []
    monkeypatch.setattr(kb_common, "connect", lambda dsn: seen.append("connect"))
    monkeypatch.setattr(kb_load, "_read", lambda target: seen.append("read"))
    return seen


def refuses_in_production(module: Any, corpus: tuple[Path, str], seen: list[str]) -> None:
    for argv in _argvs(corpus[1]):
        assert _run(module, argv, _settings("production", corpus[0])) == REFUSAL, argv[0]
    assert seen == [], "the refusal must come before any corpus read or connection"


def test_load_and_approve_refuse_in_production_before_any_read_or_connection(
    corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    refuses_in_production(kb_load, corpus, no_reads)


def test_the_refusal_is_a_fixed_row_of_the_exit_table() -> None:
    assert (kb_load.FAILED_GATES_OPEN.status, kb_load.FAILED_GATES_OPEN.message) == REFUSAL
    assert kb_load.FAILED_GATES_OPEN in kb_load.EXIT_TABLE
    assert kb_load.PRODUCTION_LOAD_GATES_CLOSED is False


def test_the_refusal_also_holds_through_main_with_the_production_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    """The real settings path: production environment, secrets directory, corpus target. Every
    plausible variable that might be read to lift the gate is set; none does."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / secret_file_name("loader_database_url")).write_text(
        "postgresql://divesafe_loader:Zq9@db/divesafe?sslmode=require&connect_timeout=5\n"
    )
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "DIVESAFE_ENVIRONMENT": "production",
        "DIVESAFE_SECRETS_DIR": str(secrets),
        "DIVESAFE_CORPUS_TARGET": str(corpus[0]),
        "DIVESAFE_PRODUCTION_LOAD_GATES_CLOSED": "true",
        "PRODUCTION_LOAD_GATES_CLOSED": "1",
        "DIVESAFE_LOAD_GATES_CLOSED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    for argv in _argvs(corpus[1]):
        assert kb_load._main(argv) == kb_load.FAILED_GATES_OPEN, argv[0]
    assert no_reads == []


def test_no_setting_environment_variable_or_file_can_lift_the_gate() -> None:
    """A pin on the source: the constant is assigned once, at module level, to the literal False;
    the only other use is the first statement of `run`, which reads nothing but
    `settings.production` and the constant. The module reads no environment itself, and the
    loader settings have no field and no key for the gate."""
    tree = ast.parse(SRC.read_text())
    assigns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and "PRODUCTION_LOAD_GATES_CLOSED"
        in ast.dump(n.targets[0] if isinstance(n, ast.Assign) else n.target)
    ]
    assert len(assigns) == 1 and assigns[0] in tree.body
    value = assigns[0].value
    assert isinstance(value, ast.Constant) and value.value is False
    uses = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "PRODUCTION_LOAD_GATES_CLOSED"
    ]
    assert len(uses) == 2  # the assignment and the check in run()
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    first = run.body[0]
    assert isinstance(first, ast.If)
    assert ast.unparse(first.test) == "settings.production and (not PRODUCTION_LOAD_GATES_CLOSED)"
    assert ast.unparse(first.body[0]) == "return FAILED_GATES_OPEN"
    for module in ("os", "dotenv"):
        assert module not in {
            a.name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in n.names
        } | {getattr(n, "module", None) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not names & {"environ", "getenv", "environb", "putenv"}
    assert "global PRODUCTION_LOAD_GATES_CLOSED" not in SRC.read_text()
    assert set(LoaderSettings.model_fields) == {
        "environment",
        "loader_database_url",
        "corpus_target",
    }
    assert "GATE" not in inspect.getsource(loader_settings).upper()


def test_withdraw_still_works_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through withdraw's own entry point with the production settings: it writes and exits 0."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / secret_file_name("loader_database_url")).write_text(
        "postgresql://divesafe_loader:Zq9@db/divesafe?sslmode=require&connect_timeout=5\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIVESAFE_ENVIRONMENT", "production")
    monkeypatch.setenv("DIVESAFE_SECRETS_DIR", str(secrets))
    fake = FakeConnection()
    install(monkeypatch, fake)
    assert kb_withdraw._run(["withdraw", "site-guide", "2"]) == kb_withdraw.OK
    assert fake.committed_inserts == 1
    assert "PRODUCTION_LOAD_GATES_CLOSED" not in inspect.getsource(kb_withdraw)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_outside_production_load_and_approve_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str], environment: str
) -> None:
    for argv, expected in zip(
        _argvs(corpus[1]), (kb_load.OK_LOAD, kb_load.OK_APPROVE), strict=True
    ):
        fake = LoadFakeConnection()
        monkeypatch.setattr(kb_common, "connect", lambda dsn, fake=fake: fake)
        monkeypatch.setattr(sys, "stdout", Out(fake))
        assert _run(kb_load, argv, _settings(environment, corpus[0])) == (
            expected.status,
            expected.message,
        )
        assert fake.committed_inserts > 0


def test_closing_the_gates_in_code_lets_production_reach_the_hash_rules(
    monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str]
) -> None:
    """Shows what the reviewed change will do: the gate is the only thing in front; the existing
    production rules (hash required, no development flag) then apply as before."""
    monkeypatch.setattr(kb_load, "PRODUCTION_LOAD_GATES_CLOSED", True)
    fake = LoadFakeConnection()
    monkeypatch.setattr(kb_common, "connect", lambda dsn: fake)
    monkeypatch.setattr(sys, "stdout", Out(fake))
    assert _run(kb_load, _argvs(corpus[1])[0], _settings("production", corpus[0]))[0] == 0
    assert _run(kb_load, ["load"], _settings("production", corpus[0]))[0] == 15


# --- mutations ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        (
            "gate open",
            "PRODUCTION_LOAD_GATES_CLOSED = False",
            "PRODUCTION_LOAD_GATES_CLOSED = True",
        ),
        (
            "check removed",
            "    if settings.production and not PRODUCTION_LOAD_GATES_CLOSED:\n"
            "        return FAILED_GATES_OPEN",
            "    pass",
        ),
        (
            "environment ignored",
            "if settings.production and not PRODUCTION_LOAD_GATES_CLOSED:",
            "if False and not PRODUCTION_LOAD_GATES_CLOSED:",
        ),
        (
            "load only",
            "if settings.production and not PRODUCTION_LOAD_GATES_CLOSED:",
            "if settings.production and request.command == 'load'"
            " and not PRODUCTION_LOAD_GATES_CLOSED:",
        ),
        (
            "approve only",
            "if settings.production and not PRODUCTION_LOAD_GATES_CLOSED:",
            "if settings.production and request.command == 'approve'"
            " and not PRODUCTION_LOAD_GATES_CLOSED:",
        ),
    ],
)
def test_mutation_a_weaker_gate_is_caught(
    label: str, old: str, new: str, corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    mutant = mutated_module(kb_load, (old, new))
    with pytest.raises(AssertionError):
        refuses_in_production(mutant, corpus, no_reads)
    assert label
