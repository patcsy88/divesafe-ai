"""Offline logger, handler and warnings tests of ADR 0027 §2's dataset sender (ADR 0030 decision 3
and Testing, "Partial run", the seventeen logger tests), with the stub SDK of `langfuse_rig.py`.
The stub is loaded by a `sys.meta_path` finder with a real spec (ADR 0030 Known gaps), never placed
in `sys.modules` by a test, and records the listed loggers' state when the sender imports it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.evaluation import harness, langfuse_checks
from tests.evaluation.langfuse_checks import (
    EVAL_SILENCED_LOGGERS,
    LANGFUSE_VERIFIED_VERSION,
    SenderRefused,
)
from tests.evaluation.langfuse_rig import MARKER, Rig, make_rig
from tests.evaluation.langfuse_sender import COMPLETION_SCORE_NAME

from divesafe.api import trace_export

pytestmark = pytest.mark.safety

# The test's own literal list (never the constant itself).
LISTED = (
    "langfuse",
    "httpx",
    "httpcore",
    "opentelemetry",
    "urllib3",
    "requests",
    "backoff",
    "charset_normalizer",
    "asyncio",
    "concurrent",
    "py.warnings",
)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    yield from make_rig(tmp_path, monkeypatch)


def _assert_refused_before_import(rig: Rig, refused: SenderRefused, label: str) -> None:
    assert refused.label == label
    assert rig.finder.executed == []
    assert rig.nothing_happened()


def _assert_refused_before_build(rig: Rig, refused: SenderRefused) -> None:
    assert refused.label == "logger_handler"
    assert rig.finder.executed  # the import happened; nothing after it
    assert not rig.control.built
    assert not rig.judge_calls
    assert rig.server.requests_seen == [] and rig.adapter.urls == []


def _assert_refused_after_build(rig: Rig, refused: SenderRefused, label: str) -> None:
    assert refused.label == label
    assert rig.finder.executed and rig.control.built
    assert not rig.judge_calls
    assert rig.server.requests_seen == []
    assert rig.adapter.urls == []


# --- the constant and the state at import (test 1) --------------------------------------------


def test_the_eval_logger_constant_is_pinned_to_its_literal() -> None:
    assert EVAL_SILENCED_LOGGERS == LISTED


def test_the_constant_holds_every_production_silenced_logger() -> None:
    assert set(trace_export.SILENCED_LOGGERS) <= set(EVAL_SILENCED_LOGGERS)


def test_every_listed_logger_is_silenced_before_the_sdk_import(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        langfuse_checks, "read_installed_version", lambda: LANGFUSE_VERIFIED_VERSION
    )
    for name in LISTED:  # start from a state the silencing must change
        logging.getLogger(name).propagate = True
        logging.getLogger(name).handlers[:] = []
    rig.control.watch = list(LISTED)

    rig.send(judge=None, case_limit=1)

    assert rig.finder.executed[0] == "langfuse"
    assert rig.control.at_import is not None
    assert set(rig.control.at_import) == set(LISTED)
    for name, (propagate, handlers) in rig.control.at_import.items():
        assert propagate is False, name
        assert handlers and all(h is logging.NullHandler for h in handlers), name


# --- handlers before the set-up (tests 2 and 3) -------------------------------------------------


@pytest.mark.parametrize("name", ["httpx", "py.warnings", "urllib3.connectionpool"])
def test_a_stream_handler_on_a_listed_logger_or_child_refuses_before_the_import(
    rig: Rig, name: str
) -> None:
    logging.getLogger(name).addHandler(logging.StreamHandler())

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_before_import(rig, refused.value, "logger_handler")


# --- handlers the SDK adds (tests 4 and 5) ------------------------------------------------------


def test_a_handler_the_client_constructor_adds_to_a_child_refuses_after_the_build(
    rig: Rig,
) -> None:
    rig.control.on_build.append(
        lambda: logging.getLogger("httpx.stub_child").addHandler(logging.StreamHandler())
    )

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "logger_handler")


def test_a_handler_a_flush_adds_to_a_new_child_fails_the_run_partway(rig: Rig) -> None:
    rig.control.on_flush.append(
        lambda: logging.getLogger("opentelemetry.stub_flush_child").addHandler(
            logging.StreamHandler()
        )
    )

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "logger_handler"
    assert rig.judge_calls == [harness.SCENARIOS[0].name]
    names = {s["name"] for batch in rig.server.score_batches for s in batch}
    assert COMPLETION_SCORE_NAME not in names
    assert not rig.output_dir.exists() or not any(rig.output_dir.iterdir())


# --- the SDK's own `console_handler` (tests 6 to 11) --------------------------------------------


def test_the_sdk_console_handler_added_once_is_removed_and_the_run_starts(rig: Rig) -> None:
    result = rig.send()

    assert result.complete
    handler = sys.modules["langfuse.logger"].console_handler  # type: ignore[attr-defined]
    assert handler not in logging.getLogger("httpx").handlers
    assert all(type(h) is logging.NullHandler for h in logging.getLogger("httpx").handlers)


def test_no_console_handler_refuses(rig: Rig) -> None:
    rig.control.logger_variant = "no_console_handler"

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_before_build(rig, refused.value)


def test_a_console_handler_attached_twice_refuses_before_any_removal(rig: Rig) -> None:
    rig.control.logger_variant = "twice"

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_before_build(rig, refused.value)
    handler = sys.modules["langfuse.logger"].console_handler  # type: ignore[attr-defined]
    assert sum(h is handler for h in logging.getLogger("httpx").handlers) == 2


def test_a_console_handler_also_on_langfuse_refuses_before_any_removal(rig: Rig) -> None:
    rig.control.logger_variant = "also_langfuse"

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_before_build(rig, refused.value)
    handler = sys.modules["langfuse.logger"].console_handler  # type: ignore[attr-defined]
    assert handler in logging.getLogger("httpx").handlers
    assert handler in logging.getLogger("langfuse").handlers


def test_a_second_stream_handler_on_httpx_refuses(rig: Rig) -> None:
    rig.control.logger_variant = "second_stream_handler"

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_before_build(rig, refused.value)


def test_httpx_set_to_propagate_refuses(rig: Rig) -> None:
    rig.control.logger_variant = "propagate"

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_before_build(rig, refused.value)


def test_a_root_handler_added_by_the_client_constructor_refuses(rig: Rig) -> None:
    rig.control.on_build.append(lambda: logging.getLogger().addHandler(logging.StreamHandler()))

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "logger_handler")


# --- unlisted loggers (tests 12 and 13) ---------------------------------------------------------


@pytest.mark.parametrize("name", ["stubnewtop", "stubnewname.child"])
def test_a_new_unlisted_logger_from_the_client_constructor_refuses(rig: Rig, name: str) -> None:
    rig.control.on_build.append(lambda: logging.getLogger(name))

    try:
        with pytest.raises(SenderRefused) as refused:
            rig.send()
    finally:
        manager = logging.Logger.manager
        for key in [k for k in manager.loggerDict if k.split(".")[0] == name.split(".")[0]]:
            del manager.loggerDict[key]

    _assert_refused_after_build(rig, refused.value, "logger_unlisted")


def test_a_new_unlisted_logger_from_a_flush_fails_the_run_partway(rig: Rig) -> None:
    rig.control.on_flush.append(lambda: logging.getLogger("stubflushtop"))

    try:
        with pytest.raises(SenderRefused) as refused:
            rig.send()
    finally:
        logging.Logger.manager.loggerDict.pop("stubflushtop", None)

    assert refused.value.label == "logger_unlisted"
    assert rig.judge_calls == [harness.SCENARIOS[0].name]
    names = {s["name"] for batch in rig.server.score_batches for s in batch}
    assert COMPLETION_SCORE_NAME not in names
    assert not rig.output_dir.exists() or not any(rig.output_dir.iterdir())


# --- warnings (tests 14 to 17) ------------------------------------------------------------------


def _warn_on_build(rig: Rig) -> list[bool]:
    """The stub client's constructor raises a warning holding the marker; records whether
    `warnings.showwarning` was logging's function at that point."""
    seen: list[bool] = []

    def warn() -> None:
        seen.append(warnings.showwarning is getattr(logging, "_showwarning", None))
        warnings.warn(MARKER, UserWarning, stacklevel=1)

    rig.control.on_build.append(warn)
    return seen


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, logging.Handler]]:
    """Each (message, handler) pair a log record reached, `NullHandler`s included."""
    hits: list[tuple[str, logging.Handler]] = []
    original = logging.Handler.handle
    null_original = logging.NullHandler.handle

    def handle(self: logging.Handler, record: logging.LogRecord) -> Any:
        hits.append((record.getMessage(), self))
        return original(self, record)

    def null_handle(self: logging.NullHandler, record: logging.LogRecord) -> Any:
        hits.append((record.getMessage(), self))
        return null_original(self, record)

    monkeypatch.setattr(logging.Handler, "handle", handle)
    monkeypatch.setattr(logging.NullHandler, "handle", null_handle)
    return hits


