"""Export of finished run traces to the local, self-hosted Langfuse (ADR 0027 amendment, step 3).

`build_trace_export` runs the start-up check first and builds nothing unless it passes and
tracing is enabled. Only then is OpenTelemetry (or `requests`) imported; no module-level import
of either exists in `src`. Everything is passed explicitly, so no `OTEL_*` variable is read for
anything that matters (they are refused at start-up anyway):

- a private `TracerProvider` (never the process-wide one), `shutdown_on_exit=False`, an explicit
  resource, sampler, span limits and a no-op meter provider;
- one `BatchSpanProcessor` with fixed bounds, so export runs on its own thread;
- the OTLP/HTTP exporter with the endpoint, headers, timeout, compression, size cap and a
  `requests.Session` that ignores the environment, never follows a redirect, has no https
  adapter and refuses any URL but the endpoint.

Span attributes are built from fixed field lists (`RUN_ATTRIBUTES`, `STAGE_ATTRIBUTES`,
`MODEL_CALL_ATTRIBUTES`), never by serialising a collected mapping. Failures are counted by a
fixed reason (`count_drop`) and never raised into a run; the library loggers are silenced.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from divesafe.agents import PROMPT_VERSION
from divesafe.agents.cross_examination import PEER_TASK_PREFIX
from divesafe.agents.runner import CALL_OUTCOMES
from divesafe.agents.specialists import SPECIALISTS
from divesafe.api.tracing import check_tracing_startup
from divesafe.config import Settings
from divesafe.config.logging import valid_request_id
from divesafe.domain import ScenarioKind, StageName, StageOutcome
from divesafe.models.llm import PROVIDER_FAILURE_REASONS, plain_count
from divesafe.observability import (
    DropReason,
    ModelCallEntry,
    RunTrace,
    StageEntry,
    TraceSink,
    count_drop,
)
from divesafe.services import FailureCode

logger = logging.getLogger(__name__)

SERVICE_NAME = "divesafe-api"
INSTRUMENTATION_NAME = "divesafe.api"
# Fixed at build (ADR 0027 "Off the event loop"); tests read these constants. The exporter's
# own retry count is the library's fixed `_MAX_RETRIES`; every retry ends within the export timeout.
EXPORT_TIMEOUT_SECONDS = 3.0
MAX_QUEUE_SIZE = 512
MAX_EXPORT_BATCH_SIZE = 128
SCHEDULE_DELAY_MILLIS = 2000
MAX_REQUEST_BYTES = 4 * 1024 * 1024
SHUTDOWN_WAIT_SECONDS = 4.0
MAX_ATTRIBUTE_CHARS = 128
# Langfuse documents this header for spans sent straight over OTLP; without it new data can take
# minutes to appear. It is fixed text and carries nothing from a run.
INGESTION_HEADER = ("x-langfuse-ingestion-version", "4")
# The library loggers can print the endpoint, the server's reply text and connection errors with
# host and port; each gets a NullHandler and stops propagating (ADR 0027, "Counted, not logged").
SILENCED_LOGGERS = ("opentelemetry", "urllib3", "requests")

# The whole export allow-list (ADR 0027 "The allow-list covers the whole export"). A change here
# is a `tests/safety` change reviewed by risk-reviewer, with the pin in the same change.
RUN_ATTRIBUTES = ("run_outcome", "prompt_version", "assessment_id", "request_id")
STAGE_ATTRIBUTES = ("stage", "outcome", "duration_ms")
# The log line's `CALL_LOG_FIELDS`, with `output_used` renamed on the span only so Langfuse reads
# the count as usage (owner, 2026-11-16); `input_used` (step 4) likewise; and the model name.
# `provider_reason` only from `PROVIDER_FAILURE_REASONS` (ADR 0018 amendment 2026-11-18).
MODEL_CALL_ATTRIBUTES = (
    "task",
    "outcome",
    "provider_reason",
    "output_limit",
    "output_chars",
    "duration_ms",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.input_tokens",
    "gen_ai.request.model",
)
_MODEL_CALL_COUNTS = (
    ("output_limit", "output_limit"),
    ("output_chars", "output_chars"),
    ("duration_ms", "duration_ms"),
    ("output_used", "gen_ai.usage.output_tokens"),
    ("input_used", "gen_ai.usage.input_tokens"),
)
RUN_OUTCOMES: frozenset[str] = frozenset({"stored", *(code.value for code in FailureCode)})
TASK_NAMES: frozenset[str] = frozenset(
    {
        "risk_assessment",
        *(f"specialist:{spec.name}" for spec in SPECIALISTS),
        *(f"{PEER_TASK_PREFIX}{spec.name}" for spec in SPECIALISTS),
        *(f"scenario:{kind.value}" for kind in ScenarioKind),
    }
)
STAGE_NAMES: frozenset[str] = frozenset(name.value for name in StageName)
STAGE_OUTCOMES: frozenset[str] = frozenset(outcome.value for outcome in StageOutcome)
UNLISTED_TASK = "unlisted_task"
# Both forms the API creates: `uuid4().hex` and `_idempotent_id` (32 lowercase hex characters).
_ASSESSMENT_ID = re.compile(r"[0-9a-f]{32}")
_MODEL_NAME = re.compile(r"[A-Za-z0-9._:/@-]{1,128}")


# ---- attributes: plain functions over plain values, no OpenTelemetry ----------------------------


def run_attributes(trace: RunTrace) -> dict[str, str]:
    attributes = {"prompt_version": PROMPT_VERSION}
    if type(trace.run_outcome) is str and trace.run_outcome in RUN_OUTCOMES:
        attributes["run_outcome"] = trace.run_outcome
    if type(trace.assessment_id) is str and _ASSESSMENT_ID.fullmatch(trace.assessment_id):
        attributes["assessment_id"] = trace.assessment_id
    if valid_request_id(trace.request_id):
        attributes["request_id"] = str(trace.request_id)
    return attributes


def stage_name(entry: StageEntry) -> str | None:
    value = getattr(entry.name, "value", None)
    return value if type(value) is str and value in STAGE_NAMES else None


def stage_attributes(entry: StageEntry) -> dict[str, str | int]:
    attributes: dict[str, str | int] = {}
    name = stage_name(entry)
    if name is not None:
        attributes["stage"] = name
    outcome = getattr(entry.outcome, "value", None)
    if type(outcome) is str and outcome in STAGE_OUTCOMES:
        attributes["outcome"] = outcome
    duration = plain_count(entry.duration_ms)
    if duration is not None:
        attributes["duration_ms"] = duration
    return attributes


def task_name(fields: Mapping[str, object]) -> str:
    task = fields.get("task")
    return task if type(task) is str and task in TASK_NAMES else UNLISTED_TASK


def model_call_attributes(entry: ModelCallEntry, model: str | None) -> dict[str, str | int]:
    fields = entry.fields
    attributes: dict[str, str | int] = {}
    task = task_name(fields)
    if task != UNLISTED_TASK:
        attributes["task"] = task
    outcome = fields.get("outcome")
    if type(outcome) is str and outcome in CALL_OUTCOMES:
        attributes["outcome"] = outcome
    reason = fields.get("provider_reason")
    if outcome == "provider_error" and type(reason) is str and reason in PROVIDER_FAILURE_REASONS:
        attributes["provider_reason"] = next(r for r in PROVIDER_FAILURE_REASONS if r == reason)
    for source, target in _MODEL_CALL_COUNTS:
        count = plain_count(fields.get(source))
        if count is not None:
            attributes[target] = count
    if model is not None:
        attributes["gen_ai.request.model"] = model
    return attributes


def exportable_model_name(model: object, host: str | None) -> str | None:
    """The model name for the `gen_ai.request.model` attribute, or None if it is not a plain,
    short name (Langfuse needs it for prices; it is never a URL, the model host or free text).
    With no model host there is nothing to check the name against, so it is never exported."""
    if type(model) is not str or _MODEL_NAME.fullmatch(model) is None or "//" in model:
        return None
    if not host:
        return None
    if host.lower() in model.lower():
        return None
    return model


def _span_times(start_ns: object, duration_ms: object) -> tuple[int, int] | None:
    if type(start_ns) is not int or start_ns <= 0:
        return None
    duration = plain_count(duration_ms) or 0
    return start_ns, start_ns + duration * 1_000_000


def silence_library_loggers() -> None:
    for name in SILENCED_LOGGERS:
        library = logging.getLogger(name)
        library.propagate = False
        if not any(isinstance(h, logging.NullHandler) for h in library.handlers):
            library.addHandler(logging.NullHandler())


def basic_authorization(settings: Settings) -> str:
    """`Basic <base64(public:secret)>`. The start-up check has pinned both keys to
    `[A-Za-z0-9._-]{1,256}`, so the header holds no CR, LF or extra `:`."""
    public, secret = settings.trace_public_key, settings.trace_secret_key
    if public is None or secret is None:  # unreachable after the start-up check
        raise RuntimeError("refusing to start: tracing needs both Langfuse project keys")
    pair = f"{public.get_secret_value()}:{secret.get_secret_value()}".encode("ascii")
    return "Basic " + base64.b64encode(pair).decode("ascii")


class _Pending:
    """Spans handed to the processor and not yet exported. Capped at the processor's queue size,
    so the SDK's own queue never overflows (it would drop silently): a run whose spans do not
    fit is dropped whole and counted `queue_full`. The lock guards only the arithmetic."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0
        self._lock = threading.Lock()

    def reserve(self, n: int) -> bool:
        with self._lock:
            if self._count + n > self._limit:
                return False
            self._count += n
            return True

    def release(self, n: int) -> None:
        with self._lock:
            self._count = max(0, self._count - n)

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


