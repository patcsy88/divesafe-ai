"""The offline rig for ADR 0027 §2's dataset sender (ADR 0030 decision 3): a stub `langfuse`
package loaded by a `sys.meta_path` finder with a real spec (ADR 0030 Known gaps), never placed in
`sys.modules` by a test, an `httpx.MockTransport` Langfuse API behind the sender's real
`httpx_client`, and a `requests` adapter mounted on the sender's real OTLP session, so both
response hooks run. Shared by `test_langfuse_sender_offline.py` and
`test_langfuse_sender_logging_offline.py`; not a test module itself.

The finder records each module it actually executes, so "never imported" is observable. The
version read and the distribution lookup are stubbed through the sender's seams; neither check is
loosened.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import json
import logging
import sys
import types
import urllib.request
import uuid
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests
import requests.adapters
from opentelemetry import trace as trace_api
from opentelemetry.util._once import Once
from requests.structures import CaseInsensitiveDict
from tests.agent.scripted_llm import fake
from tests.evaluation import harness, langfuse_checks, langfuse_sender
from tests.evaluation.langfuse_checks import (
    EVAL_SILENCED_LOGGERS,
    MEDIA_VARIABLE,
    PUBLIC_KEY_FILE,
    SECRET_KEY_FILE,
)
from tests.evaluation.langfuse_sender import JudgeScore, run_send

from divesafe.domain import AssessmentRecord

COMMIT = "0123456789abcdef0123456789abcdef01234567"
STARTED = datetime(2026, 11, 19, 8, 30, 0, tzinfo=UTC)
RUN_UUID = uuid.UUID("11111111-2222-4333-8444-555555555555")
LOCAL = {"egress_basis": "declared_local", "model_host": "localhost"}
MARKER = "SEND-ERROR-MARKER-7f3a"
PROJECT = {"id": "project-1", "name": "divesafe-eval"}
# The stub server's Langfuse version, from its health reply (the live check only).
SERVER_VERSION = "3.112.0"

# --- the stub SDK -----------------------------------------------------------------------------

_STUB_INIT = """
import json
import logging
import os

from opentelemetry import trace as _trace
from opentelemetry.sdk.trace import TracerProvider as _TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# The logger state when the sender imports the SDK (logger tests).
CONTROL.at_import = {
    n: (logging.getLogger(n).propagate, [type(h) for h in logging.getLogger(n).handlers])
    for n in CONTROL.watch
}
for _name in CONTROL.import_loggers:  # loggers the SDK creates at import (logger tests)
    logging.getLogger(_name)

import langfuse.logger  # adds `console_handler` to `httpx`, as the pinned SDK does


class _Resources:
    def __init__(self):
        self._media_upload_enabled = os.environ.get("LANGFUSE_MEDIA_UPLOAD_ENABLED") != "false"
        self._media_upload_consumers = []


class _Call:
    def __init__(self, http, base_url):
        self._http = http
        self._base = base_url

    def send(self, method, path, body=None, key=None):
        base = CONTROL.api_base or self._base
        path = CONTROL.path_override.get(key, path)
        reply = self._http.request(method, base + path, json=body)
        if key in CONTROL.double:
            reply = self._http.request(method, base + path, json=body)
        if reply.status_code >= 300:
            # The error message carries the reply text (a marker in the tests).
            raise RuntimeError("stub sdk: STUB-SEND-ERROR-MARKER " + reply.text)
        return reply.status_code, (reply.json() if reply.content else {})


def _typed(key, default):
    import langfuse.api

    return getattr(langfuse.api, CONTROL.type_override.get(key, default))


class _Projects:
    def __init__(self, call):
        self._call = call

    def get(self, *, request_options=None):
        from langfuse.api import Project, Projects

        _, body = self._call.send("GET", "/api/public/projects")
        return Projects(data=[Project(**p) for p in body["data"]])


class _Health:
    def __init__(self, call):
        self._call = call

    def health(self, *, request_options=None):
        _, body = self._call.send("GET", "/api/public/health", None, "health")
        return _typed("health", "HealthResponse")(**body)


class _Datasets:
    def __init__(self, call):
        self._call = call

    def create(self, *, name, request_options=None):
        _, body = self._call.send("POST", "/api/public/v2/datasets", {"name": name}, "dataset")
        return _typed("dataset", "Dataset")(**body)