def _assert_warning_captured(
    recwarn: pytest.WarningsRecorder,
    reached: list[tuple[str, logging.Handler]],
    seen: list[bool],
) -> None:
    assert seen == [True]
    assert not any(MARKER in str(w.message) for w in recwarn)
    hits = [handler for message, handler in reached if MARKER in message]
    assert hits and all(type(h) is logging.NullHandler for h in hits)


def test_warnings_from_the_sdk_go_to_the_silenced_logger(
    rig: Rig, recwarn: pytest.WarningsRecorder, reached: list[tuple[str, logging.Handler]]
) -> None:
    seen = _warn_on_build(rig)

    assert rig.send(judge=None, case_limit=1).confirmed_cases == 1

    _assert_warning_captured(recwarn, reached, seen)


def test_warnings_are_captured_again_after_pytest_lost_the_hook(
    rig: Rig, recwarn: pytest.WarningsRecorder, reached: list[tuple[str, logging.Handler]]
) -> None:
    # As pytest does at the end of a test: the capture is turned on inside a block that restores
    # `showwarning` when it is left, so logging believes it is still capturing.
    with warnings.catch_warnings():
        logging.captureWarnings(True)
    assert warnings.showwarning is not getattr(logging, "_showwarning", None)
    seen = _warn_on_build(rig)

    assert rig.send(judge=None, case_limit=1).confirmed_cases == 1

    _assert_warning_captured(recwarn, reached, seen)