@dataclass(frozen=True)
class TraceExport:
    """The sink for `RunManager` and the bounded shutdown for the app's lifespan."""

    sink: TraceSink
    _close: Callable[[], None]

    def shutdown(self, wait: float = SHUTDOWN_WAIT_SECONDS) -> bool:
        """Shut the provider down in a daemon thread joined for at most `wait` seconds, so a hung
        export delays process exit by at most that bound. True if it finished in time."""
        thread = threading.Thread(target=self._close, name="divesafe-trace-shutdown", daemon=True)
        thread.start()
        thread.join(wait)
        return not thread.is_alive()


def build_trace_export(
    settings: Settings, environ: Mapping[str, str], *, model: object, host: str | None
) -> TraceExport | None:
    """None with tracing off. Otherwise the start-up check runs first (and raises on any
    refusal), and only after it has passed is anything imported or built."""
    check_tracing_startup(settings, environ)
    if not settings.tracing_enabled:
        return None
    endpoint = settings.trace_endpoint
    if endpoint is None:  # unreachable after the start-up check
        raise RuntimeError("refusing to start: tracing needs DIVESAFE_TRACE_ENDPOINT")
    silence_library_loggers()
    return _build(endpoint, basic_authorization(settings), exportable_model_name(model, host))


def _build(endpoint: str, authorization: str, model: str | None) -> TraceExport:
    import requests
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.metrics import NoOpMeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON
    from opentelemetry.sdk.version import __version__ as sdk_version

    class _TraceSession(requests.Session):
        """No environment (proxies, `.netrc`, CA bundles), no redirect, no https adapter, and
        only the one endpoint. Records why the last request failed, as a fixed reason."""

        def __init__(self) -> None:
            super().__init__()
            self.trust_env = False
            self.max_redirects = 0
            self.proxies = {}
            self.adapters.clear()
            self.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
            self.failure: DropReason | None = None

        def request(  # type: ignore[override]
            self, method: str, url: str, *args: Any, **kwargs: Any
        ) -> requests.Response:
            self.failure = None
            if method != "POST" or url != endpoint:
                self.failure = "connection"
                raise requests.ConnectionError("refused: not the trace endpoint")
            kwargs["allow_redirects"] = False
            try:
                response = super().request(method, url, *args, **kwargs)
            except requests.Timeout:
                self.failure = "timeout"
                raise
            except requests.TooManyRedirects:
                # A 3xx reply with `max_redirects = 0`: refused before any second request.
                self.failure = "http_status"
                raise
            except requests.RequestException:
                self.failure = "connection"
                raise
            if not 200 <= response.status_code < 300:
                self.failure = "http_status"
            return response

    session = _TraceSession()
    meters = NoOpMeterProvider()
    inner = OTLPSpanExporter(
        endpoint=endpoint,
        certificate_file=None,
        client_key_file=None,
        client_certificate_file=None,
        headers={"Authorization": authorization, INGESTION_HEADER[0]: INGESTION_HEADER[1]},
        timeout=EXPORT_TIMEOUT_SECONDS,
        compression=Compression.NoCompression,
        session=session,
        max_request_size=MAX_REQUEST_BYTES,
        meter_provider=meters,
    )
    pending = _Pending(MAX_QUEUE_SIZE)

    class _CountingExporter(SpanExporter):
        """Counts each failed export once, by the session's last reason. A failure the session
        did not see (encoding, the size cap, already shut down) never reached the server and
        counts as `connection`. A 3xx reply, which the library reads as success, counts as
        `http_status`."""

        def export(self, spans: Any) -> SpanExportResult:
            batch: tuple[ReadableSpan, ...] = tuple(spans)
            session.failure = None
            try:
                result = inner.export(batch)
            except Exception:
                result = SpanExportResult.FAILURE
            finally:
                pending.release(len(batch))
            if result is not SpanExportResult.SUCCESS or session.failure is not None:
                count_drop(session.failure or "connection")
            return result

        def shutdown(self) -> None:
            inner.shutdown()  # type: ignore[no-untyped-call]

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    processor = BatchSpanProcessor(
        _CountingExporter(),
        max_queue_size=MAX_QUEUE_SIZE,
        schedule_delay_millis=SCHEDULE_DELAY_MILLIS,
        max_export_batch_size=MAX_EXPORT_BATCH_SIZE,
        export_timeout_millis=EXPORT_TIMEOUT_SECONDS * 1000,
        meter_provider=meters,
    )
    resource = Resource(
        {
            "service.name": SERVICE_NAME,
            "telemetry.sdk.language": "python",
            "telemetry.sdk.name": "opentelemetry",
            "telemetry.sdk.version": sdk_version,
        }
    )
    limits = SpanLimits(
        max_attributes=len(MODEL_CALL_ATTRIBUTES),
        max_events=0,
        max_links=0,
        max_span_attributes=len(MODEL_CALL_ATTRIBUTES),
        max_event_attributes=0,
        max_link_attributes=0,
        max_attribute_length=MAX_ATTRIBUTE_CHARS,
        max_span_attribute_length=MAX_ATTRIBUTE_CHARS,
    )
    provider = TracerProvider(
        sampler=ALWAYS_ON,
        resource=resource,
        shutdown_on_exit=False,
        span_limits=limits,
        meter_provider=meters,
    )
    provider.add_span_processor(processor)
    tracer = provider.get_tracer(INSTRUMENTATION_NAME)
    closed = threading.Event()

    class _Sink:
        def emit(self, trace: RunTrace) -> None:
            if closed.is_set():
                return
            needed = 1 + len(trace.stages) + len(trace.model_calls)
            if not pending.reserve(needed):
                count_drop("queue_full")
                return
            ended = [0]
            try:
                _emit_spans(tracer, trace, model, ended)
            finally:
                pending.release(needed - ended[0])

    def close() -> None:
        closed.set()
        try:
            provider.shutdown()
        except Exception:
            logger.warning("trace export did not shut down cleanly")

    return TraceExport(sink=_Sink(), _close=close)