class _DatasetItems:
    def __init__(self, call):
        self._call = call

    def create(self, *, dataset_name, input=None, expected_output=None, metadata=None, id=None,
               request_options=None):
        payload = {"datasetName": dataset_name, "input": input, "expectedOutput": expected_output,
                   "metadata": metadata, "id": id}
        _, body = self._call.send("POST", "/api/public/dataset-items", payload, "dataset_item")
        return _typed("dataset_item", "DatasetItem")(**body)


class _Scores:
    def __init__(self, call):
        self._call = call

    def create(self, *, request, request_options=None):
        from langfuse.api import CreateScoreBatchResponse, CreateScoreBatchResults

        status, body = self._call.send(
            "POST", "/api/public/scores", [r.dump() for r in request], "scores"
        )
        if status == 207 or "results" in body:
            return CreateScoreBatchResults(**body)
        return CreateScoreBatchResponse(**body)


class _Api:
    def __init__(self, http, base_url):
        call = _Call(http, base_url)
        self.projects = _Projects(call)
        self.health = _Health(call)
        self.datasets = _Datasets(call)
        self.dataset_items = _DatasetItems(call)
        self.scores = _Scores(call)


class _Observation:
    def __init__(self, span):
        self._otel_span = _FailingSpan(span) if CONTROL.fail_set_attribute else span
        context = span.get_span_context()
        self.id = format(context.span_id, "016x")
        self.trace_id = format(context.trace_id, "032x")

    def end(self):
        if CONTROL.fail_end is not None:  # an SDK error whose text holds a marker
            raise RuntimeError("stub sdk: " + CONTROL.fail_end)
        if not CONTROL.never_end:
            self._otel_span.end()


class _FailingSpan:
    def __init__(self, span):
        self._span = span

    def set_attribute(self, key, value):
        raise RuntimeError("stub sdk: " + CONTROL.fail_set_attribute)

    def __getattr__(self, name):
        return getattr(self._span, name)


class Langfuse:
    def __init__(self, **kwargs):
        if CONTROL.fail_build is not None:  # a constructor error whose text holds a marker
            raise RuntimeError("stub sdk: " + CONTROL.fail_build)
        CONTROL.built.append(self)
        self.kwargs = kwargs
        self._provider = kwargs.get("tracer_provider")
        if self._provider is None:  # as the pinned SDK: its own provider, set as the global one
            self._provider = _TracerProvider()
            _trace.set_tracer_provider(self._provider)
        self._provider.add_span_processor(
            BatchSpanProcessor(kwargs["span_exporter"], schedule_delay_millis=600000)
        )
        self._resources = _Resources()
        self.api = _Api(kwargs["httpx_client"], kwargs["base_url"])
        for action in CONTROL.on_build:
            action()

    def start_observation(self, *, name, as_type, input=None, output=None, metadata=None):
        CONTROL.events.append("span_started")
        # What the sender gave the SDK, as the JSON text the attributes carry (risk S1).
        CONTROL.observations.append({"input": json.dumps(input), "output": json.dumps(output)})
        span = self._provider.get_tracer("stub-langfuse").start_span(name)
        span.set_attribute("langfuse.observation.input", json.dumps(input))
        span.set_attribute("langfuse.observation.output", json.dumps(output))
        if CONTROL.extra_span:  # a span the sender did not start, in the same batch
            self._provider.get_tracer("stub-langfuse").start_span("extra").end()
        self._last = span
        return _Observation(span)

    def create_score(self, **kwargs):
        CONTROL.events.append("queued_create_score")

    def flush(self):
        self._provider.force_flush()
        if CONTROL.double_export:
            self.kwargs["span_exporter"].export([self._last])
        for action in CONTROL.on_flush:
            action()

    def shutdown(self):
        CONTROL.events.append("shutdown")
        self._provider.shutdown()
"""

_STUB_LOGGER = """
import logging

_variant = CONTROL.logger_variant
if _variant != "no_console_handler":
    console_handler = logging.StreamHandler()
    logging.getLogger("httpx").addHandler(console_handler)
    if _variant == "twice":  # `addHandler` skips a handler already attached
        logging.getLogger("httpx").handlers.append(console_handler)
    if _variant == "also_langfuse":
        logging.getLogger("langfuse").addHandler(console_handler)
    if _variant == "second_stream_handler":
        logging.getLogger("httpx").addHandler(logging.StreamHandler())
    if _variant == "propagate":
        logging.getLogger("httpx").propagate = True
