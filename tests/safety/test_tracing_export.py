"""ADR 0027 amendment, step 3: what the trace exporter sends, and what it never does.

The real session, OTLP exporter, batch processor and tracer provider run against a stub HTTP
adapter (`tests.safety.trace_wire`), so no socket is opened. Each body is decoded as an OTLP
`ExportTraceServiceRequest` and pinned whole: span names, attribute keys per span kind, the
resource, the scope, and no events, links or status. Markers placed in a real run's replies, in
the model-call fields, in the assessment id and in the model name never reach the wire; the model
name appears only as `gen_ai.request.model`. Library log lines never reach the app's handlers or
stderr. The global OpenTelemetry providers are never set, FastAPI's own telemetry is off, and
nothing is built (or, with tracing off, imported) unless the start-up check passes. The allow-
lists are literals here, never imported, so a code change cannot move the test.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient
from opentelemetry import metrics as metrics_api
from opentelemetry import trace as trace_api
from opentelemetry.exporter.otlp.common import http as otlp_http
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.agent import scripted_llm as sl
from tests.safety import trace_wire as tw
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_cross_examination import _pipeline_connectors as site_connectors

from divesafe.api import app as app_module
from divesafe.api import trace_export
from divesafe.data import TIOMAN_ISLAND
from divesafe.domain import AssessmentRecord, StageName, StageOutcome
from divesafe.models import FakeProvider
from divesafe.observability import RunTrace, start_collecting
from divesafe.observability import collector as collector_module
from divesafe.orchestration import RunOptions, assess_dive

pytestmark = pytest.mark.safety

SRC = Path(__file__).resolve().parents[2] / "src" / "divesafe"
ROOT = SRC.parents[1]

REPLY_MARKER = "REPLY-MARKER-q7"
PROMPT_MARKER = "PROMPT-MARKER-k3"
EVIDENCE_ID = "ev-MARKER-z9"
EXCEPTION_MARKER = "EXCEPTION-MARKER-p5"
MODEL_MARKER = "model-marker-m4"
URL_MODEL = "https://llm.example/v1/model"
ASSESSMENT_ID = "0123456789abcdef0123456789abcdef"
REQUEST_ID = "proxy-Request-7f3a"
# Not a valid request id (a space), so it must never reach the wire (ADR 0027 item 1).
REQUEST_ID_MARKER = "REQUEST-ID MARKER-r8"

RUN_KEYS = {"run_outcome", "prompt_version", "assessment_id", "request_id"}
STAGE_KEYS = {"stage", "outcome", "duration_ms"}
MODEL_KEYS = {
    "task",
    "outcome",
    "provider_reason",
    "output_limit",
    "output_chars",
    "duration_ms",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.input_tokens",
    "gen_ai.request.model",
}
STAGES = {"observe", "retrieve", "assess", "reason", "rejudge", "propose", "supervise", "gate"}
SPECIALISTS = {"site_intelligence", "weather", "ocean_conditions", "tide_current", "prediction"}
TASKS = {
    "risk_assessment",
    *(f"specialist:{name}" for name in SPECIALISTS),
    *(f"cross_examination:{name}" for name in SPECIALISTS),
    *(f"scenario:{kind}" for kind in ("favourable", "marginal", "deteriorating")),
}
RESOURCE = {
    "service.name": "divesafe-api",
    "telemetry.sdk.language": "python",
    "telemetry.sdk.name": "opentelemetry",
}
LIBRARY_LOGGERS = (
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk._shared_internal",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.common.http",
    "urllib3.connectionpool",
    "requests",
)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[tw.Wire]:
    with tw.installed_wire(monkeypatch) as installed:
        yield installed


def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The library retries transient failures with 1 s, 2 s, ... waits inside the 3 s timeout;
    tests skip the waits, not the retries."""
    monkeypatch.setattr(otlp_http._OTLPHTTPClient, "_compute_backoff", lambda self, retry: 0.0)


def _flush(export: trace_export.TraceExport) -> None:
    assert export.shutdown(5.0), "the export did not finish"


# ---- a real run with cross-examination, markers in every reply ---------------------------------


def _marked_reply(request: Any) -> str:
    text = sl.scripted()(request)
    for field in ('"summary": "', '"rationale": "'):
        text = text.replace(field, f"{field}{REPLY_MARKER} ")
    return text


def _real_run() -> tuple[RunTrace, AssessmentRecord, FakeProvider]:
    provider = FakeProvider(_marked_reply, model=MODEL_MARKER)

    async def run() -> tuple[RunTrace, AssessmentRecord]:
        collector = start_collecting()
        record = await assess_dive(
            assessment_id=ASSESSMENT_ID,
            plan=sl.PLAN,
            site=TIOMAN_ISLAND,
            connectors=site_connectors(),
            engine=sl.engine(with_warning_rule=True),
            now=sl.NOW,
            provider=provider,
            clock=lambda: sl.NOW,
            egress_basis="declared_local",
            model_host="localhost",
            options=RunOptions(cross_examination=True),
        )
        return collector.finish(ASSESSMENT_ID, "stored", request_id=REQUEST_ID), record

    trace, record = asyncio.run(run())
    return trace, record, provider


