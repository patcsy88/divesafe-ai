"""Owner decisions (2026-11-09): `kb_load load` and `kb_load approve` refuse (exit 24) before any
read or write until the ADR 0024 A6 gates are closed, in production AND whenever the loader's
connection string came from DIVESAFE_SECRETS_DIR, whatever DIVESAFE_ENVIRONMENT says
(development, test, empty or unset). The only switch the code offers is the literal
`PRODUCTION_LOAD_GATES_CLOSED`. The check reads only that literal, `settings.production` and
`settings.dsn_from_secrets_dir`; a single environment value (DIVESAFE_ENVIRONMENT included) cannot
lift it while the string comes from the secrets directory, but the environment does decide where
the string comes from (see below).

What this does NOT stop (ADR 0024 A8): an operator with Docker access who replaces the entrypoint,
shadows the module, or hand-builds a connection string in the environment instead of the secrets
directory. The gate stops accidents and casual overrides such as `-e DIVESAFE_ENVIRONMENT=test`.

`withdraw` keeps working, from the secrets directory too. Development and test with
DIVESAFE_LOADER_DATABASE_URL and no secrets directory are unchanged. Offline: a fake connection;
the live database check is in tests/integration/test_kb_load_live.py. Mutations on COPIES."""

from __future__ import annotations

import ast
import inspect
import sys
from collections.abc import Callable
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
DEV_URL = DSN + "?connect_timeout=5"
SECRET_URL = "postgresql://divesafe_loader:Zq9@db/divesafe?sslmode=require&connect_timeout=5\n"
REFUSAL = (24, "refused: production load gates (ADR 0024 A6) are not closed; nothing written")
SRC = Path(inspect.getfile(kb_load))
CHECK = (
    "if (settings.production or settings.dsn_from_secrets_dir)"
    " and not PRODUCTION_LOAD_GATES_CLOSED:"
)
# "" and unset both mean development to the loader settings; each is tested on its own.
UNSET = None
ENVIRONMENTS_WITH_SECRETS_DIR = ["development", "test", "", UNSET]