"""

_STUB_API = """
import enum


class _Model:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class Project(_Model):
    pass


class Projects(_Model):
    pass


class Dataset(_Model):
    pass


class HealthResponse(_Model):
    pass


class DatasetItem(_Model):
    pass


class CreateScoreBatchResponse(_Model):
    pass


class CreateScoreBatchResults(_Model):
    pass


class Unexpected(_Model):
    pass


class CreateScoreRequest(_Model):
    def dump(self):
        return {
            k: (v.value if isinstance(v, enum.Enum) else v) for k, v in self.__dict__.items()
        }


class ScoreDataType(enum.StrEnum):
    NUMERIC = "NUMERIC"
    BOOLEAN = "BOOLEAN"
    CATEGORICAL = "CATEGORICAL"
"""


@dataclass
class StubControl:
    """Shared with the stub SDK (set on each stub module before it runs)."""

    built: list[Any] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    on_build: list[Any] = field(default_factory=list)
    on_flush: list[Any] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    at_import: dict[str, Any] | None = None
    logger_variant: str = ""
    api_base: str | None = None
    path_override: dict[str, str] = field(default_factory=dict)
    type_override: dict[str, str] = field(default_factory=dict)
    double: set[str] = field(default_factory=set)
    extra_span: bool = False
    never_end: bool = False
    double_export: bool = False
    # What `start_observation` received, as JSON text: {"input": ..., "output": ...}.
    observations: list[dict[str, str]] = field(default_factory=list)
    # Logger names the stub `langfuse/__init__.py` creates when it is imported.
    import_loggers: list[str] = field(default_factory=list)
    # Marker texts: the constructor, `set_attribute` on the span, or `end` raise with it.
    fail_build: str | None = None
    fail_set_attribute: str | None = None
    fail_end: str | None = None


class _StubLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname: str, path: str, finder: StubFinder) -> None:
        super().__init__(fullname, path)
        self._finder = finder

    def exec_module(self, module: types.ModuleType) -> None:
        self._finder.executed.append(module.__name__)
        module.CONTROL = self._finder.control  # type: ignore[attr-defined]
        super().exec_module(module)


class StubFinder(importlib.abc.MetaPathFinder):
    """Finds `langfuse` and its submodules in the stub package with real specs; records each
    module it actually executes."""

    def __init__(self, root: Path, control: StubControl) -> None:
        self.root = root
        self.control = control
        self.executed: list[str] = []

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: types.ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "langfuse" and not fullname.startswith("langfuse."):
            return None
        parts = fullname.split(".")
        package_dir = self.root.joinpath(*parts)
        if package_dir.is_dir():
            file, search = package_dir / "__init__.py", [str(package_dir)]
        else:
            file, search = self.root.joinpath(*parts[:-1], f"{parts[-1]}.py"), None
        if not file.is_file():
            return None
        return importlib.util.spec_from_file_location(
            fullname,
            file,
            loader=_StubLoader(fullname, str(file), self),
            submodule_search_locations=search,
        )


def _write_stub(root: Path) -> Path:
    package = root / "langfuse"
    (package / "api").mkdir(parents=True)
    (package / "__init__.py").write_text(_STUB_INIT)
    (package / "logger.py").write_text(_STUB_LOGGER)
    (package / "api" / "__init__.py").write_text(_STUB_API)
    return package / "__init__.py"


# --- the network stand-ins --------------------------------------------------------------------


@dataclass
class LangfuseServer:
    """The `httpx.MockTransport` handler: Langfuse's public API, recording what it received."""

    score_status: int = 202
    dataset_status: int = 200
    item_status: int = 200
    score_body: dict[str, Any] | None = None
    # The health reply (the live check only): status, and the JSON body's `version` value.
    health_status: int = 200
    health_version: object = SERVER_VERSION
    # (score batch index, status, reply text): that batch gets this reply.
    fail_score_batch: tuple[int, int, str] | None = None
    # A dataset-item call for the case at this index gets 500 with the marker as its text.
    fail_item_index: int | None = None
    requests_seen: list[tuple[str, str, str]] = field(default_factory=list)
    score_batches: list[list[dict[str, Any]]] = field(default_factory=list)
    items_seen: list[dict[str, Any]] = field(default_factory=list)
    # Another path -> the path it is answered as (a reply that differs only in path).
    aliases: dict[str, str] = field(default_factory=dict)
    in_progress: int = 0
    max_in_progress: int = 0
    events: list[str] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.in_progress += 1
        self.max_in_progress = max(self.max_in_progress, self.in_progress)
        if self.events is not None:
            self.events.append("send")
        try:
            return self._handle(request)
        finally:
            self.in_progress -= 1

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests_seen.append((request.method, url.host, url.path))
        path = self.aliases.get(url.path, url.path)
        if request.method == "GET" and path == "/api/public/projects":
            return httpx.Response(200, json={"data": [PROJECT]})
        if request.method == "GET" and path == "/api/public/health":
            return httpx.Response(
                self.health_status, json={"version": self.health_version, "status": "OK"}
            )
        if request.method == "POST" and path == "/api/public/v2/datasets":
            return httpx.Response(
                self.dataset_status, json={"id": "dataset-1", "name": "divesafe-scenarios"}
            )
        if request.method == "POST" and path == "/api/public/dataset-items":
            body = json.loads(request.content)
            index = len(self.items_seen)
            self.items_seen.append(body)
            if self.fail_item_index == index:
                return httpx.Response(500, text=MARKER)
            return httpx.Response(self.item_status, json={"id": body["id"]})
        if request.method == "POST" and path == "/api/public/scores":
            self.score_batches.append(json.loads(request.content))
            failing = self.fail_score_batch
            if failing is not None and failing[0] == len(self.score_batches) - 1:
                return httpx.Response(failing[1], text=failing[2])
            if self.score_body is not None:
                return httpx.Response(self.score_status, json=self.score_body)
            if self.score_status == 207:
                return httpx.Response(207, json={"results": []})
            return httpx.Response(self.score_status, json={"message": "Accepted"})
        return httpx.Response(404)


