"""Start-up refusals and constants of the evaluation dataset sender (ADR 0027 §2, ADR 0030
decision 3). Development only: never imported by `src`.

This module never imports `langfuse` in any form, so its refusals run (and are tested offline)
without the SDK. Every refusal raises `SenderRefused` with a fixed label only, never text from
another exception, and is raised outside any `except` block so it carries no `__context__`.

Every read of the process environment is in one of these functions (an AST test pins the list):
`refuse_sdk_environment`, `refuse_divesafe_variables`, `eval_secrets_dir`,
`set_media_variable`, `remove_media_variable`, `check_post_build_environment`, `git_environment`.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import stat
import subprocess
import sys
import urllib.request
import uuid
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypeGuard

from pydantic import SecretStr
from tests.agent import scripted_llm
from tests.evaluation import adversarial, harness
from tests.evaluation.output_dir import CaptureRefused, check_output_dir

from divesafe.data.removed_sources import PHN_ETIDE_SOURCE as PHN_SOURCE
from divesafe.data.tides_today import SOURCE as TIDES_TODAY_SOURCE
from divesafe.domain import AssessmentRecord, DataKind

# ADR 0027 §2: the version recorded at build step 1; changed only in the reviewed change that
# repeats those checks. Must equal the `[eval]` `==` pin (an offline test reads pyproject.toml).
LANGFUSE_VERIFIED_VERSION: Final = "5.2.0"
# The fixed port Langfuse's web service publishes on this machine; a constant, never read from the
# environment. Plain http on loopback is ADR 0027 §2's development-only exception.
LANGFUSE_BASE_URL: Final = "http://127.0.0.1:3000"
LANGFUSE_PROJECT: Final = "divesafe-eval"
OTLP_TRACES_PATH: Final = "/api/public/otel/v1/traces"
# The OTLP ingestion-version header, the same value as production's (`trace_export`), defined here
# so the sender's modules import nothing from `divesafe.api`.
INGESTION_HEADER: Final = ("x-langfuse-ingestion-version", "4")
# ADR 0030 decision 3: the SDK's own loggers and its HTTP and OTLP client loggers, fixed at build
# step 1 for the pinned SDK. Holds every name in `trace_export.SILENCED_LOGGERS`. Its own constant.
EVAL_SILENCED_LOGGERS: Final = (
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
MEDIA_VARIABLE: Final = "LANGFUSE_MEDIA_UPLOAD_ENABLED"
DEBUG_VARIABLE: Final = "LANGFUSE_DEBUG"
PROXY_VARIABLES: Final = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
# ADR 0027 §2: must be unset (present at all, even empty, refuses).
UNSET_DIVESAFE_VARIABLES: Final = (
    "DIVESAFE_TRACING_ENABLED",
    "DIVESAFE_DATABASE_URL",
    "DIVESAFE_ADMIN_DATABASE_URL",
    "DIVESAFE_EVAL_KNOWLEDGE_HOST",
)
SECRETS_DIR_VARIABLE: Final = "DIVESAFE_EVAL_SECRETS_DIR"
# Key file names in DIVESAFE_EVAL_SECRETS_DIR (builder choice, after `divesafe_trace_public_key`).
PUBLIC_KEY_FILE: Final = "divesafe_eval_public_key"
SECRET_KEY_FILE: Final = "divesafe_eval_secret_key"
# The production key pattern (settings._TRACE_KEY): keeps CR, LF and `:` out of the header.
_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,256}")
# The longest key file: 256 key characters and one trailing newline.
_KEY_FILE_MAX_BYTES: Final = 257
_COMMIT: Final = re.compile(r"[0-9a-f]{40}")
# The Langfuse server version the live check records: a version shape starting with a digit
# (an optional `v`), two to four numeric parts and an optional short `-`/`+` suffix, e.g. `3.112.0`
# or `3.112.0-rc.1`. A key (`pk-lf-...`, `sk-lf-...`) cannot match it.
_SERVER_VERSION: Final = re.compile(r"v?[0-9]{1,4}(\.[0-9]{1,4}){1,3}([-+][0-9A-Za-z.]{1,20})?")
# The repository root, from this module's own path (never the working directory or environment).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
# ADR 0027 §2: the recorded fixtures the allow-listed `SCENARIOS` use, pinned by SHA-256.
_FIXTURES: Final = REPO_ROOT / "tests" / "fixtures"
FIXTURE_SHA256: Final = {
    "open_meteo_marine_tioman_recorded_2026-10-08.json": (
        "405af6b665f67800053459634cb35cf72281f47acdacca6e8a25f9fe0eed496e"
    ),
    "data_gov_my_warning_recorded_2026-10-09.json": (
        "0d05571336e097c21d283cea19b5c4d8842ad8abebd3b7250bb25eb4d1b31c26"
    ),
}
# The tide items never sent, identified by source: PHN (ADR 0021) and tides.today (ADR 0029).
# This is the only use of the tides.today constant (owner, 2026-11-19; pinned by
# `test_no_evaluation_module_builds_or_reads_tides_today`).
REFUSED_SOURCES: Final = frozenset({PHN_SOURCE, TIDES_TODAY_SOURCE})


class SenderRefused(RuntimeError):
    """A refusal or a failed run with a fixed label only, never another exception's text.

    Before the SDK import (no network call yet):
    - `case_limit`: the limit is neither absent nor exactly the int `1`;
    - `cases_not_allow_listed`: the cases are not an allow-listed set, by identity;
    - `case_set_runner_not_built`: the adversarial set, whose live runner is not built;
    - `fixture_hash`: a pinned fixture file or its loaded payload does not match;
    - `judge_missing`: no judge for a full run;
    - `model_host_missing`: no model host;
    - `debug`: `LANGFUSE_DEBUG` is set;
    - `sdk_environment`: a `LANGFUSE_*`, `OTEL_*` or proxy variable, or a system proxy (also
      checked again after the build);
    - `divesafe_variable_set`: a `DIVESAFE_*` variable that must be unset is present;
    - `already_imported`: `langfuse` is already in `sys.modules`;
    - `langfuse_not_installed`, `langfuse_version`: no SDK installed, or not the verified version;
    - `import_origin`: the module to import is not the distribution's;
    - `logger_handler`: the warnings capture or a listed logger's handlers are wrong (also
      checked in the set-up and after each case);
    - `logger_last_resort`: a listed logger or a child of one neither propagates nor has a
      handler, so
      `logging.lastResort` would print its records (checked with `logger_handler`);
    - `secrets_dir`, `eval_keys`: the secrets directory, or a key file or its content;
    - `git_worktree`, `git_commit`: the worktree is not clean, or `HEAD` is not a full commit id;
    - `run_name`: the run name could not be built;
    - `output_dir`: the output directory is refused.

    Set-up (after the import, before any case):
    - `logger_unlisted`: a new logger name that is not allowed (also checked after each case);
    - `client_build`: the OTLP exporter or the client could not be built;
    - `global_tracer_provider`: building the client set a global tracer provider;
    - `media_upload`: media upload is not off;
    - `project`: the keys do not show `divesafe-eval` and only it;
    - `server_version`: on the live check, the health reply's server version is not a short
      version string, or contains a key value, `pk-lf` / `sk-lf` (ignoring case), or any run of 6
      or more characters that also appears in a key (case-insensitive).

    Per case:
    - `provider_missing`: the provider factory returned none;
    - `record_knowledge`, `record_tide_item`: the record holds knowledge, or a PHN or tides.today
      tide item.

    Send (the set-up's calls, each case's item, span and scores, and the completion marker):
    - `send_failed`: a send was not confirmed; it can fail the run partway.

    Shutdown:
    - `shutdown_failed`: the client shutdown failed after the completion marker was confirmed in
      Langfuse, before the local file is written.

    See ADR 0030 Known gaps (partial-run limits)."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