def _settings(environment: str, corpus: Path, *, from_secrets_dir: bool = False) -> LoaderSettings:
    return LoaderSettings(
        environment=environment,  # type: ignore[arg-type]
        loader_database_url=SecretStr(DSN),
        corpus_target=corpus,
        dsn_from_secrets_dir=from_secrets_dir,
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
    """Any connection or corpus read is recorded and stops the run: the refusal must come before
    both."""
    seen: list[str] = []

    def record(what: str) -> Any:
        seen.append(what)
        raise AssertionError(f"{what} before the gate")

    monkeypatch.setattr(kb_common, "connect", lambda dsn: record("connect"))
    monkeypatch.setattr(kb_load, "_read", lambda target: record("read"))
    return seen


def _secrets_dir(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / secret_file_name("loader_database_url")).write_text(SECRET_URL)
    return secrets


def _environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str | None, **values: str
) -> None:
    """A clean loader environment in a directory without a .env file."""
    monkeypatch.chdir(tmp_path)
    for key in ("DIVESAFE_ENVIRONMENT", "DIVESAFE_SECRETS_DIR", "DIVESAFE_LOADER_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    if environment is not None:
        monkeypatch.setenv("DIVESAFE_ENVIRONMENT", environment)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


# --- checks, shared by the tests and the mutation tests ------------------------------------------


def refuses_in_production(module: Any, corpus: tuple[Path, str], seen: list[str]) -> None:
    for argv in _argvs(corpus[1]):
        assert _run(module, argv, _settings("production", corpus[0])) == REFUSAL, argv[0]
    assert seen == [], "the refusal must come before any corpus read or connection"


def refuses_with_a_secrets_dir_dsn(module: Any, corpus: tuple[Path, str], seen: list[str]) -> None:
    for environment in ("development", "test"):
        settings = _settings(environment, corpus[0], from_secrets_dir=True)
        for argv in _argvs(corpus[1]):
            assert _run(module, argv, settings) == REFUSAL, (environment, argv[0])
    assert seen == [], "the refusal must come before any corpus read or connection"


def main_refuses_from_a_secrets_dir(
    main: Callable[[list[str]], Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    corpus: tuple[Path, str],
    seen: list[str],
) -> None:
    """Through `_main` and the real loader settings: every environment value, with the
    connection string in a secrets directory."""
    secrets = _secrets_dir(tmp_path)
    for environment in ENVIRONMENTS_WITH_SECRETS_DIR:
        _environment(
            monkeypatch,
            tmp_path,
            environment,
            DIVESAFE_SECRETS_DIR=str(secrets),
            DIVESAFE_CORPUS_TARGET=str(corpus[0]),
        )
        for argv in _argvs(corpus[1]):
            outcome = main(argv)
            assert (outcome.status, outcome.message) == REFUSAL, (environment, argv[0])
    assert seen == [], "the refusal must come before any corpus read or connection"


def unchanged_without_a_secrets_dir(
    module: Any, monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str]
) -> None:
    for environment in ("development", "test"):
        for argv, expected in zip(
            _argvs(corpus[1]), (kb_load.OK_LOAD, kb_load.OK_APPROVE), strict=True
        ):
            fake = LoadFakeConnection()
            monkeypatch.setattr(kb_common, "connect", lambda dsn, fake=fake: fake)
            monkeypatch.setattr(sys, "stdout", Out(fake))
            assert _run(module, argv, _settings(environment, corpus[0])) == (
                expected.status,
                expected.message,
            ), (environment, argv[0])
            assert fake.committed_inserts > 0


def withdraw_works_from_a_secrets_dir(
    run_withdraw: Callable[[list[str]], Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment: str | None,
) -> None:
    secrets = _secrets_dir(tmp_path)
    _environment(monkeypatch, tmp_path, environment, DIVESAFE_SECRETS_DIR=str(secrets))
    fake = FakeConnection()
    install(monkeypatch, fake)
    assert run_withdraw(["withdraw", "site-guide", "2"]) == kb_withdraw.OK
    assert fake.committed_inserts == 1


def the_check_reads_only_the_literal_and_two_settings_fields(source: str) -> None:
    """A pin on the source: the constant is assigned once, at module level, to the literal False;
    the only other use is the first statement of `run`, which reads `settings.production`,
    `settings.dsn_from_secrets_dir` and the constant, nothing else. The module reads no
    environment itself."""
    tree = ast.parse(source)
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
    assert ast.unparse(first.test) == (
        "(settings.production or settings.dsn_from_secrets_dir)"
        " and (not PRODUCTION_LOAD_GATES_CLOSED)"
    )
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
    assert not names & {"environ", "getenv", "environb", "putenv", "__import__"}
    assert "global PRODUCTION_LOAD_GATES_CLOSED" not in source


def the_field_is_set_only_from_where_the_dsn_was_found(source: str) -> None:
    """In the loader settings: `dsn_from_secrets_dir` is passed once, as `bool(location)`, and
    `location` is assigned once, from DIVESAFE_SECRETS_DIR, which is also what selects the secret
    file path. No environment value names the field, and there is no key for the gate."""
    tree = ast.parse(source)
    passed = [
        n for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "dsn_from_secrets_dir"
    ]
    assert [ast.unparse(k.value) for k in passed] == ["bool(location)"]
    location = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "location" for t in n.targets)
    ]
    assert [ast.unparse(n.value) for n in location] == ["env.get(SECRETS_DIR_KEY)"]
    branch = next(
        n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == "location"
    )
    assert ast.unparse(branch.body[0]).startswith("url = _url_from_secrets_dir(")
    assert "GATE" not in source.upper()
    # The field's name appears only as the model field and the keyword above (and in comments).
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    assert code.count("dsn_from_secrets_dir") == 2
    assert "DSN_FROM_SECRETS_DIR" not in code


# --- the tests -----------------------------------------------------------------------------------