_SUBPROCESS = """
import sys
import urllib.request
import warnings
from pathlib import Path

sys.path[:0] = [{root!r}, {src!r}]
import httpx
from tests.agent.scripted_llm import fake
from tests.evaluation import harness, langfuse_checks, langfuse_sender
from tests.evaluation.langfuse_rig import (
    COMMIT, LangfuseServer, OtlpAdapter, StubControl, StubFinder, _git_stub, _write_stub,
)

tmp = Path({tmp!r})
init_file = _write_stub(tmp / "stub_sdk")
control = StubControl()
sys.meta_path.insert(0, StubFinder(tmp / "stub_sdk", control))
langfuse_checks.read_installed_version = lambda: "5.2.0"
langfuse_checks.distribution_init_file = lambda: init_file
langfuse_checks.run_git = _git_stub(COMMIT, "")
urllib.request.getproxies = dict


def built():
    warnings.warn({marker!r}, UserWarning, stacklevel=1)
    print("BUILD-REACHED-TOKEN", flush=True)


control.on_build.append(built)
{prepare}
server = LangfuseServer()
langfuse_sender.run_send(
    harness.SCENARIOS, fake, None, egress_basis="declared_local", model_host="localhost",
    output_dir=tmp / "out", case_limit=1,
    httpx_transport=httpx.MockTransport(server.handle), otlp_adapter=OtlpAdapter(),
)
"""