def _planted() -> RunTrace:
    """Every field a mutant could leak holds a marker or a wrong type."""
    return tw.run_trace(
        assessment_id=EVIDENCE_ID,
        run_outcome="NO-GO",
        request_id=REQUEST_ID_MARKER,
        stages=(
            tw.stage(StageName.OBSERVE, StageOutcome.COMPLETED),
            tw.stage(EVIDENCE_ID, StageOutcome.COMPLETED),
            tw.stage(StageName.REASON, REPLY_MARKER, duration_ms=PROMPT_MARKER),
        ),
        model_calls=(
            tw.call(
                task=EVIDENCE_ID,
                outcome=REPLY_MARKER,
                provider_reason=EXCEPTION_MARKER,
                output_limit=PROMPT_MARKER,
                output_chars=True,
                output_used=-1,
                duration_ms=3,
                reply=REPLY_MARKER,
                prompt=PROMPT_MARKER,
                evidence_ids=(EVIDENCE_ID,),
                error=EXCEPTION_MARKER,
                recommendation="NO-GO",
            ),
            tw.call(
                task="risk_assessment",
                outcome="ok",
                output_limit=6000,
                output_chars=10,
                output_used=3,
                input_used=40,
                duration_ms=5,
                reply=REPLY_MARKER,
            ),
            # A provider error with free text as its label: only the label check stops it.
            tw.call(
                task="scenario:marginal",
                outcome="provider_error",
                provider_reason=EXCEPTION_MARKER,
                duration_ms=7,
            ),
            # A provider error with a real label: exported, as the set's string.
            tw.call(
                task="scenario:favourable",
                outcome="provider_error",
                provider_reason="read_timeout",
                duration_ms=8,
            ),
        ),
    )


def _scan_markers(record: AssessmentRecord) -> list[str]:
    return [
        REPLY_MARKER,
        PROMPT_MARKER,
        EVIDENCE_ID,
        EXCEPTION_MARKER,
        REQUEST_ID_MARKER,
        URL_MODEL,
        "llm.example",
        "INSUFFICIENT EVIDENCE",
        "NO-GO",
        "CAUTION",
        TIOMAN_ISLAND.id,
        TIOMAN_ISLAND.name,
        "localhost",
        "declared_local",
        tw.PUBLIC_KEY,
        tw.SECRET_KEY,
        *(item.id for item in record.evidence),
        *(item.source for item in record.evidence),
    ]


def _export_both(
    wire: tw.Wire,
    module: Any = trace_export,
    model: object = MODEL_MARKER,
    host: str | None = "llm.example",
) -> Any:
    trace, record, provider = _real_run()
    export = wire.build(model, module=module, host=host)
    export.sink.emit(trace)
    export.sink.emit(_planted())
    _flush(export)
    return record, provider


def _leaks(wire: tw.Wire, record: AssessmentRecord) -> list[str]:
    body = "\n".join(bytes(r.body or b"").decode("utf-8", errors="replace") for r in wire.sent)
    return [marker for marker in _scan_markers(record) if marker in body]


def test_the_whole_export_holds_only_the_allow_list(wire: tw.Wire) -> None:
    record, provider = _export_both(wire)
    requests_ = wire.decoded()
    assert requests_, "nothing was exported"
    assert _leaks(wire, record) == []
    for request in requests_:
        for resource_spans in request.resource_spans:
            resource = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
            assert set(resource) == {*RESOURCE, "telemetry.sdk.version"}
            assert {k: resource[k] for k in RESOURCE} == RESOURCE
            assert resource_spans.schema_url == ""
            for scope_spans in resource_spans.scope_spans:
                assert scope_spans.scope.name == "divesafe.api"
                assert scope_spans.scope.version == ""
                assert list(scope_spans.scope.attributes) == []
                assert scope_spans.schema_url == ""
    spans = wire.spans()
    runs = [s for s in spans if s.name == "run"]
    assert len(runs) == 2
    names: set[str] = set()
    for span in spans:
        attributes = tw.attributes(span)
        assert list(span.events) == [] and list(span.links) == []
        assert (span.status.code, span.status.message) == (0, "")
        assert span.kind == 1  # SPAN_KIND_INTERNAL
        assert span.trace_state == ""
        assert (span.dropped_events_count, span.dropped_links_count) == (0, 0)
        assert span.start_time_unix_nano <= span.end_time_unix_nano
        if span.name == "run":
            assert span.parent_span_id == b""
            assert set(attributes) <= RUN_KEYS
            assert all(type(v) is str for v in attributes.values())
        elif span.name in STAGES:
            assert set(attributes) <= STAGE_KEYS
            assert attributes.get("stage") == span.name
        else:
            assert span.name in TASKS or span.name == "unlisted_task", span.name
            assert set(attributes) <= MODEL_KEYS
            assert attributes.get("gen_ai.request.model") == MODEL_MARKER
            assert all(
                type(attributes[k]) is int
                for k in MODEL_KEYS - {"task", "outcome", "provider_reason", "gen_ai.request.model"}
                if k in attributes
            )
            if "provider_reason" in attributes:
                assert type(attributes["provider_reason"]) is str
        if span.name != "run":
            parents = [r for r in runs if r.trace_id == span.trace_id]
            assert len(parents) == 1 and span.parent_span_id == parents[0].span_id
        for key, value in attributes.items():
            if value == MODEL_MARKER:
                assert key == "gen_ai.request.model"
        names.add(span.name)
    assert names >= TASKS, "the real run's model calls were not all exported"
    assert len(provider.requests) == 14