def test_load_and_approve_refuse_in_production_before_any_read_or_connection(
    corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    refuses_in_production(kb_load, corpus, no_reads)


def test_load_and_approve_refuse_with_a_secrets_dir_dsn_in_any_environment(
    corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    refuses_with_a_secrets_dir_dsn(kb_load, corpus, no_reads)


def test_the_refusal_is_a_fixed_row_of_the_exit_table() -> None:
    assert (kb_load.FAILED_GATES_OPEN.status, kb_load.FAILED_GATES_OPEN.message) == REFUSAL
    assert kb_load.FAILED_GATES_OPEN in kb_load.EXIT_TABLE
    assert kb_load.PRODUCTION_LOAD_GATES_CLOSED is False


def test_the_refusal_also_holds_through_main_with_the_production_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    """The real settings path: production environment, secrets directory, corpus target. Every
    plausible variable that might be read to lift the gate is set; none does."""
    secrets = _secrets_dir(tmp_path)
    _environment(
        monkeypatch,
        tmp_path,
        "production",
        DIVESAFE_SECRETS_DIR=str(secrets),
        DIVESAFE_CORPUS_TARGET=str(corpus[0]),
        DIVESAFE_PRODUCTION_LOAD_GATES_CLOSED="true",
        PRODUCTION_LOAD_GATES_CLOSED="1",
        DIVESAFE_LOAD_GATES_CLOSED="true",
        DIVESAFE_DSN_FROM_SECRETS_DIR="false",
    )
    for argv in _argvs(corpus[1]):
        assert kb_load._main(argv) == kb_load.FAILED_GATES_OPEN, argv[0]
    assert no_reads == []


def test_main_refuses_with_a_secrets_dir_whatever_the_environment_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    """`-e DIVESAFE_ENVIRONMENT=development` (or test, or empty, or removing it) on the compose
    loader no longer lifts the gate: the string still comes from the secrets directory."""
    main_refuses_from_a_secrets_dir(kb_load._main, monkeypatch, tmp_path, corpus, no_reads)


def test_the_settings_record_where_the_dsn_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = _secrets_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    for environment in ("development", "test", "", "production"):
        env = {"DIVESAFE_SECRETS_DIR": str(secrets), "DIVESAFE_ENVIRONMENT": environment}
        assert loader_settings.load_loader_settings(env).dsn_from_secrets_dir is True
    for environment in ("development", "test", ""):
        env = {"DIVESAFE_LOADER_DATABASE_URL": DEV_URL, "DIVESAFE_ENVIRONMENT": environment}
        assert loader_settings.load_loader_settings(env).dsn_from_secrets_dir is False
    # A value in the environment that looks like the field is not read (and not allowed by the
    # model either: settings are built from code, never from an environment mapping).
    env = {"DIVESAFE_LOADER_DATABASE_URL": DEV_URL, "DIVESAFE_DSN_FROM_SECRETS_DIR": "true"}
    assert loader_settings.load_loader_settings(env).dsn_from_secrets_dir is False
    env = {"DIVESAFE_SECRETS_DIR": str(secrets), "DIVESAFE_DSN_FROM_SECRETS_DIR": "false"}
    assert loader_settings.load_loader_settings(env).dsn_from_secrets_dir is True


def test_the_gate_check_reads_only_the_literal_and_how_the_settings_found_the_dsn() -> None:
    """Not a claim that nothing can lift the gate: see the module docstring and ADR 0024 A8."""
    the_check_reads_only_the_literal_and_two_settings_fields(SRC.read_text())
    the_field_is_set_only_from_where_the_dsn_was_found(inspect.getsource(loader_settings))
    assert set(LoaderSettings.model_fields) == {
        "environment",
        "loader_database_url",
        "corpus_target",
        "dsn_from_secrets_dir",
    }
    assert LoaderSettings.model_fields["dsn_from_secrets_dir"].default is False


@pytest.mark.parametrize("environment", ["production", "development", "test", "", UNSET])
def test_withdraw_still_works_from_the_secrets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str | None
) -> None:
    """Through withdraw's own entry point: it writes and exits 0, in every environment."""
    withdraw_works_from_a_secrets_dir(kb_withdraw._run, monkeypatch, tmp_path, environment)
    source = inspect.getsource(kb_withdraw)
    assert "PRODUCTION_LOAD_GATES_CLOSED" not in source
    assert "dsn_from_secrets_dir" not in source


def test_without_a_secrets_dir_load_and_approve_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str]
) -> None:
    unchanged_without_a_secrets_dir(kb_load, monkeypatch, corpus)


@pytest.mark.parametrize("environment", ["development", "test", "", UNSET])
def test_main_without_a_secrets_dir_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corpus: tuple[Path, str],
    environment: str | None,
) -> None:
    """Development and test with DIVESAFE_LOADER_DATABASE_URL, through `_main`."""
    _environment(
        monkeypatch,
        tmp_path,
        environment,
        DIVESAFE_LOADER_DATABASE_URL=DEV_URL,
        DIVESAFE_CORPUS_TARGET=str(corpus[0]),
    )
    for argv, expected in zip(
        _argvs(corpus[1]), (kb_load.OK_LOAD, kb_load.OK_APPROVE), strict=True
    ):
        fake = LoadFakeConnection()
        monkeypatch.setattr(kb_common, "connect", lambda dsn, fake=fake: fake)
        monkeypatch.setattr(sys, "stdout", Out(fake))
        assert kb_load._main(argv) == expected, argv[0]
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
    settings = _settings("production", corpus[0], from_secrets_dir=True)
    assert _run(kb_load, _argvs(corpus[1])[0], settings)[0] == 0
    assert _run(kb_load, ["load"], settings)[0] == 15


# --- mutations ----------------------------------------------------------------------------------
# Every mutant of the round-2 list (9 + withdraw) and of the round-3 list is here, run on copies.