def _run_outside_pytest(tmp_path: Path, prepare: str) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, text in (
        (langfuse_checks.PUBLIC_KEY_FILE, "pk-lf-test\n"),
        (langfuse_checks.SECRET_KEY_FILE, "sk-lf-test\n"),
    ):
        (secrets / name).write_text(text)
        (secrets / name).chmod(0o600)  # the key-file read refuses group or other permission bits
    script = tmp_path / "run.py"
    script.write_text(
        textwrap.dedent(_SUBPROCESS).format(
            root=str(root),
            src=str(root / "src"),
            tmp=str(tmp_path),
            marker=MARKER,
            prepare=prepare,
        )
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("LANGFUSE_", "OTEL_", "DIVESAFE_", "PYTEST", "PYTHONWARN"))
        and k.lower() not in langfuse_checks.PROXY_VARIABLES
    }
    env[langfuse_checks.SECRETS_DIR_VARIABLE] = str(secrets)

    return subprocess.run(  # noqa: S603
        [sys.executable, "-W", "default", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
        check=False,
    )


# Pytest has created `py.warnings` before any test runs; a plain process has not. The snapshot
# `logger_unlisted` compares against holds every full name in the logger table and is taken
# BEFORE the sender's own silencing, so in a plain process the silencing creates `py.warnings`
# (a listed name, allowed) and its placeholder ancestor `py` after the snapshot. `py` passes only
# under the placeholder-ancestor allowance (an ancestor of a listed name that is still a
# `logging.PlaceHolder`, never a real `Logger`). The fifteenth test therefore runs in both forms:
# as pytest leaves the logger table, and from scratch.
_AS_PYTEST_LEAVES_IT = "import logging; logging.getLogger('py.warnings')"


def test_a_run_outside_pytest_reaches_the_build_with_the_warning_captured(tmp_path: Path) -> None:
    done = _run_outside_pytest(tmp_path, _AS_PYTEST_LEAVES_IT)

    assert done.returncode == 0, done.stderr[-2000:]
    assert "BUILD-REACHED-TOKEN" in done.stdout
    assert MARKER not in done.stderr


def test_a_run_in_a_fresh_process_is_not_refused_for_its_own_py_warnings_logger(
    tmp_path: Path,
) -> None:
    done = _run_outside_pytest(tmp_path, "")

    assert "logger_unlisted" not in done.stderr
    assert done.returncode == 0, done.stderr[-2000:]
    assert "BUILD-REACHED-TOKEN" in done.stdout
    assert MARKER not in done.stderr


def test_a_capture_that_does_not_take_effect_refuses_before_the_import(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(warnings, "showwarning", warnings._showwarning_orig)  # type: ignore[attr-defined]
    monkeypatch.setattr(logging, "captureWarnings", lambda capture: None)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_before_import(rig, refused.value, "logger_handler")


def test_a_client_replacing_showwarning_refuses_after_the_build(rig: Rig) -> None:
    def replace() -> None:
        warnings.showwarning = lambda *args, **kwargs: None

    rig.control.on_build.append(replace)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "logger_handler")


# --- loggers the SDK creates at import, by full name (round 2026-11-20) -----------------------


def _assert_unlisted_before_build(rig: Rig, refused: SenderRefused) -> None:
    assert refused.label == "logger_unlisted"
    assert refused.__context__ is None and refused.__cause__ is None
    assert rig.finder.executed  # the import happened; nothing after it
    assert not rig.control.built
    assert not rig.judge_calls
    assert "span_started" not in rig.control.events
    assert rig.server.requests_seen == [] and rig.adapter.urls == []


def _send_with_import_loggers(rig: Rig, names: list[str], *, fresh: bool = True) -> SenderRefused:
    """The stub SDK creates `names` when imported. With `fresh`, every entry under the names' top
    levels is removed from the logger table first (so the snapshot does not hold them, as in a
    fresh process); without it the table is left as pytest leaves it (`py.warnings` exists).
    Either way those entries are put back as they were afterwards."""
    manager = logging.Logger.manager
    tops = {n.split(".")[0] for n in names}
    saved = {k: v for k, v in manager.loggerDict.items() if k.split(".")[0] in tops}
    if fresh:
        for key in saved:
            del manager.loggerDict[key]
    rig.control.import_loggers = names
    try:
        with pytest.raises(SenderRefused) as refused:
            rig.send()
    finally:
        for key in [k for k in manager.loggerDict if k.split(".")[0] in tops]:
            del manager.loggerDict[key]
        manager.loggerDict.update(saved)
        _reparent(list(manager.loggerDict.items()))
    return refused.value


def _reparent(entries: list[tuple[str, Any]]) -> None:
    """Each logger's parent back to its nearest real ancestor in the table (a removed real
    logger may still be some logger's parent), and the level caches cleared."""
    manager = logging.Logger.manager
    for name, item in entries:
        if not isinstance(item, logging.Logger):
            continue
        parts = name.split(".")
        parent: logging.Logger = logging.getLogger()
        for end in range(len(parts) - 1, 0, -1):
            found = manager.loggerDict.get(".".join(parts[:end]))
            if isinstance(found, logging.Logger):
                parent = found
                break
        item.parent = parent
    manager._clear_cache()  # type: ignore[attr-defined]


def test_a_new_top_level_logger_at_import_refuses_before_the_build(rig: Rig) -> None:
    """Risk S2: a logger the SDK creates at import is in no snapshot taken after the import."""
    refused = _send_with_import_loggers(rig, ["stubimporttop"])

    _assert_unlisted_before_build(rig, refused)


def test_an_unlisted_child_of_a_listed_ancestor_refuses(rig: Rig) -> None:
    """`py.other` shares only its top level with `py.warnings` (in the table, as pytest leaves
    it): full names are compared."""
    assert "py.warnings" in logging.Logger.manager.loggerDict
    refused = _send_with_import_loggers(rig, ["py.other"], fresh=False)

    _assert_unlisted_before_build(rig, refused)


def test_the_ancestor_of_a_listed_name_made_a_real_logger_refuses(rig: Rig) -> None:
    """`py` is allowed only as the placeholder `py.warnings` leaves; a real `py` logger (its
    handlers and propagation would apply to `py.warnings`'s records) refuses."""
    refused = _send_with_import_loggers(rig, ["py"])

    _assert_unlisted_before_build(rig, refused)


# --- `logger_last_resort` (round 2026-11-20, C) -----------------------------------------------

_CHILD = "httpx.stubchild"


def _child_after(rig: Rig, when: str, *, propagate: bool, handler: logging.Handler | None) -> None:
    def make() -> None:
        child = logging.getLogger(_CHILD)
        child.propagate = propagate
        if handler is not None:
            child.addHandler(handler)

    (rig.control.on_build if when == "build" else rig.control.on_flush).append(make)


@pytest.fixture
def child_cleanup() -> Iterator[None]:
    yield
    manager = logging.Logger.manager
    if _CHILD in manager.loggerDict:
        del manager.loggerDict[_CHILD]
    manager._clear_cache()  # type: ignore[attr-defined]


def test_a_silent_child_with_no_handler_refuses_last_resort_after_the_build(
    rig: Rig, child_cleanup: None
) -> None:
    """`propagate=False` and no handler: `logging.lastResort` would print its records."""
    _child_after(rig, "build", propagate=False, handler=None)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "logger_last_resort")
    assert refused.value.__context__ is None


