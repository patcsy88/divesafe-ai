"""A stub wire for the trace exporter (not a test file; ADR 0027 amendment, step 3).

`install` swaps the HTTP adapter the export's session mounts for `StubAdapter`, so the real
session, OTLP exporter, batch processor and tracer provider run and no socket is opened. The
adapter keeps every request it is handed and answers by `Wire.mode`. `install` also passes the
subnet probe, resets the drop counter and restores the library loggers afterwards.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk import trace as sdk_trace
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict
from tests.agent.scripted_llm import NOW

from divesafe.api import trace_export
from divesafe.api import tracing as tracing_module
from divesafe.config import Settings
from divesafe.observability import ModelCallEntry, RunTrace, StageEntry
from divesafe.observability import collector as collector_module

ADDRESS = "172.30.77.10"
ENDPOINT = f"http://{ADDRESS}:3000/api/public/otel/v1/traces"
PUBLIC_KEY, SECRET_KEY = "pk-lf-0000", "sk-lf-0000"
ON = {
    "tracing_enabled": True,
    "trace_endpoint": ENDPOINT,
    "trace_public_key": PUBLIC_KEY,
    "trace_secret_key": SECRET_KEY,
}
BLOCK_SECONDS = 10.0
LIBRARY_LOGGERS = ("opentelemetry", "urllib3", "requests")


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**ON, **overrides})  # type: ignore[arg-type, call-arg]


@dataclass
class Wire:
    """`mode`: "ok" answers `status` (with `location`, if set); "raise" raises `error`; "block"
    waits for `release` (at most `BLOCK_SECONDS`), then answers 200."""

    mode: str = "ok"
    status: int = 200
    location: str | None = None
    error: Exception | None = None
    release: threading.Event = field(default_factory=threading.Event)
    sent: list[requests.PreparedRequest] = field(default_factory=list)
    send_options: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[requests.Session] = field(default_factory=list)
    adapter_kwargs: list[dict[str, Any]] = field(default_factory=list)
    providers: list[sdk_trace.TracerProvider] = field(default_factory=list)
    exports: list[trace_export.TraceExport] = field(default_factory=list)

    def build(
        self,
        model: object = "model-x",
        *,
        module: Any = trace_export,
        host: str | None = "llm.example",
        **overrides: Any,
    ) -> trace_export.TraceExport:
        export = module.build_trace_export(settings(**overrides), {}, model=model, host=host)
        assert export is not None
        self.exports.append(export)
        return export

    def decoded(self) -> list[ExportTraceServiceRequest]:
        return [ExportTraceServiceRequest.FromString(bytes(r.body or b"")) for r in self.sent]

    def spans(self) -> list[Any]:
        return [
            span
            for request in self.decoded()
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]

    def body_text(self) -> str:
        """Every byte sent (bodies, URLs and headers), for substring scans."""
        parts: list[str] = []
        for request in self.sent:
            parts.append(str(request.url))
            parts.extend(f"{k}: {v}" for k, v in request.headers.items())
            parts.append(bytes(request.body or b"").decode("utf-8", errors="replace"))
        return "\n".join(parts)


class StubAdapter(BaseAdapter):
    def __init__(self, wire: Wire) -> None:
        super().__init__()
        self._wire = wire

    def send(self, request: requests.PreparedRequest, **options: Any) -> requests.Response:
        wire = self._wire
        wire.sent.append(request)
        wire.send_options.append(options)
        if wire.mode == "raise":
            assert wire.error is not None
            raise wire.error
        if wire.mode == "block":
            wire.release.wait(BLOCK_SECONDS)
        response = requests.Response()
        response.status_code = wire.status if wire.mode == "ok" else 200
        response.reason = "stub"
        response.request = request
        response.url = str(request.url)
        response._content = b""
        headers: CaseInsensitiveDict[str] = CaseInsensitiveDict()
        if wire.location is not None:
            headers["Location"] = wire.location
        response.headers = headers
        return response

    def close(self) -> None:
        return None


def install(mp: pytest.MonkeyPatch, wire: Wire) -> Wire:
    mp.setattr(tracing_module, "_has_interface_in_subnet", lambda *args: True)

    def adapter(**kwargs: Any) -> StubAdapter:
        wire.adapter_kwargs.append(kwargs)
        return StubAdapter(wire)

    mp.setattr(requests.adapters, "HTTPAdapter", adapter)
    real_mount = requests.Session.mount

    def mount(session: requests.Session, prefix: str, adapter: BaseAdapter) -> None:
        if session not in wire.sessions:
            wire.sessions.append(session)
        real_mount(session, prefix, adapter)

    mp.setattr(requests.Session, "mount", mount)
    real_init = sdk_trace.TracerProvider.__init__

    def init(provider: sdk_trace.TracerProvider, *args: Any, **kwargs: Any) -> None:
        wire.providers.append(provider)
        real_init(provider, *args, **kwargs)

    mp.setattr(sdk_trace.TracerProvider, "__init__", init)
    mp.setattr(collector_module, "_drops", dict.fromkeys(collector_module.DROP_REASONS, 0))
    for name in LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        mp.setattr(library, "propagate", library.propagate)
        mp.setattr(library, "handlers", list(library.handlers))
    return wire


@contextmanager
def installed_wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[Wire]:
    """For a test module's own `wire` fixture: every export built is shut down afterwards."""
    installed = install(monkeypatch, Wire())
    try:
        yield installed
    finally:
        installed.release.set()
        for export in installed.exports:
            export.shutdown(1.0)


# ---- synthetic traces --------------------------------------------------------------------------


def stage(name: Any, outcome: Any, duration_ms: Any = 2, ended_ns: Any = None) -> StageEntry:
    ended = time.time_ns() if ended_ns is None else ended_ns
    return StageEntry(name, outcome, NOW, NOW, duration_ms, ended)


def call(**fields: object) -> ModelCallEntry:
    return ModelCallEntry(fields, time.time_ns())


def run_trace(
    *,
    assessment_id: str = "a1b2c3",
    run_outcome: str = "stored",
    stages: tuple[StageEntry, ...] = (),
    model_calls: tuple[ModelCallEntry, ...] = (),
) -> RunTrace:
    started = time.time_ns() - 1_000_000_000
    return RunTrace(assessment_id, run_outcome, started, time.time_ns(), model_calls, stages)


def attributes(span: Any) -> dict[str, object]:
    """Each attribute's value, by its one set field (`string_value`, `int_value`, ...)."""
    found: dict[str, object] = {}
    for kv in span.attributes:
        which = kv.value.WhichOneof("value")
        found[kv.key] = None if which is None else getattr(kv.value, which)
    return found