def _emit_spans(tracer: Any, trace: RunTrace, model: str | None, ended: list[int]) -> None:
    """The run span and its children, each with explicit start and end times, from fixed field
    lists only. No span is made current; no exception is recorded; no status is set."""
    from opentelemetry import trace as trace_api
    from opentelemetry.context import Context

    times = _span_times(trace.started_ns, 0)
    if times is None or type(trace.finished_ns) is not int:
        return
    run_start, run_end = times[0], max(times[0], trace.finished_ns)
    run = tracer.start_span(
        "run",
        context=Context(),
        attributes=run_attributes(trace),
        start_time=run_start,
        record_exception=False,
        set_status_on_exception=False,
    )
    parent = trace_api.set_span_in_context(run, Context())
    try:
        for stage in trace.stages:
            name = stage_name(stage)
            duration = plain_count(stage.duration_ms) or 0
            stage_times = _span_times(
                stage.ended_ns - duration * 1_000_000 if type(stage.ended_ns) is int else None,
                duration,
            )
            if name is None or stage_times is None:
                continue
            span = tracer.start_span(
                name,
                context=parent,
                attributes=stage_attributes(stage),
                start_time=stage_times[0],
                record_exception=False,
                set_status_on_exception=False,
            )
            span.end(end_time=stage_times[1])
            ended[0] += 1
        for call in trace.model_calls:
            call_times = _span_times(call.started_ns, call.fields.get("duration_ms"))
            if call_times is None:
                continue
            span = tracer.start_span(
                task_name(call.fields),
                context=parent,
                attributes=model_call_attributes(call, model),
                start_time=call_times[0],
                record_exception=False,
                set_status_on_exception=False,
            )
            span.end(end_time=call_times[1])
            ended[0] += 1
    finally:
        run.end(end_time=run_end)
        ended[0] += 1