class OtlpAdapter(requests.adapters.BaseAdapter):
    """The OTLP endpoint: answers each export with a fixed status and records the URL."""

    def __init__(self, status: int = 200) -> None:
        super().__init__()
        self.status = status
        self.urls: list[str] = []
        self.bodies: list[bytes] = []
        # Export index -> (status, reason): that export gets this reply.
        self.fail_at: dict[int, tuple[int, str]] = {}
        self.server: LangfuseServer | None = None

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        server = self.server
        if server is not None:
            server.in_progress += 1
            server.max_in_progress = max(server.max_in_progress, server.in_progress)
        try:
            return self._reply(request)
        finally:
            if server is not None:
                server.in_progress -= 1

    def _reply(self, request: requests.PreparedRequest) -> requests.Response:
        index = len(self.urls)
        self.urls.append(str(request.url))
        body = request.body
        if isinstance(body, str):
            body = body.encode()
        if body is not None and not isinstance(body, bytes):
            raise TypeError("the OTLP exporter sends a bytes body")
        self.bodies.append(body or b"")
        response = requests.Response()
        response.status_code, response.reason = self.fail_at.get(index, (self.status, "OK"))
        response._content = b""
        response.headers = CaseInsensitiveDict()
        response.url = str(request.url)
        response.request = request
        return response

    def close(self) -> None:
        pass


# --- fixtures ---------------------------------------------------------------------------------


@dataclass
class Rig:
    finder: StubFinder
    control: StubControl
    server: LangfuseServer
    adapter: OtlpAdapter
    output_dir: Path
    judge_calls: list[str]
    # Case index -> what the judge raises for that case (a BaseException in the partial-run tests).
    judge_raises: dict[int, BaseException] = field(default_factory=dict)
    judge_seconds: float = 0.0

    def judge(self, scenario: harness.Scenario, record: AssessmentRecord) -> list[JudgeScore]:
        import time

        index = len(self.judge_calls)
        self.judge_calls.append(scenario.name)
        self.control.events.append("judge")
        server = self.server
        server.in_progress += 1
        server.max_in_progress = max(server.max_in_progress, server.in_progress)
        try:
            if self.judge_seconds:
                time.sleep(self.judge_seconds)
            if index in self.judge_raises:
                raise self.judge_raises[index]
            return [JudgeScore("judge.example_flag", 1.0, "BOOLEAN", "screened rationale")]
        finally:
            server.in_progress -= 1

    def send(self, **overrides: Any) -> langfuse_sender.SendResult:
        arguments: dict[str, Any] = {
            "cases": harness.SCENARIOS,
            "provider_factory": lambda: fake(),
            "judge": self.judge,
            **LOCAL,
            "output_dir": self.output_dir,
            "httpx_transport": httpx.MockTransport(self.server.handle),
            "otlp_adapter": self.adapter,
        }
        arguments.update(overrides)
        cases = arguments.pop("cases")
        factory = arguments.pop("provider_factory")
        judge = arguments.pop("judge")
        return run_send(cases, factory, judge, **arguments)

    def nothing_happened(self) -> bool:
        return (
            not self.finder.executed
            and not self.server.requests_seen
            and not self.adapter.urls
            and not self.judge_calls
        )