# --- arguments ---------------------------------------------------------------------------------


def check_case_limit(limit: object) -> int | None:
    """Absent (a full run) or exactly the int `1`. `True`, 0, 2, -1 and any other value refuse."""
    if limit is None:
        return None
    if type(limit) is int and limit == 1:
        return 1
    raise SenderRefused("case_limit")


def check_allowed_cases(cases: object) -> tuple[harness.Scenario, ...]:
    """The cases must be one of the allow-listed sets, by identity (never by a `synthetic` flag),
    and the fixture payloads they use must match the pinned hashes. The adversarial set is on the
    allow-list, but its live runner is ADR 0030 build step 4 and is not built, so it refuses."""
    if cases is not harness.SCENARIOS and cases is not adversarial.CASES:
        raise SenderRefused("cases_not_allow_listed")
    if cases is not harness.SCENARIOS:
        raise SenderRefused("case_set_runner_not_built")
    check_fixture_pins()
    return harness.SCENARIOS


def check_fixture_pins() -> None:
    """Each pinned fixture file matches its SHA-256, and the payloads loaded at import still equal
    the files (so an in-memory change is caught too)."""
    loaded = {
        "open_meteo_marine_tioman_recorded_2026-10-08.json": scripted_llm.MARINE,
        "data_gov_my_warning_recorded_2026-10-09.json": scripted_llm.WARNINGS,
    }
    for name, digest in FIXTURE_SHA256.items():
        data = (_FIXTURES / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest or json.loads(data) != loaded[name]:
            raise SenderRefused("fixture_hash")


# --- environment --------------------------------------------------------------------------------


def _sdk_variable(name: str) -> bool:
    return name.upper().startswith(("LANGFUSE_", "OTEL_")) or name.lower() in PROXY_VARIABLES


def refuse_sdk_environment() -> None:
    """ADR 0027 §2: no `LANGFUSE_*`, `OTEL_*` or proxy variable in any case, with no exception
    (so an operator's `LANGFUSE_MEDIA_UPLOAD_ENABLED`, even `false`, refuses), and
    `urllib.request.getproxies()` empty (which includes the macOS system proxy). `LANGFUSE_DEBUG`
    gets its own label (ADR 0030 decision 3). Runs before the sender sets anything."""
    names = list(os.environ)
    if any(name.upper() == DEBUG_VARIABLE for name in names):
        raise SenderRefused("debug")
    if any(_sdk_variable(name) for name in names) or urllib.request.getproxies():
        raise SenderRefused("sdk_environment")


def refuse_divesafe_variables() -> None:
    """ADR 0027 §2: tracing off and no knowledge connector or database (present at all, in any
    case, refuses)."""
    if any(name.upper() in UNSET_DIVESAFE_VARIABLES for name in os.environ):
        raise SenderRefused("divesafe_variable_set")


def set_media_variable() -> None:
    """ADR 0030 decision 3: the only `LANGFUSE_*` variable the sender sets, after the refusal
    above passed and before the import. Never read from the operator."""
    os.environ[MEDIA_VARIABLE] = "false"


def remove_media_variable() -> None:
    """Removes the variable; called only when the sender set it, however the run ends."""
    os.environ.pop(MEDIA_VARIABLE, None)


def check_post_build_environment() -> None:
    """After the build: the media variable still holds exactly `false`, and no other
    `LANGFUSE_*`, `OTEL_*` or proxy variable is set."""
    if os.environ.get(MEDIA_VARIABLE) != "false":
        raise SenderRefused("media_upload")
    others = [name for name in os.environ if name != MEDIA_VARIABLE]
    if any(_sdk_variable(name) for name in others) or urllib.request.getproxies():
        raise SenderRefused("sdk_environment")


def is_server_version_shape(version: object) -> TypeGuard[str]:
    """Whether the live check's Langfuse server version from the health reply is a `str` matching
    the version shape `_SERVER_VERSION` in full (at most 41 characters). It never raises: the
    caller deletes every copy of a refused value and then raises `server_version` itself, so no
    frame on the way to the raise holds it. The sender also refuses a version holding a key or
    part of one (`langfuse_sender._version_holds_key`)."""
    return type(version) is str and _SERVER_VERSION.fullmatch(version) is not None


def check_model_host(model_host: str | None) -> str:
    """ADR 0030 decision 3: with a provider, a `None` host fails the run."""
    if model_host is None:
        raise SenderRefused("model_host_missing")
    return model_host


# --- keys ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalKeys:
    public: SecretStr
    secret: SecretStr


def eval_secrets_dir() -> Path:
    """`DIVESAFE_EVAL_SECRETS_DIR`: set, an existing directory, and outside the repository (the
    output-directory check, comparing each ancestor with `samefile`)."""
    configured = os.environ.get(SECRETS_DIR_VARIABLE)
    if not configured:
        raise SenderRefused("secrets_dir")
    directory: Path | None = None
    try:
        directory = check_output_dir(Path(configured), REPO_ROOT)
    except CaptureRefused:
        directory = None
    if directory is None or not directory.is_dir():
        raise SenderRefused("secrets_dir")
    return directory


def _read_key_file(path: Path) -> str | None:
    """One key file, or None. Opened without following a symlink and without blocking (a FIFO);
    it must be a regular file owned by this user, with no group or other permission bits, of at
    most `_KEY_FILE_MAX_BYTES` bytes (checked on the open descriptor and again on what was read).
    Any OS error is dropped: no path or OS text is kept."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    data: bytes | None = None
    try:
        info = os.fstat(fd)
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_size <= _KEY_FILE_MAX_BYTES
            and info.st_mode & 0o077 == 0
            and info.st_uid == os.getuid()
        ):
            data = os.read(fd, _KEY_FILE_MAX_BYTES + 1)
    except OSError:
        data = None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    if data is None or len(data) > _KEY_FILE_MAX_BYTES:
        return None
    try:
        return data.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError:
        return None


def read_eval_keys() -> EvalKeys:
    """The `divesafe-eval` keys from their files (`_read_key_file`); each must match the
    production key pattern (one trailing newline allowed). Any failure refuses with `eval_keys`;
    the text is never logged or put in a message."""
    directory = eval_secrets_dir()
    values: list[str] = []
    for name in (PUBLIC_KEY_FILE, SECRET_KEY_FILE):
        text = _read_key_file(directory / name)
        if text is None or not _KEY_PATTERN.fullmatch(text):
            raise SenderRefused("eval_keys")
        values.append(text)
    return EvalKeys(SecretStr(values[0]), SecretStr(values[1]))


# --- the SDK, before it is imported -------------------------------------------------------------


def refuse_if_imported() -> None:
    """ADR 0030 Known gaps: refuse if `langfuse` is already in `sys.modules`."""
    if any(name == "langfuse" or name.startswith("langfuse.") for name in sys.modules):
        raise SenderRefused("already_imported")


def read_installed_version() -> str:
    """Seam: `importlib.metadata.version("langfuse")`, read before import."""
    return importlib.metadata.version("langfuse")


def distribution_init_file() -> Path:
    """Seam: the installed distribution's `langfuse/__init__.py`."""
    located = importlib.metadata.distribution("langfuse").locate_file("langfuse/__init__.py")
    return Path(str(located))


def check_installed_version() -> None:
    """The installed version equals `LANGFUSE_VERIFIED_VERSION`; none installed also refuses and
    is never a skip."""
    version: str | None = None
    try:
        version = read_installed_version()
    except importlib.metadata.PackageNotFoundError:
        version = None
    if version is None:
        raise SenderRefused("langfuse_not_installed")
    if version != LANGFUSE_VERIFIED_VERSION:
        raise SenderRefused("langfuse_version")


def check_import_origin() -> None:
    """`find_spec("langfuse").origin` resolves to the distribution's `langfuse/__init__.py`, so the
    module imported is the one whose version was read. `None`, or any error, refuses."""
    origin: Path | None = None
    expected: Path | None = None
    try:
        spec = importlib.util.find_spec("langfuse")
        if spec is not None and spec.origin is not None:
            origin = Path(spec.origin).resolve()
            expected = distribution_init_file().resolve()
    except Exception:  # any failure of the lookup refuses; the error is dropped
        origin = None
    if origin is None or expected is None or origin != expected:
        raise SenderRefused("import_origin")


# --- loggers ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoggingBaseline:
    """Taken within the test body at the start of the set-up, before `silence_eval_loggers()`:
    the root logger's handlers and every full name in `logging.Logger.manager.loggerDict` (loggers
    and placeholders)."""

    root_handlers: tuple[logging.Handler, ...]
    names: frozenset[str]


def logging_baseline() -> LoggingBaseline:
    """The snapshot at the start of the set-up."""
    return LoggingBaseline(
        tuple(logging.getLogger().handlers), frozenset(list(logging.Logger.manager.loggerDict))
    )


def silence_eval_loggers() -> None:
    """`propagate=False` and a `NullHandler` on every listed logger, before the SDK is imported."""
    for name in EVAL_SILENCED_LOGGERS:
        library = logging.getLogger(name)
        library.propagate = False
        if not any(type(h) is logging.NullHandler for h in library.handlers):
            library.addHandler(logging.NullHandler())


def capture_warnings() -> None:
    """Off then on, so the capture takes effect again in each test (pytest restores
    `warnings.showwarning` when a test ends)."""
    logging.captureWarnings(False)
    logging.captureWarnings(True)


def _listed_and_children() -> list[logging.Logger]:
    found = [logging.getLogger(name) for name in EVAL_SILENCED_LOGGERS]
    for name, item in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(item, logging.Logger) and any(
            name.startswith(f"{listed}.") for listed in EVAL_SILENCED_LOGGERS
        ):
            found.append(item)
    return found


def check_handlers(baseline: LoggingBaseline) -> None:
    """The handler check (ADR 0030 decision 3), label `logger_handler`: `warnings.showwarning` is
    logging's own function; no listed logger, or existing child of one, has a handler other than a
    `NullHandler`; every listed logger has `propagate=False`; the root logger's handlers are, by
    identity, those recorded at the start of the set-up.

    Then, label `logger_last_resort`: a listed logger, or an existing child of one, with
    `propagate=False` and no handler at all refuses, since `logging.lastResort` would print its
    WARNING and above records to stderr. Every one of them must propagate or have a handler (a
    `NullHandler` counts)."""
    if warnings.showwarning is not getattr(logging, "_showwarning", None):
        raise SenderRefused("logger_handler")
    for library in _listed_and_children():
        if any(type(h) is not logging.NullHandler for h in library.handlers):
            raise SenderRefused("logger_handler")
    if any(logging.getLogger(name).propagate for name in EVAL_SILENCED_LOGGERS):
        raise SenderRefused("logger_handler")
    root = tuple(logging.getLogger().handlers)
    if len(root) != len(baseline.root_handlers) or any(
        a is not b for a, b in zip(root, baseline.root_handlers, strict=True)
    ):
        raise SenderRefused("logger_handler")
    for library in _listed_and_children():
        if not library.propagate and not library.handlers:
            raise SenderRefused("logger_last_resort")


def _allowed_new_name(name: str, item: object) -> bool:
    """A name new since the snapshot is allowed only if it is listed, a child of a listed name, or
    a placeholder (never a real `Logger`) whose name is an ancestor of a listed name, such as `py`
    for `py.warnings`."""
    for listed in EVAL_SILENCED_LOGGERS:
        if name == listed or name.startswith(f"{listed}."):
            return True
        if listed.startswith(f"{name}.") and isinstance(item, logging.PlaceHolder):
            return True
    return False


def check_no_unlisted_loggers(baseline: LoggingBaseline) -> None:
    """A full name in `loggerDict` (a logger or a placeholder) that was not there at the start of
    the set-up and is not allowed by `_allowed_new_name` refuses with `logger_unlisted`."""
    new = [
        (name, item)
        for name, item in list(logging.Logger.manager.loggerDict.items())
        if name not in baseline.names
    ]
    if not all(_allowed_new_name(name, item) for name, item in new):
        raise SenderRefused("logger_unlisted")


def remove_sdk_console_handler() -> None:
    """Right after the import: `sys.modules["langfuse.logger"].console_handler` must be a
    `logging.Handler` attached to `httpx` exactly once and to no other logger (the root included);
    it is then removed by identity. Anything else refuses with `logger_handler`, before any
    removal."""
    module = sys.modules.get("langfuse.logger")
    handler = getattr(module, "console_handler", None) if module is not None else None
    if not isinstance(handler, logging.Handler):
        raise SenderRefused("logger_handler")
    httpx_logger = logging.getLogger("httpx")
    if sum(h is handler for h in httpx_logger.handlers) != 1:
        raise SenderRefused("logger_handler")
    others: list[logging.Logger] = [logging.getLogger()]
    others += [
        item
        for item in list(logging.Logger.manager.loggerDict.values())
        if isinstance(item, logging.Logger) and item is not httpx_logger
    ]
    if any(any(h is handler for h in other.handlers) for other in others):
        raise SenderRefused("logger_handler")
    httpx_logger.removeHandler(handler)


# --- provenance and run name --------------------------------------------------------------------


def git_environment() -> dict[str, str]:
    """The process environment with every `GIT_*` variable removed, for the `git` calls only."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def run_git(argv: Sequence[str], env: dict[str, str]) -> str | None:
    """Seam: one `git` command; its stdout, or None when it could not run or exited non-zero."""
    try:
        done = subprocess.run(  # noqa: S603
            list(argv), capture_output=True, text=True, timeout=10, check=False, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_commit() -> str:
    """`git -C <root> rev-parse HEAD`: 40 lowercase hex characters, or the set-up refuses."""
    out = run_git(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], git_environment())
    commit = None if out is None else out.strip()
    if commit is None or not _COMMIT.fullmatch(commit):
        raise SenderRefused("git_commit")
    return commit


# No fsmonitor hook and no untracked cache (either could hide a change or run a configured
# program), and no optional index lock.
GIT_STATUS_ARGS: Final = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "--no-optional-locks",
    "status",
    "--porcelain",
    "--untracked-files=all",
    "--ignore-submodules=none",
)


def check_clean_worktree() -> None:
    """`git -C <root> -c core.fsmonitor=false -c core.untrackedCache=false --no-optional-locks
    status --porcelain --untracked-files=all --ignore-submodules=none` must be empty; run on every
    run, the live check and a full run alike."""
    out = run_git(["git", "-C", str(REPO_ROOT), *GIT_STATUS_ARGS], git_environment())
    if out is None or out.strip():
        raise SenderRefused("git_worktree")


def utc_now() -> datetime:
    """Seam: the run's UTC start time."""
    return datetime.now(UTC)


def build_run_name(commit: str, started: datetime, run_uuid: uuid.UUID) -> str:
    """The dataset run name: the commit, the UTC start time and a fresh `uuid.uuid4()`. Never from
    an argument of the entry point, the environment or the command line."""
    if not _COMMIT.fullmatch(commit) or started.utcoffset() is None:
        raise SenderRefused("run_name")
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"eval-{commit}-{stamp}-{run_uuid}"


# --- per-case record content --------------------------------------------------------------------


def check_record_content(record: AssessmentRecord) -> None:
    """Nothing §2 never sends is in the record: no KNOWLEDGE item or `kb:` evidence (sent or
    withheld) and no PHN or tides.today tide item, identified by source (`REFUSED_SOURCES`)."""
    items = (*record.evidence, *record.withheld_evidence)
    if record.withheld_knowledge or any(
        e.data_kind is DataKind.KNOWLEDGE or e.id.startswith("kb:") for e in items
    ):
        raise SenderRefused("record_knowledge")
    if any(e.source in REFUSED_SOURCES for e in items):
        raise SenderRefused("record_tide_item")