def test_the_real_run_and_the_planted_run_export_exactly_these_attributes(
    wire: tw.Wire,
) -> None:
    _export_both(wire)
    by_run: dict[bytes, list[Any]] = {}
    for span in wire.spans():
        by_run.setdefault(span.trace_id, []).append(span)
    planted = next(
        spans
        for spans in by_run.values()
        if tw.attributes(next(s for s in spans if s.name == "run")).get("assessment_id") is None
    )
    real = next(spans for spans in by_run.values() if spans is not planted)
    run = tw.attributes(next(s for s in real if s.name == "run"))
    assert run == {
        "run_outcome": "stored",
        "prompt_version": trace_export.PROMPT_VERSION,
        "assessment_id": ASSESSMENT_ID,
        "request_id": REQUEST_ID,
    }
    planted_run = tw.attributes(next(s for s in planted if s.name == "run"))
    assert planted_run == {"prompt_version": trace_export.PROMPT_VERSION}
    planted_by_name = {s.name: tw.attributes(s) for s in planted}
    assert set(planted_by_name) == {
        "run",
        "observe",
        "reason",
        "unlisted_task",
        "risk_assessment",
        "scenario:marginal",
        "scenario:favourable",
    }
    assert planted_by_name["scenario:marginal"] == {
        "task": "scenario:marginal",
        "outcome": "provider_error",
        "duration_ms": 7,
        "gen_ai.request.model": MODEL_MARKER,
    }
    assert planted_by_name["scenario:favourable"] == {
        "task": "scenario:favourable",
        "outcome": "provider_error",
        "provider_reason": "read_timeout",
        "duration_ms": 8,
        "gen_ai.request.model": MODEL_MARKER,
    }
    assert planted_by_name["reason"] == {"stage": "reason"}
    assert planted_by_name["unlisted_task"] == {
        "duration_ms": 3,
        "gen_ai.request.model": MODEL_MARKER,
    }
    assert planted_by_name["risk_assessment"] == {
        "task": "risk_assessment",
        "outcome": "ok",
        "output_limit": 6000,
        "output_chars": 10,
        "duration_ms": 5,
        "gen_ai.usage.output_tokens": 3,
        "gen_ai.usage.input_tokens": 40,
        "gen_ai.request.model": MODEL_MARKER,
    }


def _reason_on_other_outcomes(module: Any) -> list[object]:
    found: list[object] = []
    for outcome in ("ok", "schema", "token_limit", EXCEPTION_MARKER, None):
        entry = tw.call(task="risk_assessment", outcome=outcome, provider_reason="read_timeout")
        if "provider_reason" in module.model_call_attributes(entry, None):
            found.append(outcome)
    return found


def test_the_provider_reason_is_exported_only_for_a_provider_error() -> None:
    assert _reason_on_other_outcomes(trace_export) == []


def test_a_mutant_without_the_outcome_check_is_caught() -> None:
    mutant = mutated_module(
        trace_export, ('if outcome == "provider_error" and type(reason)', "if type(reason)")
    )
    assert _reason_on_other_outcomes(mutant) == [
        "ok",
        "schema",
        "token_limit",
        EXCEPTION_MARKER,
        None,
    ]


class _OddReason(str):
    """A string subclass with a real label's value: never exported as itself."""


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("read_timeout", "read_timeout"),
        ("unclassified", "unclassified"),
        (EXCEPTION_MARKER, None),
        (7, None),
        (None, None),
        (_OddReason("http_error"), None),
    ],
    ids=["label", "unclassified", "marker", "int", "none", "str_subclass"],
)
def test_the_provider_reason_is_exported_only_as_a_fixed_label(
    reason: object, expected: str | None
) -> None:
    entry = tw.call(task="risk_assessment", outcome="provider_error", provider_reason=reason)
    attributes = trace_export.model_call_attributes(entry, None)
    assert attributes.get("provider_reason") == expected
    if expected is not None:
        assert type(attributes["provider_reason"]) is str