def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith(("LANGFUSE_", "OTEL_")) or name.lower() in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            monkeypatch.delenv(name)
    for name in langfuse_checks.UNSET_DIVESAFE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", dict)


def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    """The rig as a generator; each test module wraps it in its own `rig` fixture."""
    _clean_environment(monkeypatch)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, text in ((PUBLIC_KEY_FILE, "pk-lf-test\n"), (SECRET_KEY_FILE, "sk-lf-test\n")):
        (secrets / name).write_text(text)
        (secrets / name).chmod(0o600)  # the key-file read refuses group or other permission bits
    monkeypatch.setenv(langfuse_checks.SECRETS_DIR_VARIABLE, str(secrets))

    init_file = _write_stub(tmp_path / "stub_sdk")
    control = StubControl()
    finder = StubFinder(tmp_path / "stub_sdk", control)
    monkeypatch.setattr(langfuse_checks, "read_installed_version", lambda: "5.2.0")
    monkeypatch.setattr(langfuse_checks, "distribution_init_file", lambda: init_file)
    monkeypatch.setattr(langfuse_checks, "run_git", _git_stub(COMMIT, ""))
    monkeypatch.setattr(langfuse_sender, "utc_now", lambda: STARTED)
    monkeypatch.setattr(uuid, "uuid4", lambda: RUN_UUID)

    saved = {
        name: (logging.getLogger(name).propagate, list(logging.getLogger(name).handlers))
        for name in EVAL_SILENCED_LOGGERS
    }
    known = {
        name: (item.propagate, list(item.handlers))
        for name, item in list(logging.Logger.manager.loggerDict.items())
        if isinstance(item, logging.Logger)
    }
    root_handlers = list(logging.getLogger().handlers)
    showwarning = warnings.showwarning
    tracer_state = (trace_api._TRACER_PROVIDER, trace_api._TRACER_PROVIDER_SET_ONCE)
    trace_api._TRACER_PROVIDER, trace_api._TRACER_PROVIDER_SET_ONCE = None, Once()
    sys.meta_path.insert(0, finder)
    server = LangfuseServer()
    adapter = OtlpAdapter()
    adapter.server = server
    try:
        yield Rig(
            finder,
            control,
            server,
            adapter,
            tmp_path / "out",
            [],
        )
    finally:
        sys.meta_path.remove(finder)
        for name in [n for n in sys.modules if n == "langfuse" or n.startswith("langfuse.")]:
            del sys.modules[name]
        for client in control.built:
            client._provider.shutdown()
        for name, (propagate, handlers) in saved.items():
            library = logging.getLogger(name)
            library.propagate = propagate
            library.handlers[:] = handlers
        logging.captureWarnings(False)
        warnings.showwarning = showwarning
        logging.getLogger().handlers[:] = root_handlers
        for name, item in list(logging.Logger.manager.loggerDict.items()):
            if not isinstance(item, logging.Logger):
                continue
            if name in known:
                item.propagate, item.handlers[:] = known[name][0], known[name][1]
            else:  # a logger a test created: no handler left behind
                item.handlers[:] = []
        trace_api._TRACER_PROVIDER, trace_api._TRACER_PROVIDER_SET_ONCE = tracer_state


def _git_stub(commit: str, status: str) -> Any:
    def run(argv: Sequence[str], env: dict[str, str]) -> str | None:
        if "rev-parse" in argv:
            return commit + "\n"
        return status

    return run


def _assert_media_variable_removed() -> None:
    import os

    assert MEDIA_VARIABLE not in os.environ
