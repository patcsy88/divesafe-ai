"""The dev tool `divesafe.services.kb_pins` (generates the pins of ADR 0022 condition 9). Offline,
with a fake connection: no database. It must (a) refuse APPLY unless `public.schema_migrations` is
absent (exit 3, fixed text, nothing applied), (b) connect with lock and statement
timeouts, (c) never print the DSN, (d) print the function hashes and one `tgtype` line per
trigger. Mutations are run on COPIES of the module.

EQUIVALENT MUTANTS (not tested): the order of the stdout lines between functions and triggers is
asserted, but `conn.rollback()` at the end of a read-only run is not observable offline beyond
being called; changing `connect_timeout=5` to another number is not asserted (only that one is
set)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.safety.kb_catalogue_fake import Catalog, Rows, expected_tgtype
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_pins, migrations
from divesafe.services.migrations import KNOWLEDGE_FUNCTION_SHA256, KNOWLEDGE_TRIGGERS, MIGRATIONS

pytestmark = pytest.mark.safety

SECRET_DSN = "postgresql://scratch_user:never-printed-secret@db.invalid:5432/scratch_test"
REFUSAL = "refused: APPLY needs an empty scratch database\n"


class PinConnection:
    def __init__(self, *, has_history: bool, deny: bool = False) -> None:
        self.has_history = has_history
        self.catalog = Catalog()
        self.catalog.deny = deny
        self.executed: list[str] = []
        self.rollbacks = 0
        self.read_only = False
        self.connect_args: tuple[tuple[Any, ...], dict[str, Any]] = ((), {})

    def __enter__(self) -> PinConnection:
        return self

    def __exit__(self, *rest: object) -> None:
        return None

    def rollback(self) -> None:
        self.rollbacks += 1

    def execute(self, query: Any, params: Any = None) -> Rows:
        text = str(query)
        self.executed.append(text)
        if "to_regclass('public.schema_migrations')" in text:
            return Rows([("schema_migrations",)] if self.has_history else [(None,)])
        if any(m.sql == text for m in MIGRATIONS):
            return Rows([])
        answer = self.catalog.answer(text, params)
        return answer if answer is not None else Rows([])


def install(monkeypatch: pytest.MonkeyPatch, module: Any, conn: PinConnection) -> None:
    def connect(*args: Any, **kwargs: Any) -> PinConnection:
        conn.connect_args = (args, kwargs)
        return conn

    monkeypatch.setattr(module.psycopg, "connect", connect)


def run(
    monkeypatch: pytest.MonkeyPatch,
    conn: PinConnection,
    capsys: pytest.CaptureFixture[str],
    *,
    apply: bool,
    module: Any = kb_pins,
) -> tuple[int, str, str]:
    install(monkeypatch, module, conn)
    monkeypatch.setenv("DIVESAFE_PIN_DSN", SECRET_DSN)
    if apply:
        monkeypatch.setenv("DIVESAFE_PIN_APPLY", "1")
    else:
        monkeypatch.delenv("DIVESAFE_PIN_APPLY", raising=False)
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- APPLY is refused unless the database is empty --------------------------------------------


def test_apply_is_refused_when_the_migration_history_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=True)
    code, out, err = run(monkeypatch, conn, capsys, apply=True)
    assert code == 3
    assert err == REFUSAL
    assert out == ""
    applied = [t for t in conn.executed if any(t == m.sql for m in MIGRATIONS)]
    assert applied == []  # nothing was applied
    assert conn.rollbacks >= 1


def test_apply_on_an_empty_database_applies_every_migration_then_rolls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=False)
    code, out, err = run(monkeypatch, conn, capsys, apply=True)
    assert code == 0
    assert err == ""
    applied = [t for t in conn.executed if any(t == m.sql for m in MIGRATIONS)]
    assert applied == [m.sql for m in MIGRATIONS]
    assert conn.rollbacks >= 1  # the scratch changes are rolled back, never committed
    assert out.splitlines()[0] == "postgresql_major 16"


def test_the_apply_check_runs_before_the_first_migration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=False)
    run(monkeypatch, conn, capsys, apply=True)
    check_at = next(i for i, t in enumerate(conn.executed) if "schema_migrations" in t)
    first_at = next(i for i, t in enumerate(conn.executed) if t == MIGRATIONS[0].sql)
    assert check_at < first_at


def test_without_apply_the_database_is_read_only_and_nothing_is_applied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=True)
    code, out, _ = run(monkeypatch, conn, capsys, apply=False)
    assert code == 0
    assert conn.read_only is True
    assert [t for t in conn.executed if any(t == m.sql for m in MIGRATIONS)] == []
    assert not any("schema_migrations" in t for t in conn.executed)
    assert out.startswith("postgresql_major 16\n")


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2"])
def test_only_the_exact_value_1_turns_apply_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    conn = PinConnection(has_history=True)
    install(monkeypatch, kb_pins, conn)
    monkeypatch.setenv("DIVESAFE_PIN_DSN", SECRET_DSN)
    monkeypatch.setenv("DIVESAFE_PIN_APPLY", value)
    code = kb_pins.main()
    capsys.readouterr()
    assert code == 0  # read-only mode: the history is not even looked at
    assert conn.read_only is True


def test_a_missing_dsn_exits_2_without_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never(*a: Any, **k: Any) -> None:
        raise AssertionError("connected without a DSN")

    monkeypatch.setattr(kb_pins.psycopg, "connect", never)
    monkeypatch.delenv("DIVESAFE_PIN_DSN", raising=False)
    code = kb_pins.main()
    err = capsys.readouterr().err
    assert code == 2
    assert err == "set DIVESAFE_PIN_DSN\n"


# --- timeouts and the DSN ---------------------------------------------------------------------


def test_the_connection_carries_lock_and_statement_timeouts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=False)
    run(monkeypatch, conn, capsys, apply=True)
    args, kwargs = conn.connect_args
    assert args == (SECRET_DSN,)
    options = kwargs["options"]
    assert "-c lock_timeout=5000" in options
    assert "-c statement_timeout=30000" in options
    assert kwargs["connect_timeout"] == 5


def test_the_timeouts_are_also_set_for_a_read_only_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=True)
    run(monkeypatch, conn, capsys, apply=False)
    assert "lock_timeout=5000" in conn.connect_args[1]["options"]
    assert "statement_timeout=30000" in conn.connect_args[1]["options"]


@pytest.mark.parametrize(("has_history", "apply"), [(True, True), (False, True), (True, False)])
def test_the_dsn_is_never_printed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    has_history: bool,
    apply: bool,
) -> None:
    conn = PinConnection(has_history=has_history)
    _, out, err = run(monkeypatch, conn, capsys, apply=apply)
    for text in (out, err):
        assert "never-printed-secret" not in text
        assert "scratch_user" not in text
        assert "db.invalid" not in text


def test_a_database_error_gives_the_fixed_text_and_no_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=True, deny=True)
    code, out, err = run(monkeypatch, conn, capsys, apply=False)
    assert code == 4
    assert err == "could not read the database\n"
    assert out == ""
    assert "never-printed-secret" not in err


# --- the output -------------------------------------------------------------------------------


def test_the_output_lists_the_major_the_five_hashes_and_nine_tgtype_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = PinConnection(has_history=False)
    _, out, _ = run(monkeypatch, conn, capsys, apply=True)
    lines = out.splitlines()
    assert lines[0] == "postgresql_major 16"
    hashes = dict(line.split(" ") for line in lines[1:6])
    assert hashes == dict(KNOWLEDGE_FUNCTION_SHA256[16])
    type_lines = [line for line in lines if line.startswith("tgtype ")]
    assert len(type_lines) == 9
    parsed = {line.split(" ")[1]: int(line.split(" ")[2]) for line in type_lines}
    assert set(parsed) == set(KNOWLEDGE_TRIGGERS)
    assert parsed == {n: expected_tgtype(n) for n in KNOWLEDGE_TRIGGERS}
    assert lines[6:] == type_lines  # the tgtype lines come after the function lines


# --- mutations (copies) -----------------------------------------------------------------------

GUARD_CHECK = "                if row.fetchone() != (None,):"
EXIT_CODE = "                    return 3"
REFUSAL_TEXT = '"refused: APPLY needs an empty scratch database\\n"'
OPTIONS = '_OPTIONS = "-c lock_timeout=5000 -c statement_timeout=30000"'
APPLY_FLAG = 'apply = os.environ.get("DIVESAFE_PIN_APPLY") == "1"'
READ_ONLY = "                conn.read_only = True"
MIGRATION_LOOP = "                for m in MIGRATIONS:\n                    conn.execute(m.sql)"
ROLLBACK_IN_REFUSAL = (
    '                    conn.rollback()\n                    sys.stderr.write("refused'
)
TGTYPE_OUT = '        sys.stdout.write(f"tgtype {name} {value}\\n")'

PIN_MUTATIONS = [
    pytest.param(GUARD_CHECK, "                if False:", id="p-apply-guard-removed"),
    pytest.param(
        GUARD_CHECK, "                if row.fetchone() == (None,):", id="p-apply-guard-inverted"
    ),
    pytest.param(
        GUARD_CHECK,
        "                if row.fetchone() is None:",
        id="p-apply-guard-compares-the-wrong-shape",
    ),
    pytest.param(EXIT_CODE, "                    return 4", id="p-refusal-exit-code-changed"),
    pytest.param(EXIT_CODE, "                    return 0", id="p-refusal-exits-zero"),
    pytest.param(REFUSAL_TEXT, '"refused\\n"', id="p-refusal-text-changed"),
    pytest.param(OPTIONS, '_OPTIONS = "-c statement_timeout=30000"', id="p-lock-timeout-removed"),
    pytest.param(OPTIONS, '_OPTIONS = "-c lock_timeout=5000"', id="p-statement-timeout-removed"),
    pytest.param(OPTIONS, '_OPTIONS = ""', id="p-both-timeouts-removed"),
    pytest.param(
        APPLY_FLAG, 'apply = os.environ.get("DIVESAFE_PIN_APPLY") != "0"', id="p-apply-by-default"
    ),
    pytest.param(
        APPLY_FLAG, 'apply = bool(os.environ.get("DIVESAFE_PIN_APPLY"))', id="p-apply-for-any-value"
    ),
    pytest.param(READ_ONLY, "                pass", id="p-read-only-removed"),
    pytest.param(
        TGTYPE_OUT, '        sys.stdout.write(f"{name} {value}\\n")', id="p-tgtype-label-dropped"
    ),
    pytest.param(
        "    for name, value in types.items():\n" + TGTYPE_OUT,
        "    for name, value in list(types.items())[:8]:\n" + TGTYPE_OUT,
        id="p-one-tgtype-line-dropped",
    ),
    pytest.param(
        '    sys.stderr.write("could not read the database\\n")',
        '    sys.stderr.write(dsn + "\\n")',
        id="p-dsn-printed-on-error",
    ),
    pytest.param(
        '                    sys.stderr.write("refused: APPLY needs an empty scratch database\\n")',
        '                    sys.stderr.write(f"refused: {dsn}\\n")',
        id="p-dsn-printed-on-refusal",
    ),
]


def pin_misses(
    module: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> list[str]:
    bad = []

    def attempt(label: str, history: bool, apply: bool) -> tuple[Any, ...] | None:
        conn = PinConnection(has_history=history)
        try:
            code, out, err = run(monkeypatch, conn, capsys, apply=apply, module=module)
        except Exception:  # noqa: BLE001
            bad.append(f"{label}: crash")
            return None
        return code, out, err, conn

    refused = attempt("refusal", True, True)
    if refused is not None:
        code, out, err, conn = refused
        applied = [t for t in conn.executed if any(t == m.sql for m in MIGRATIONS)]
        if code != 3 or err != REFUSAL or out != "" or applied:
            bad.append("refusal")
        if any(secret in err + out for secret in ("never-printed-secret", "db.invalid")):
            bad.append("dsn-printed")
    empty = attempt("apply", False, True)
    if empty is not None:
        code, out, err, conn = empty
        options = conn.connect_args[1].get("options", "")
        if code != 0 or "-c lock_timeout=5000" not in options:
            bad.append("apply-or-lock-timeout")
        if "-c statement_timeout=30000" not in options:
            bad.append("statement-timeout")
        if len([line for line in out.splitlines() if line.startswith("tgtype ")]) != 9:
            bad.append("nine-tgtype-lines")
    inspect_only = attempt("inspect", True, False)
    if inspect_only is not None:
        code, out, err, conn = inspect_only
        if code != 0 or conn.read_only is not True:
            bad.append("read-only-inspection")
        if [t for t in conn.executed if any(t == m.sql for m in MIGRATIONS)]:
            bad.append("inspection-applied-a-migration")
    zero = PinConnection(has_history=True)
    install(monkeypatch, module, zero)
    monkeypatch.setenv("DIVESAFE_PIN_DSN", SECRET_DSN)
    monkeypatch.setenv("DIVESAFE_PIN_APPLY", "0")
    try:
        zero_code = module.main()
    except Exception:  # noqa: BLE001
        bad.append("apply-0: crash")
    else:
        if zero_code != 0 or zero.read_only is not True:
            bad.append("apply-0-is-not-apply")
    capsys.readouterr()
    errored = PinConnection(has_history=True, deny=True)
    try:
        code, out, err = run(monkeypatch, errored, capsys, apply=False, module=module)
    except Exception:  # noqa: BLE001
        bad.append("error-path: crash")
    else:
        if code != 4 or "never-printed-secret" in err:
            bad.append("error-path")
    return bad


def test_the_pin_checker_passes_on_the_real_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    misses = pin_misses(kb_pins, monkeypatch, capsys)
    assert misses == []


@pytest.mark.parametrize(("old", "new"), PIN_MUTATIONS)
def test_mutation_of_kb_pins_is_caught(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    old: str,
    new: str,
) -> None:
    mutant = mutated_module(kb_pins, (old, new))
    misses = pin_misses(mutant, monkeypatch, capsys)
    assert misses != []
    assert not all(m.endswith("crash") for m in misses), "caught only by a crash"


def test_the_pins_tool_reads_through_the_same_functions_the_check_uses() -> None:
    assert kb_pins.knowledge_function_hashes is migrations.knowledge_function_hashes
    assert kb_pins.knowledge_trigger_types is migrations.knowledge_trigger_types