def test_the_code_allow_lists_equal_the_pinned_literals() -> None:
    assert set(trace_export.RUN_ATTRIBUTES) == RUN_KEYS
    assert set(trace_export.STAGE_ATTRIBUTES) == STAGE_KEYS
    assert set(trace_export.MODEL_CALL_ATTRIBUTES) == MODEL_KEYS
    assert trace_export.TASK_NAMES == TASKS
    assert trace_export.STAGE_NAMES == STAGES
    assert trace_export.SILENCED_LOGGERS == ("opentelemetry", "urllib3", "requests")


@pytest.mark.parametrize(
    "model",
    [URL_MODEL, "a b", "", "x" * 129, 7, None, "m\nx", "llm.example/qwen", "u@LLM.example:8080/m"],
)
def test_a_model_name_that_is_not_a_plain_short_name_is_never_exported(
    model: object, wire: tw.Wire
) -> None:
    export = wire.build(model)
    export.sink.emit(_planted())
    _flush(export)
    for span in wire.spans():
        assert "gen_ai.request.model" not in tw.attributes(span)
    assert "llm.example" not in wire.body_text()


def test_a_plain_model_name_without_the_host_is_exported(wire: tw.Wire) -> None:
    export = wire.build("qwen3.5:9b")
    export.sink.emit(_planted())
    _flush(export)
    models = {tw.attributes(s).get("gen_ai.request.model") for s in wire.spans()}
    assert "qwen3.5:9b" in models


def test_a_mutant_without_the_host_check_is_caught(wire: tw.Wire) -> None:
    mutant = mutated_module(
        trace_export, ("    if host.lower() in model.lower():\n", "    if False:\n")
    )
    export = wire.build("llm.example/qwen", module=mutant)
    export.sink.emit(_planted())
    _flush(export)
    assert "llm.example" in wire.body_text()


def test_with_no_model_host_no_model_name_is_exported(wire: tw.Wire) -> None:
    export = wire.build("qwen3.5:9b", host=None)
    export.sink.emit(_planted())
    _flush(export)
    assert wire.spans() != []
    for span in wire.spans():
        assert "gen_ai.request.model" not in tw.attributes(span)
    assert "qwen3.5:9b" not in wire.body_text()


def test_a_mutant_without_the_no_host_refusal_is_caught(wire: tw.Wire) -> None:
    mutant = mutated_module(
        trace_export,
        ("    if not host:\n        return None\n", ""),
        (
            "    if host.lower() in model.lower():\n",
            "    if host and host.lower() in model.lower():\n",
        ),
    )
    export = wire.build("qwen3.5:9b", module=mutant, host=None)
    export.sink.emit(_planted())
    _flush(export)
    assert "qwen3.5:9b" in wire.body_text()


def test_the_headers_are_exactly_these(wire: tw.Wire) -> None:
    export = wire.build()
    export.sink.emit(_planted())
    _flush(export)
    (request,) = wire.sent
    assert request.method == "POST" and request.url == tw.ENDPOINT
    headers = {k.lower(): v for k, v in request.headers.items()}
    assert set(headers) == {
        "authorization",
        "content-type",
        "user-agent",
        "x-langfuse-ingestion-version",
        "accept",
        "accept-encoding",
        "connection",
        "content-length",
    }
    assert headers["authorization"] == "Basic cGstbGYtMDAwMDpzay1sZi0wMDAw"
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert headers["content-type"] == "application/x-protobuf"


# ---- mutation checks: the scan and the pins catch a leak ---------------------------------------

_MODEL_ATTRIBUTE = '        attributes["gen_ai.request.model"] = model\n'


def _one_field(field: str) -> tuple[tuple[str, str], ...]:
    """Export one more planted field as text: one mutant per field, so the span's attribute
    limit (which drops the oldest key) cannot hide the field under test."""
    line = f"    attributes[{field!r}] = str(fields.get({field!r}))\n"
    return ((_MODEL_ATTRIBUTE, _MODEL_ATTRIBUTE + line),)


MUTANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "field_reply": _one_field("reply"),
    "field_prompt": _one_field("prompt"),
    "field_evidence_ids": _one_field("evidence_ids"),
    "field_error": _one_field("error"),
    "field_recommendation": _one_field("recommendation"),
    "any_run_outcome": (
        ("type(trace.run_outcome) is str and trace.run_outcome in RUN_OUTCOMES", "True"),
    ),
    "any_assessment_id": (
        (
            "type(trace.assessment_id) is str and _ASSESSMENT_ID.fullmatch(trace.assessment_id)",
            "True",
        ),
    ),
    "any_request_id": (("    if valid_request_id(trace.request_id):\n", "    if True:\n"),),
    "any_task_name": (
        (
            "    return task if type(task) is str and task in TASK_NAMES else UNLISTED_TASK\n",
            "    return str(task)\n",
        ),
    ),
    "any_model_name": (('_MODEL_NAME.fullmatch(model) is None or "//" in model', "False"),),
    "any_provider_reason": (
        (
            '    if outcome == "provider_error" and type(reason) is str and reason in '
            "PROVIDER_FAILURE_REASONS:\n"
            '        attributes["provider_reason"] = next(r for r in PROVIDER_FAILURE_REASONS '
            "if r == reason)\n",
            '    if reason is not None:\n        attributes["provider_reason"] = str(reason)\n',
        ),
    ),
    "any_provider_reason_label": (
        (
            '    if outcome == "provider_error" and type(reason) is str and reason in '
            "PROVIDER_FAILURE_REASONS:\n"
            '        attributes["provider_reason"] = next(r for r in PROVIDER_FAILURE_REASONS '
            "if r == reason)\n",
            '    if outcome == "provider_error" and reason is not None:\n'
            '        attributes["provider_reason"] = str(reason)\n',
        ),
    ),
    "event_without_limit": (
        ("        max_events=0,\n", ""),
        (
            "    parent = trace_api.set_span_in_context(run, Context())\n",
            "    parent = trace_api.set_span_in_context(run, Context())\n"
            "    run.add_event('exception', {'exception.message': 'x'})\n",
        ),
    ),
}