GATED = "(settings.production or settings.dsn_from_secrets_dir)"


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        (
            "gate open",
            "PRODUCTION_LOAD_GATES_CLOSED = False",
            "PRODUCTION_LOAD_GATES_CLOSED = True",
        ),
        ("check removed", f"    {CHECK}\n        return FAILED_GATES_OPEN", "    pass"),
        ("environment ignored", GATED, "settings.dsn_from_secrets_dir"),
        ("load only", GATED, f"{GATED} and request.command == 'load'"),
        ("approve only", GATED, f"{GATED} and request.command == 'approve'"),
        (
            "message changed",
            "refused: production load gates (ADR 0024 A6) are not closed; nothing written",
            "refused: production load gates are not closed",
        ),
    ],
)
def test_mutation_a_weaker_production_gate_is_caught(
    label: str, old: str, new: str, corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    mutant = mutated_module(kb_load, (old, new))
    with pytest.raises(AssertionError):
        refuses_in_production(mutant, corpus, no_reads)
    assert label


def test_mutation_a_check_after_the_connection_is_caught(
    corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    mutant = mutated_module(
        kb_load,
        (f"    {CHECK}\n        return FAILED_GATES_OPEN  # first: no corpus read", "    #"),
        (
            "    try:\n        return _run_on(conn, request, corpus, checked, deadline)",
            f"    {CHECK}\n        return FAILED_GATES_OPEN\n"
            "    try:\n        return _run_on(conn, request, corpus, checked, deadline)",
        ),
    )
    with pytest.raises(AssertionError):
        refuses_in_production(mutant, corpus, no_reads)


@pytest.mark.parametrize(
    ("label", "new"),
    [
        ("secrets-dir condition dropped", "settings.production"),
        ("keyed on the environment again", "(settings.environment != 'development')"),
        ("keyed on the environment, test exempt", "(settings.environment not in ('test',))"),
        (
            "secrets dir only in production",
            "(settings.production or (settings.dsn_from_secrets_dir and settings.production))",
        ),
    ],
)
def test_mutation_a_gate_that_trusts_the_environment_again_is_caught(
    label: str, new: str, corpus: tuple[Path, str], no_reads: list[str]
) -> None:
    mutant = mutated_module(kb_load, (GATED, new))
    with pytest.raises(AssertionError):
        refuses_with_a_secrets_dir_dsn(mutant, corpus, no_reads)
    assert label


@pytest.mark.parametrize(
    ("label", "new"),
    [
        ("field never set", ""),
        ("field from the environment name", 'dsn_from_secrets_dir=name == "production",'),
        (
            "field from an environment value",
            'dsn_from_secrets_dir=env.get("DIVESAFE_DSN_FROM_SECRETS_DIR", "") == "true",',
        ),
    ],
)
def test_mutation_settings_that_lose_where_the_dsn_came_from_are_caught(
    label: str,
    new: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corpus: tuple[Path, str],
    no_reads: list[str],
) -> None:
    mutant = mutated_module(loader_settings, ("dsn_from_secrets_dir=bool(location),", new))
    monkeypatch.setattr(kb_load, "load_loader_settings", mutant.load_loader_settings)
    monkeypatch.setattr(kb_load, "LoaderSettings", mutant.LoaderSettings)
    with pytest.raises(AssertionError):
        main_refuses_from_a_secrets_dir(kb_load._main, monkeypatch, tmp_path, corpus, no_reads)
    with pytest.raises(AssertionError):
        the_field_is_set_only_from_where_the_dsn_was_found(
            inspect.getsource(loader_settings).replace("dsn_from_secrets_dir=bool(location),", new)
        )
    assert label


def test_mutation_a_gate_also_outside_production_and_the_secrets_dir_is_caught(
    monkeypatch: pytest.MonkeyPatch, corpus: tuple[Path, str]
) -> None:
    mutant = mutated_module(kb_load, (GATED, "True"))
    with pytest.raises(AssertionError):
        unchanged_without_a_secrets_dir(mutant, monkeypatch, corpus)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        (
            "from an environment variable",
            "PRODUCTION_LOAD_GATES_CLOSED = False",
            "PRODUCTION_LOAD_GATES_CLOSED = __import__('os').environ.get('X') == 'true'",
        ),
        (
            "assigned again in a function",
            "def run(request: Request, settings: LoaderSettings) -> Outcome:\n",
            "def run(request: Request, settings: LoaderSettings) -> Outcome:\n"
            "    global PRODUCTION_LOAD_GATES_CLOSED\n",
        ),
        ("an extra condition", GATED, f"{GATED} and settings.corpus_target is not None"),
    ],
)
def test_mutation_the_source_pin_has_teeth(label: str, old: str, new: str) -> None:
    source = SRC.read_text()
    assert source.count(old) == 1, label
    with pytest.raises(AssertionError):
        the_check_reads_only_the_literal_and_two_settings_fields(source.replace(old, new))


def test_mutation_a_gated_withdraw_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = mutated_module(
        kb_withdraw,
        (
            "    return run_withdraw(args[1:], settings.loader_database_url.get_secret_value())",
            "    if settings.dsn_from_secrets_dir:\n"
            "        return FAILED_CONFIGURATION\n"
            "    return run_withdraw(args[1:], settings.loader_database_url.get_secret_value())",
        ),
    )
    with pytest.raises(AssertionError):
        withdraw_works_from_a_secrets_dir(mutant._run, monkeypatch, tmp_path, "development")