def test_a_silent_child_with_no_handler_from_a_flush_fails_the_run_partway(
    rig: Rig, child_cleanup: None
) -> None:
    _child_after(rig, "flush", propagate=False, handler=None)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "logger_last_resort"
    assert rig.judge_calls == [harness.SCENARIOS[0].name]
    assert len(rig.server.items_seen) == 1
    names = {s["name"] for batch in rig.server.score_batches for s in batch}
    assert COMPLETION_SCORE_NAME not in names
    assert not rig.output_dir.exists() or not any(rig.output_dir.iterdir())


@pytest.mark.parametrize("form", ["null_handler", "propagating"])
def test_a_child_with_a_null_handler_or_propagating_does_not_refuse(
    rig: Rig, child_cleanup: None, form: str
) -> None:
    if form == "null_handler":
        _child_after(rig, "build", propagate=False, handler=logging.NullHandler())
    else:
        _child_after(rig, "build", propagate=True, handler=None)

    result = rig.send()

    assert result.complete
    assert logging.getLogger(_CHILD).propagate is (form == "propagating")


def test_a_silent_child_with_a_stream_handler_still_refuses_logger_handler(
    rig: Rig, child_cleanup: None
) -> None:
    _child_after(rig, "build", propagate=False, handler=logging.StreamHandler())

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "logger_handler")


# --- `logger_last_resort` on a listed logger (round 2026-11-20 b, D) --------------------------


def _strip_listed(name: str) -> None:
    library = logging.getLogger(name)
    library.handlers[:] = []  # the sender's `NullHandler` removed
    library.propagate = False  # as the silencing left it


@pytest.mark.parametrize("name", ["requests", "httpx"])
def test_a_listed_logger_whose_null_handler_is_removed_refuses_last_resort_after_the_build(
    rig: Rig, name: str
) -> None:
    rig.control.on_build.append(lambda: _strip_listed(name))

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "logger_last_resort")
    assert refused.value.__context__ is None


def test_a_listed_logger_stripped_during_a_flush_fails_the_run_partway(rig: Rig) -> None:
    rig.control.on_flush.append(lambda: _strip_listed("requests"))

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "logger_last_resort"
    assert rig.judge_calls == [harness.SCENARIOS[0].name]
    names = {s["name"] for batch in rig.server.score_batches for s in batch}
    assert COMPLETION_SCORE_NAME not in names
    assert not rig.output_dir.exists() or not any(rig.output_dir.iterdir())