# What each mutant must be caught by: a leaked marker, or an event name.
EXPECTED = {
    "field_reply": REPLY_MARKER,
    "field_prompt": PROMPT_MARKER,
    "field_evidence_ids": EVIDENCE_ID,
    "field_error": EXCEPTION_MARKER,
    "field_recommendation": "NO-GO",
    "any_run_outcome": "NO-GO",
    "any_assessment_id": EVIDENCE_ID,
    "any_request_id": REQUEST_ID_MARKER,
    "any_task_name": EVIDENCE_ID,
    "any_model_name": URL_MODEL,
    "any_provider_reason": EXCEPTION_MARKER,
    "any_provider_reason_label": EXCEPTION_MARKER,
    "event_without_limit": "exception",
}


def test_every_mutant_names_what_catches_it() -> None:
    assert set(EXPECTED) == set(MUTANTS)


@pytest.mark.parametrize("name", list(MUTANTS))
def test_a_mutant_that_widens_the_export_is_caught(name: str, wire: tw.Wire) -> None:
    # The unmutated export leaks nothing and has no events (pinned above), so finding the
    # expected marker or event shows this mutant was caught for its own reason.
    mutant = mutated_module(trace_export, *MUTANTS[name])
    model = URL_MODEL if name == "any_model_name" else MODEL_MARKER
    # With an unrelated host, so only the mutated pattern check stands between the URL and a span.
    host = "other.example" if name == "any_model_name" else "llm.example"
    record, _ = _export_both(wire, mutant, model, host)
    events = [event.name for span in wire.spans() for event in span.events]
    assert EXPECTED[name] in _leaks(wire, record) + events


# ---- library logs never reach the app's handlers or stderr -------------------------------------


def _logged_anywhere(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str], marker: str
) -> bool:
    captured = capsys.readouterr()
    return marker in caplog.text or marker in captured.err or marker in captured.out


def test_library_log_lines_reach_no_handler_and_no_stderr(
    wire: tw.Wire, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.build()
    with caplog.at_level(logging.DEBUG):
        for name in LIBRARY_LOGGERS:
            logging.getLogger(name).warning("%s %s", EXCEPTION_MARKER, tw.ENDPOINT)
    assert not _logged_anywhere(caplog, capsys, EXCEPTION_MARKER)


@pytest.mark.parametrize("error", ["connection", "status"])
def test_a_failed_export_logs_only_the_fixed_drop_line(
    error: str,
    wire: tw.Wire,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_backoff(monkeypatch)
    if error == "connection":
        wire.mode = "raise"
        wire.error = requests.ConnectionError(f"{EXCEPTION_MARKER} {tw.ADDRESS}")
    else:
        wire.status = 500
    with caplog.at_level(logging.DEBUG):
        export = wire.build()
        export.sink.emit(_planted())
        _flush(export)
    assert wire.sent
    assert not _logged_anywhere(caplog, capsys, EXCEPTION_MARKER)
    assert tw.ADDRESS not in caplog.text
    drops = [r for r in caplog.records if r.getMessage() == "trace data dropped"]
    assert [r.reason for r in drops] == ["connection" if error == "connection" else "http_status"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("old", "logger_name"),
    [
        ("        library.propagate = False\n", "opentelemetry.sdk.trace.export"),
        (
            "        if not any(isinstance(h, logging.NullHandler) for h in library.handlers):\n"
            "            library.addHandler(logging.NullHandler())\n",
            "opentelemetry.sdk.trace.export",
        ),
    ],
)
def test_a_mutant_that_does_not_silence_the_library_is_caught(
    old: str,
    logger_name: str,
    wire: tw.Wire,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutant = mutated_module(trace_export, (old, "        pass\n"))
    mutant.silence_library_loggers()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger(logger_name).warning(EXCEPTION_MARKER)
    assert _logged_anywhere(caplog, capsys, EXCEPTION_MARKER)


# ---- the session: one endpoint, no environment, no redirect ------------------------------------


def _session(wire: tw.Wire) -> requests.Session:
    wire.build()
    (session,) = [s for s in wire.sessions if isinstance(s, requests.Session)]
    return session


def test_the_session_ignores_the_environment_and_mounts_only_plain_http(
    wire: tw.Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE"):
        monkeypatch.setenv(name, "http://203.0.113.9:3128" if "PROXY" in name.upper() else "x")
    monkeypatch.setenv("NO_PROXY", "")
    session = _session(wire)
    assert session.trust_env is False and session.proxies == {}
    assert session.max_redirects == 0
    assert list(session.adapters) == ["http://"]
    assert wire.adapter_kwargs == [{"max_retries": 0}]
    export = wire.exports[-1]
    export.sink.emit(_planted())
    _flush(export)
    (options,) = wire.send_options
    assert options["proxies"] == {} and options["timeout"] is not None
    assert float(options["timeout"]) <= trace_export.EXPORT_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "http://172.30.77.11:3000/api/public/otel/v1/traces"),
        ("POST", tw.ENDPOINT + "?x=1"),
        ("POST", "https://172.30.77.10:3000/api/public/otel/v1/traces"),
        ("GET", tw.ENDPOINT),
    ],
)
def test_the_session_refuses_anything_but_a_post_to_the_endpoint(
    method: str, url: str, wire: tw.Wire
) -> None:
    session = _session(wire)
    with pytest.raises(requests.ConnectionError):
        session.request(method, url)
    assert wire.sent == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_never_followed_and_counts_as_http_status(status: int, wire: tw.Wire) -> None:
    wire.status, wire.location = status, "http://203.0.113.9:3000/api/public/otel/v1/traces"
    export = wire.build()
    export.sink.emit(_planted())
    _flush(export)
    assert [r.url for r in wire.sent] == [tw.ENDPOINT]
    assert collector_module.drop_counts()["http_status"] == 1


def test_a_mutant_that_follows_redirects_is_caught(wire: tw.Wire) -> None:
    mutant = mutated_module(
        trace_export,
        ('kwargs["allow_redirects"] = False', 'kwargs["allow_redirects"] = True'),
        ("self.max_redirects = 0", "self.max_redirects = 30"),
    )
    wire.status, wire.location = 307, "http://203.0.113.9:3000/elsewhere"
    export = wire.build(module=mutant)
    export.sink.emit(_planted())
    _flush(export)
    assert "http://203.0.113.9:3000/elsewhere" in [r.url for r in wire.sent]


def test_a_mutant_that_trusts_the_environment_is_caught(
    wire: tw.Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.9:3128")
    monkeypatch.setenv("NO_PROXY", "")
    mutant = mutated_module(trace_export, ("self.trust_env = False", "self.trust_env = True"))
    export = wire.build(module=mutant)
    export.sink.emit(_planted())
    _flush(export)
    (options,) = wire.send_options
    assert options["proxies"] != {}


# ---- refusal: nothing is built unless the start-up check passes --------------------------------


def test_with_a_refusal_nothing_is_built(wire: tw.Wire) -> None:
    with pytest.raises(RuntimeError):
        trace_export.build_trace_export(
            tw.settings(),
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://203.0.113.9:4318"},
            model="m",
            host="llm.example",
        )
    assert (wire.providers, wire.sessions, wire.sent) == ([], [], [])


def test_with_tracing_off_nothing_is_built(wire: tw.Wire) -> None:
    off = tw.settings(
        tracing_enabled=False, trace_endpoint=None, trace_public_key=None, trace_secret_key=None
    )
    export = trace_export.build_trace_export(
        off, {"OTEL_SDK_DISABLED": "1"}, model="m", host="llm.example"
    )
    assert export is None
    assert (wire.providers, wire.sessions) == ([], [])


def test_a_mutant_that_skips_the_start_up_check_is_caught(wire: tw.Wire) -> None:
    mutant = mutated_module(trace_export, ("    check_tracing_startup(settings, environ)\n", ""))
    export = mutant.build_trace_export(
        tw.settings(),
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://203.0.113.9:4318"},
        model="m",
        host="llm.example",
    )
    wire.exports.append(export)
    assert wire.providers != []


_IMPORTS_PROBE = """
import sys
from divesafe.api.app import create_app
from divesafe.api.trace_export import build_trace_export
from divesafe.config import Settings
assert build_trace_export(Settings(_env_file=None), {}, model="m", host=None) is None
heavy = ("opentelemetry.sdk", "opentelemetry.exporter", "requests", "urllib3", "google.protobuf")
print(sorted(m for m in sys.modules if m.startswith(heavy)))
"""


def test_with_tracing_off_no_sdk_exporter_or_http_library_is_imported() -> None:
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")}
    out = subprocess.run(
        [sys.executable, "-c", _IMPORTS_PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=120,
        check=True,
    )
    assert out.stdout.strip() == "[]", out.stdout


# ---- the global providers and FastAPI's own telemetry ------------------------------------------


def _proxies() -> tuple[str, str]:
    return (
        type(trace_api.get_tracer_provider()).__name__,
        type(metrics_api.get_meter_provider()).__name__,
    )


def test_building_the_export_never_sets_a_global_provider(wire: tw.Wire) -> None:
    before = _proxies()
    export = wire.build()
    export.sink.emit(_planted())
    _flush(export)
    assert _proxies() == before == ("ProxyTracerProvider", "_ProxyMeterProvider")


def _global_in_memory(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    spans = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", provider)
    return spans


def test_fastapis_native_telemetry_records_nothing_even_with_a_global_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _global_in_memory(monkeypatch)
    with TestClient(app_module.create_app()) as client:
        assert client.get("/health").status_code == 200
        client.post("/v1/assessments", json={"site_id": PROMPT_MARKER})
    assert spans.get_finished_spans() == ()


def test_a_mutant_without_the_fastapi_telemetry_switch_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _global_in_memory(monkeypatch)
    mutant = mutated_module(app_module, ("        telemetry=FASTAPI_TELEMETRY_OFF,\n", ""))
    with TestClient(mutant.create_app()) as client:
        client.get("/health")
    assert spans.get_finished_spans() != ()


def _fresh_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset global providers whose set-once guards are reset, undone after the test, so a
    mutant that sets one is seen and cannot leak into later tests."""
    from opentelemetry._logs import _internal as logs_internal
    from opentelemetry.metrics import _internal as metrics_internal
    from opentelemetry.util._once import Once

    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", None)
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(logs_internal, "_LOGGER_PROVIDER", None)
    monkeypatch.setattr(logs_internal, "_LOGGER_PROVIDER_SET_ONCE", Once())


def _auto_configured(module: Any, monkeypatch: pytest.MonkeyPatch) -> bool:
    """True when starting the app with FastAPI's auto-configuration requested from the
    environment set any global provider."""
    _fresh_globals(monkeypatch)

    def no_socket(*args: Any, **kwargs: Any) -> Any:
        raise requests.ConnectionError("no socket in tests")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", no_socket)
    _no_backoff(monkeypatch)
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://203.0.113.9:4318")
    app = module.create_app()
    try:
        with TestClient(app) as client:
            client.get("/health")
        return _proxies() != ("ProxyTracerProvider", "_ProxyMeterProvider")
    finally:
        from fastapi.telemetry import _runtime

        _runtime._shutdown()


def test_fastapi_auto_configuration_from_otel_variables_stays_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert app_module.FASTAPI_TELEMETRY_OFF == {
        "tracing": False,
        "metrics": False,
        "logs": False,
        "operation_spans": False,
        "auto_configure": False,
    }
    assert not _auto_configured(app_module, monkeypatch)


def test_a_mutant_with_fastapi_auto_configuration_on_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutant = mutated_module(
        app_module,
        ('    "tracing": False,\n', '    "tracing": True,\n'),
        ('    "auto_configure": False,\n', '    "auto_configure": True,\n'),
    )
    assert _auto_configured(mutant, monkeypatch)


# ---- the app: the export is shut down after the runs, within its bound -------------------------


def _traced_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in [n for n in os.environ if n.upper().startswith("OTEL_")]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(app_module, "get_settings", lambda: tw.settings())
    # `configure_logging` replaces the root handlers, caplog's included.
    monkeypatch.setattr(app_module, "configure_logging", lambda level: None)
    return app_module.create_app()


def test_the_lifespan_shuts_the_export_down_after_the_runs_and_before_the_pool(
    wire: tw.Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_runs = app_module.RunManager.shutdown
    real_trace = trace_export.TraceExport.shutdown

    async def runs(self: Any) -> None:
        order.append("runs")
        await real_runs(self)

    def traces(self: Any, wait: float = trace_export.SHUTDOWN_WAIT_SECONDS) -> bool:
        order.append("traces")
        return real_trace(self, wait)

    monkeypatch.setattr(app_module.RunManager, "shutdown", runs)
    monkeypatch.setattr(trace_export.TraceExport, "shutdown", traces)
    monkeypatch.setattr(app_module, "shutdown_http_pool", lambda: order.append("pool"))
    with TestClient(_traced_app(monkeypatch)) as client:
        client.get("/health")
    assert order == ["runs", "traces", "pool"]
    assert len(wire.providers) == 1


def test_a_hung_export_delays_the_app_shutdown_by_the_bound_only(
    wire: tw.Wire, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    wire.mode = "block"
    app = _traced_app(monkeypatch)
    (provider,) = wire.providers
    with TestClient(app) as client:
        client.get("/health")
        provider.get_tracer("t").start_span("run").end()
        started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < trace_export.SHUTDOWN_WAIT_SECONDS + 2.0
    assert wire.sent, "the export never started, so nothing was bounded"
    assert "trace export did not finish within its shutdown bound" in caplog.text


@pytest.mark.parametrize("bounded", [True, False])
def test_the_shutdown_bound_has_teeth(bounded: bool, wire: tw.Wire) -> None:
    wire.mode = "block"
    module = (
        trace_export
        if bounded
        else mutated_module(trace_export, ("thread.join(wait)", "thread.join()"))
    )
    export = wire.build(module=module)
    export.sink.emit(_planted())
    threading.Timer(2.0, wire.release.set).start()
    started = time.monotonic()
    finished = export.shutdown(0.3)
    elapsed = time.monotonic() - started
    if bounded:
        assert not finished and elapsed < 1.0
    else:
        assert finished and elapsed >= 1.5


# ---- AST pins -----------------------------------------------------------------------------------


def _python(*parts: str) -> list[Path]:
    return sorted((SRC.joinpath(*parts)).rglob("*.py"))


def _imports(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node, node.module or ""
            # `from divesafe import observability` names the module only in its aliases.
            for alias in node.names:
                yield node, ".".join(filter(None, [node.module or "", alias.name]))


def _module_level_imports(tree: ast.Module) -> Iterator[tuple[ast.AST, str]]:
    """Imports that run when the module is imported: everything outside function bodies."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield from _imports(node)
        else:
            stack.extend(ast.iter_child_nodes(node))


def _absolute(path: Path, node: ast.AST, name: str) -> str:
    """The absolute module name, resolving a relative `from . import x` against `path`."""
    level = node.level if isinstance(node, ast.ImportFrom) else 0
    if level == 0:
        return name
    package = list(path.relative_to(SRC.parent).with_suffix("").parts)[:-level]
    return ".".join([*package, *([name] if name else [])])


def test_a_relative_import_is_resolved() -> None:
    path = SRC / "domain" / "x.py"
    node = ast.parse("from ..observability import collector").body[0]
    assert _absolute(path, node, "observability") == "divesafe.observability"
    bare = ast.parse("from .. import observability").body[0]
    names = {_absolute(path, n, name) for n, name in _imports(bare)}
    assert "divesafe.observability" in names


@pytest.mark.parametrize("package", ["domain", "risk"])
def test_domain_and_risk_never_import_tracing(package: str) -> None:
    for path in _python(package):
        for node, relative in _imports(ast.parse(path.read_text())):
            name = _absolute(path, node, relative)
            assert not name.startswith(("opentelemetry", "divesafe.observability")), (path, name)
            assert not name.startswith(("requests", "urllib3", "langfuse")), (path, name)


def test_src_and_the_dependencies_never_name_langfuse() -> None:
    for path in _python():
        for _, name in _imports(ast.parse(path.read_text())):
            assert not name.startswith("langfuse"), (path, name)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = [*project["dependencies"]]
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    names = {re.split(r"[\s<>=!~\[;]", d.strip(), maxsplit=1)[0].lower() for d in declared}
    assert names, "no dependencies were read"
    for name in names:
        assert "langfuse" not in name, name
        assert not name.startswith(("opentelemetry-instrumentation", "opentelemetry-distro")), name


_GLOBAL_SETTERS = {
    "set_tracer_provider",
    "get_tracer_provider",
    "set_meter_provider",
    "get_meter_provider",
    "set_logger_provider",
    "get_logger_provider",
    "get_meter",
    "set_global_textmap",
}


def _global_tracer(node: ast.AST) -> bool:
    """`get_tracer` reaches the global provider unless it is called on the private one."""
    if isinstance(node, ast.Name):
        return node.id == "get_tracer"
    if isinstance(node, ast.Attribute) and node.attr == "get_tracer":
        return not (isinstance(node.value, ast.Name) and node.value.id == "provider")
    return False


def test_src_never_touches_the_global_providers() -> None:
    private_tracers = 0
    for path in _python():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            name = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.id
                if isinstance(node, ast.Name)
                else None
            )
            assert name not in _GLOBAL_SETTERS, (path, name)
            assert not _global_tracer(node), (path, ast.unparse(node))
            if isinstance(node, ast.Attribute) and node.attr == "get_tracer":
                private_tracers += 1
        for _, imported in _imports(tree):
            assert not imported.startswith("opentelemetry.instrumentation"), (path, imported)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not {a.name for a in node.names} & (_GLOBAL_SETTERS | {"get_tracer"}), path
    assert private_tracers == 1, "the private provider's tracer was not found"


def test_opentelemetry_and_requests_are_imported_only_inside_the_export_functions() -> None:
    allowed = SRC / "api" / "trace_export.py"
    for path in _python():
        tree = ast.parse(path.read_text())
        for _, name in _module_level_imports(tree):
            assert not name.startswith(("opentelemetry", "requests", "urllib3")), (path, name)
        if path != allowed:
            for _, name in _imports(tree):
                assert not name.startswith("opentelemetry"), (path, name)
                assert not name.startswith(("requests", "urllib3")), (path, name)


def test_the_exporter_never_exports_inline() -> None:
    """Export runs on the batch processor's thread, never in `emit` (a blocked wire must not
    hold a run's `finally`)."""
    tree = ast.parse((SRC / "api" / "trace_export.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "SimpleSpanProcessor" not in names
    assert "BatchSpanProcessor" in names
