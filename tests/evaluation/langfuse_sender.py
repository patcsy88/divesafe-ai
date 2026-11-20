"""ADR 0027 §2's dataset sender to the local Langfuse's `divesafe-eval` project (ADR 0030 decision
3, build step 1), with the `code.*` scores (build step 2). Development only: never imported by
`src`, never imports `api.state` or `capture_item6`.

`langfuse` is imported only inside `run_send`, after every start-up refusal in `langfuse_checks`
has passed. Delivery is confirmed by the sender itself: spans by its own recording exporter, every
`client.api` call by the replies a response hook on its own `httpx_client` recorded. A failed send
fails the whole run with the fixed label `send_failed`, raised outside any `except` block; the send
error itself is dropped, never kept, logged or written.

Nothing here logs or prints. No prompt, reply, key, header or exception text is kept beyond what
§2 sends (the captured prompts and the record's outputs, without `confidence`).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import httpx
import requests
import requests.adapters
from tests.evaluation import harness
from tests.evaluation.code_scores import CodeScore, code_scores_for_record, code_scores_for_refusal
from tests.evaluation.egress_gate import eval_output_dir, output_path, write_private
from tests.evaluation.langfuse_checks import (
    INGESTION_HEADER,
    LANGFUSE_BASE_URL,
    LANGFUSE_PROJECT,
    OTLP_TRACES_PATH,
    EvalKeys,
    LoggingBaseline,
    SenderRefused,
    build_run_name,
    capture_warnings,
    check_allowed_cases,
    check_case_limit,
    check_clean_worktree,
    check_handlers,
    check_import_origin,
    check_installed_version,
    check_model_host,
    check_no_unlisted_loggers,
    check_post_build_environment,
    check_record_content,
    git_commit,
    is_server_version_shape,
    logging_baseline,
    read_eval_keys,
    refuse_divesafe_variables,
    refuse_if_imported,
    refuse_sdk_environment,
    remove_media_variable,
    remove_sdk_console_handler,
    set_media_variable,
    silence_eval_loggers,
    utc_now,
)
from tests.evaluation.output_dir import CaptureRefused

from divesafe.agents import PROMPT_VERSION
from divesafe.domain import AssessmentRecord
from divesafe.models.llm import LLMProvider, LLMRequest, LLMResponse
from divesafe.orchestration.supervisor import SupervisorInvariantError

# --- constants (changed only through a reviewed change that repeats the live check) -------------

_ORIGIN: Final = urlsplit(LANGFUSE_BASE_URL)
# Expected replies of the direct `client.api` calls: method, path (leading `/`, as the response
# hook records it), status and return type name in `langfuse.api`. Method, path and type are from
# the pinned SDK's client code; the statuses are the sender's stated expectation (ADR 0030
# decision 3, "Other calls"), to be confirmed against the real server at the live check.
PROJECTS_CALL: Final = ("GET", "/api/public/projects", 200, "Projects")
DATASET_CALL: Final = ("POST", "/api/public/v2/datasets", 200, "Dataset")
DATASET_ITEM_CALL: Final = ("POST", "/api/public/dataset-items", 200, "DatasetItem")
SCORES_CALL: Final = ("POST", "/api/public/scores", 202, "CreateScoreBatchResponse")
# The live check only: the pinned SDK's `client.api.health.health()`, to record the server version.
HEALTH_CALL: Final = ("GET", "/api/public/health", 200, "HealthResponse")
# The shortest run of the server version's characters that, also found in a key (any case),
# refuses the run with `server_version`.
_KEY_FRAGMENT_CHARS: Final = 6
# A span batch counts only if the last status recorded during its export call is exactly this.
SPAN_STATUS: Final = 200
NO_RETRIES: Final = {"max_retries": 0}

# Builder choices (not fixed by the ADRs): names in `divesafe-eval`.
DATASET_NAME: Final = "divesafe-scenarios"
CASE_SPAN_NAME: Final = "eval-case"
COMPLETION_SCORE_NAME: Final = "run.completion_marker"
LOCAL_FILE_NAME: Final = "langfuse_send.json"
# The SDK's experiment environment (`LANGFUSE_SDK_EXPERIMENT_ENVIRONMENT`), set on the client, the
# spans and the scores. Given explicitly, so no `LANGFUSE_TRACING_ENVIRONMENT` fallback is read.
EXPERIMENT_ENVIRONMENT: Final = "sdk-experiment"

# Client and exporter settings, all given explicitly so no environment fallback is read.
CLIENT_TIMEOUT_SECONDS: Final = 10
EXPORT_TIMEOUT_SECONDS: Final = 10.0
FLUSH_AT: Final = 64
FLUSH_INTERVAL_SECONDS: Final = 60.0
MAX_REQUEST_BYTES: Final = 32 * 1024 * 1024
MAX_SPAN_ATTRIBUTES: Final = 64
MAX_ATTRIBUTE_CHARS: Final = 8 * 1024 * 1024

# Span attribute keys (the pinned SDK's `LangfuseOtelSpanAttributes`).
ATTR_ENVIRONMENT: Final = "langfuse.environment"
ATTR_EXPERIMENT_ID: Final = "langfuse.experiment.id"
ATTR_EXPERIMENT_NAME: Final = "langfuse.experiment.name"
ATTR_DATASET_ID: Final = "langfuse.experiment.dataset.id"
ATTR_ITEM_ID: Final = "langfuse.experiment.item.id"
ATTR_ROOT_OBSERVATION_ID: Final = "langfuse.experiment.item.root_observation_id"


class OriginRefused(RuntimeError):
    """A request to anything but `LANGFUSE_BASE_URL`'s scheme, host and port, refused before any
    connection. Not a connection error, so the OTLP client does not retry it."""

    def __init__(self) -> None:
        super().__init__("refused: not the Langfuse origin")


def _same_origin(scheme: str, host: str | None, port: int | None) -> bool:
    return (scheme, host, port) == (_ORIGIN.scheme, _ORIGIN.hostname, _ORIGIN.port)


# --- the judge interface (the judge itself is ADR 0030 build step 3, not built here) ------------


@dataclass(frozen=True)
class JudgeScore:
    """One validated judge score or flag, as the judge module returns it. `comment` is the
    screened rationale, or None."""

    name: str
    value: float
    data_type: Literal["NUMERIC", "BOOLEAN"]
    comment: str | None = None


Judge = Callable[[harness.Scenario, AssessmentRecord], Sequence[JudgeScore]]


# --- reply recording ----------------------------------------------------------------------------


@dataclass
class ReplyRecorder:
    """The `httpx_client`'s response hook: keeps method, path and status only."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _replies: list[tuple[str, str, int]] = field(default_factory=list)

    def record(self, response: httpx.Response) -> None:
        with self._lock:
            self._replies.append(
                (response.request.method, response.request.url.path, response.status_code)
            )

    def mark(self) -> int:
        with self._lock:
            return len(self._replies)

    def since(self, mark: int) -> list[tuple[str, str, int]]:
        with self._lock:
            return list(self._replies[mark:])


def _guard_request(request: httpx.Request) -> None:
    if not _same_origin(request.url.scheme, request.url.host, request.url.port):
        raise OriginRefused()


def build_httpx_client(
    recorder: ReplyRecorder, transport: httpx.BaseTransport | None
) -> httpx.Client:
    """No environment, no redirect, the one origin, and a hook recording each reply."""
    return httpx.Client(
        trust_env=False,
        follow_redirects=False,
        timeout=CLIENT_TIMEOUT_SECONDS,
        transport=transport,
        event_hooks={"request": [_guard_request], "response": [recorder.record]},
    )


class OtlpSession(requests.Session):
    """The OTLP exporter's session: no environment (proxies, `.netrc`, CA bundles), no redirect,
    one http adapter without retries, only the Langfuse origin, and a response hook that keeps
    each reply's HTTP status only."""

    def __init__(self, adapter: requests.adapters.BaseAdapter | None = None) -> None:
        super().__init__()
        self.trust_env = False
        self.max_redirects = 0
        self.proxies = {}
        self.adapters.clear()
        self.mount("http://", adapter or requests.adapters.HTTPAdapter(max_retries=0))
        self.statuses: list[int] = []
        self.hooks["response"].append(self._record)

    def _record(self, response: requests.Response, *args: object, **kwargs: object) -> None:
        self.statuses.append(response.status_code)

    def request(  # type: ignore[override]
        self, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        parts = urlsplit(url)
        if not _same_origin(parts.scheme, parts.hostname, parts.port):
            raise OriginRefused()
        kwargs["allow_redirects"] = False
        return super().request(method, url, *args, **kwargs)


def span_id_of(span: Any) -> str | None:
    context = span.get_span_context() if hasattr(span, "get_span_context") else None
    span_id = getattr(context, "span_id", None)
    return format(span_id, "016x") if type(span_id) is int else None


class RecordingExporter:
    """Wraps the OTLP exporter (ADR 0030 decision 3, "Spans"). Under one lock per export call: a
    batch holding a span id the sender did not start exports nothing and records a failure for
    every id; otherwise each id gets `True` only if the wrapped export returned `SUCCESS` and the
    last status recorded during the call is exactly `SPAN_STATUS`. A raised export is a failure and
    the exception is dropped."""

    def __init__(self, inner: Any, session: OtlpSession, success: Any, failure: Any) -> None:
        self._inner = inner
        self._session = session
        self._success = success
        self._failure = failure
        self._lock = threading.Lock()
        self._started: set[str] = set()
        self._results: dict[str, list[bool]] = {}

    def started(self, span_id: str) -> None:
        with self._lock:
            self._started.add(span_id)

    def export(self, spans: Sequence[Any]) -> Any:
        batch = tuple(spans)
        ids = [span_id_of(span) for span in batch]
        with self._lock:
            if any(i is None or i not in self._started for i in ids):
                for i in ids:
                    if i is not None:
                        self._results.setdefault(i, []).append(False)
                return self._failure
            self._session.statuses.clear()
            ok = False
            try:
                result = self._inner.export(batch)
                statuses = self._session.statuses
                ok = result == self._success and bool(statuses) and statuses[-1] == SPAN_STATUS
            except Exception:  # the export error is dropped, never kept or logged
                ok = False
            for i in ids:
                if i is not None:
                    self._results.setdefault(i, []).append(ok)
            return self._success if ok else self._failure

    def confirmed(self, span_ids: Sequence[str]) -> bool:
        """Each id has exactly one result and it is a success."""
        with self._lock:
            return all(self._results.get(i) == [True] for i in span_ids)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# --- the capturing provider ---------------------------------------------------------------------


class CapturingProvider:
    """Wraps the run's `LLMProvider` and keeps `LLMRequest.messages` only (role and content).
    The reply is passed on untouched and never kept (ADR 0027 §2, "Never kept, so never sent").
    Other attributes the pipeline reads (timeouts, limits, `host`) are passed through."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.name: str = inner.name
        self.model: str = inner.model
        self.external: bool = inner.external
        self.prompts: list[list[dict[str, str]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append([{"role": m.role, "content": m.content} for m in request.messages])
        return await self._inner.complete(request)


def _without_confidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_confidence(v) for k, v in value.items() if k != "confidence"}
    if isinstance(value, list):
        return [_without_confidence(v) for v in value]
    return value


def record_outputs(record: AssessmentRecord) -> dict[str, Any]:
    """The outputs §2 sends, read from the stored record (findings, scenarios, the proposal and
    the explanation), with every `confidence` field removed."""
    data = json.loads(record.model_dump_json())
    outputs: dict[str, Any] = _without_confidence(
        {
            "findings": data["findings"],
            "scenarios": data["scenarios"],
            "proposed_recommendation": data["proposed_recommendation"],
            "proposal_evidence_ids": data["proposal_evidence_ids"],
            "explanation": data["explanation"],
        }
    )
    return outputs


# --- confirmed API calls ------------------------------------------------------------------------


def experiment_id(project_id: str, dataset_id: str, run_name: str) -> str:
    """The server's stable experiment id for this dataset run (the pinned SDK's derivation)."""
    payload = json.dumps(
        ["langfuse-experiment-v1", project_id, dataset_id, run_name],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class _Api:
    """Direct `client.api` calls, each confirmed by exactly one recorded reply with the expected
    method, path and status and the expected return type (by exact type)."""

    def __init__(self, client: Any, models: Any, recorder: ReplyRecorder) -> None:
        self._client = client
        self._models = models
        self._recorder = recorder

    def call(self, expected: tuple[str, str, int, str], send: Callable[[Any], Any]) -> Any:
        method, path, status, type_name = expected
        mark = self._recorder.mark()
        failed = False
        result: Any = None
        try:
            result = send(self._client.api)
        except Exception:  # the send error is dropped; the label is raised below
            failed = True
        if failed:
            raise SenderRefused("send_failed")
        expected_type = getattr(self._models, type_name, None)
        replies = self._recorder.since(mark)
        if (
            replies != [(method, path, status)]
            or expected_type is None
            or type(result) is not expected_type
        ):
            del result  # the unchecked reply is not kept in the raising frame
            raise SenderRefused("send_failed")
        return result

    def scores(self, requests_: list[Any]) -> None:
        self.call(
            SCORES_CALL,
            lambda api: api.scores.create(request=requests_, request_options=dict(NO_RETRIES)),
        )

    def score_request(self, **fields: Any) -> Any:
        return self._models.CreateScoreRequest(environment=EXPERIMENT_ENVIRONMENT, **fields)

    def data_type(self, name: str) -> Any:
        return getattr(self._models.ScoreDataType, name)


# --- the run ------------------------------------------------------------------------------------


@dataclass
class _Run:
    client: Any
    api: _Api
    exporter: RecordingExporter
    baseline: LoggingBaseline
    run_name: str
    commit: str
    project_id: str
    dataset_id: str
    experiment: str
    server_version: str | None


@dataclass(frozen=True)
class SendResult:
    """What the run sent and confirmed. `complete` only when the completion marker was confirmed
    in Langfuse and written as the local file's last entry. `langfuse_server_version` is the
    Langfuse server's version from the confirmed health call, read on the live check only
    (`case_limit == 1`) and None on a full run; it is returned here only, never sent or written to
    the local file."""

    run_name: str
    expected_cases: int
    confirmed_cases: int
    complete: bool
    output_file: Path
    langfuse_server_version: str | None = None


def run_send(
    cases: object,
    provider_factory: Callable[[], LLMProvider | None],
    judge: Judge | None,
    *,
    egress_basis: str,
    model_host: str | None,
    output_dir: Path,
    case_limit: object = None,
    client_factory: Callable[..., Any] | None = None,
    httpx_transport: httpx.BaseTransport | None = None,
    otlp_adapter: requests.adapters.BaseAdapter | None = None,
) -> SendResult:
    """Send one harness run of an allow-listed set to `divesafe-eval` as a dataset run.

    `egress_basis` is `egress_gate`'s value for the agents' provider and `model_host` the evaluation
    base URL's host (ADR 0030 decision 3). `judge` may be None only with `case_limit == 1` (the
    live check makes no judge call because it is limited). `client_factory` (default
    `langfuse.Langfuse`), `httpx_transport` and `otlp_adapter` are test seams; there is no run-name
    parameter."""
    # Start-up refusals, before any import of the SDK or network call.
    limit = check_case_limit(case_limit)
    scenarios = check_allowed_cases(cases)
    if judge is None and limit != 1:
        raise SenderRefused("judge_missing")
    host = check_model_host(model_host)
    refuse_sdk_environment()
    refuse_divesafe_variables()
    refuse_if_imported()
    check_installed_version()
    check_import_origin()
    baseline = logging_baseline()
    silence_eval_loggers()
    capture_warnings()
    check_handlers(baseline)
    keys = read_eval_keys()
    check_clean_worktree()
    commit = git_commit()
    run_name = build_run_name(commit, utc_now(), uuid.uuid4())
    directory: Path | None = None
    try:
        directory = eval_output_dir(None, output_dir)
    except CaptureRefused:
        directory = None
    if directory is None:
        raise SenderRefused("output_dir")

    set_media_variable()
    try:
        return _send_run(
            scenarios,
            limit,
            provider_factory,
            judge,
            keys,
            baseline,
            egress_basis=egress_basis,
            model_host=host,
            directory=directory,
            run_name=run_name,
            commit=commit,
            client_factory=client_factory,
            httpx_transport=httpx_transport,
            otlp_adapter=otlp_adapter,
        )
    finally:
        remove_media_variable()


def _send_run(
    scenarios: Sequence[harness.Scenario],
    limit: int | None,
    provider_factory: Callable[[], LLMProvider | None],
    judge: Judge | None,
    keys: EvalKeys,
    baseline: LoggingBaseline,
    *,
    egress_basis: str,
    model_host: str,
    directory: Path,
    run_name: str,
    commit: str,
    client_factory: Callable[..., Any] | None,
    httpx_transport: httpx.BaseTransport | None,
    otlp_adapter: requests.adapters.BaseAdapter | None,
) -> SendResult:
    """The set-up, then every case in order, then (only when the expected and confirmed counts are
    equal) the completion marker in Langfuse and, after it was confirmed, in the local file. Any
    failure propagates before the next case: no marker, no shutdown, no further call."""
    run = _set_up(
        keys,
        baseline,
        live_check=limit == 1,
        run_name=run_name,
        commit=commit,
        client_factory=client_factory,
        httpx_transport=httpx_transport,
        otlp_adapter=otlp_adapter,
    )
    # The limit takes cases from the front only, after every start-up refusal; the expected count
    # stays the whole set's length.
    selected = scenarios[:1] if limit == 1 else scenarios
    case_judge = judge if limit is None else None
    entries: list[dict[str, Any]] = []
    for scenario in selected:
        entries.append(
            _send_case(
                run,
                scenario,
                provider_factory,
                case_judge,
                egress_basis=egress_basis,
                model_host=model_host,
            )
        )
    expected, confirmed = len(scenarios), len(entries)
    complete = expected == confirmed
    if complete:
        _send_completion_marker(run, expected, confirmed)
        entries.append(_marker(run, expected, confirmed))
    _shutdown(run.client)
    out = output_path(directory, LOCAL_FILE_NAME)
    write_private(
        out,
        json.dumps(
            {
                "run_name": run_name,
                "commit": commit,
                "egress_basis": egress_basis,
                "prompt_version": PROMPT_VERSION,
                "entries": entries,
            },
            indent=1,
        ),
    )
    return SendResult(run_name, expected, confirmed, complete, out, run.server_version)


def _shutdown(client: Any) -> None:
    """Shuts the client and its provider down after every send was confirmed (nothing is pending).
    A failure fails the run (`shutdown_failed`) before the local file is written, so that file
    never exists, while Langfuse already holds the confirmed completion marker of a complete run
    (ADR 0030 Known gaps: a report cites the local file, not the marker alone)."""
    failed = False
    try:
        client.shutdown()
    except Exception:  # the shutdown error is dropped; the label is raised below
        failed = True
    if failed:
        raise SenderRefused("shutdown_failed")


def _set_up(
    keys: EvalKeys,
    baseline: LoggingBaseline,
    *,
    live_check: bool,
    run_name: str,
    commit: str,
    client_factory: Callable[..., Any] | None,
    httpx_transport: httpx.BaseTransport | None,
    otlp_adapter: requests.adapters.BaseAdapter | None,
) -> _Run:
    import langfuse  # type: ignore[import-not-found]

    remove_sdk_console_handler()
    check_handlers(baseline)
    check_no_unlisted_loggers(baseline)
    models = importlib.import_module("langfuse.api")

    from opentelemetry import trace as trace_api
    from opentelemetry.metrics import NoOpMeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanLimits, TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    recorder = ReplyRecorder()
    httpx_client = build_httpx_client(recorder, httpx_transport)
    session = OtlpSession(otlp_adapter)
    meters = NoOpMeterProvider()
    limits = SpanLimits(
        max_attributes=MAX_SPAN_ATTRIBUTES,
        max_events=0,
        max_links=0,
        max_span_attributes=MAX_SPAN_ATTRIBUTES,
        max_event_attributes=0,
        max_link_attributes=0,
        max_attribute_length=MAX_ATTRIBUTE_CHARS,
        max_span_attribute_length=MAX_ATTRIBUTE_CHARS,
    )
    provider = TracerProvider(
        sampler=ALWAYS_ON,
        resource=Resource({"service.name": "divesafe-eval-sender"}),
        shutdown_on_exit=False,
        span_limits=limits,
        meter_provider=meters,
    )
    global_before = trace_api.get_tracer_provider()
    factory = client_factory if client_factory is not None else langfuse.Langfuse
    client, exporter = _build_client(
        keys,
        factory,
        httpx_client=httpx_client,
        session=session,
        meters=meters,
        provider=provider,
        commit=commit,
    )
    if trace_api.get_tracer_provider() is not global_before:
        raise SenderRefused("global_tracer_provider")
    check_post_build_environment()
    _check_media_off(client)
    check_handlers(baseline)
    check_no_unlisted_loggers(baseline)

    api = _Api(client, models, recorder)
    projects = api.call(PROJECTS_CALL, lambda a: a.projects.get(request_options=dict(NO_RETRIES)))
    project_id = _only_eval_project(projects)
    server_version: str | None = None
    if live_check:
        health = api.call(HEALTH_CALL, lambda a: a.health.health(request_options=dict(NO_RETRIES)))
        reply_version = getattr(health, "version", None)
        del health
        if not is_server_version_shape(reply_version) or _version_holds_key(reply_version, keys):
            # Clean frame: no copy of the refused value stays here (nor in the checks, which
            # returned), and the raise is outside any `except`, so it has no `__context__`.
            del reply_version
            raise SenderRefused("server_version")
        server_version = reply_version
        del reply_version
    dataset = api.call(
        DATASET_CALL,
        lambda a: a.datasets.create(name=DATASET_NAME, request_options=dict(NO_RETRIES)),
    )
    dataset_id = getattr(dataset, "id", None)
    if type(dataset_id) is not str or not dataset_id:
        raise SenderRefused("send_failed")
    return _Run(
        client=client,
        api=api,
        exporter=exporter,
        baseline=baseline,
        run_name=run_name,
        commit=commit,
        project_id=project_id,
        dataset_id=dataset_id,
        experiment=experiment_id(project_id, dataset_id, run_name),
        server_version=server_version,
    )


def _version_holds_key(version: str, keys: EvalKeys) -> bool:
    """Whether the live check's server version holds a key or part of one (a listener on the
    port answering with a key), all compared case-insensitively: either whole key value,
    `pk-lf` / `sk-lf`, or any run of `_KEY_FRAGMENT_CHARS` consecutive characters of the version
    that also appears in either key (a longer shared run contains such a window). It never
    raises and fails closed. Before it returns, every local holding the version, a fold or
    window of it, or a plaintext key is deleted; the caller then deletes its own copy and raises
    `server_version`, so no frame keeps any of them."""
    folded = version.lower()
    public = keys.public.get_secret_value()
    secret = keys.secret.get_secret_value()
    keys_folded = (public.lower(), secret.lower())
    holds = any(key in folded for key in keys_folded) or "pk-lf" in folded or "sk-lf" in folded
    window = ""
    for start in range(len(folded) - _KEY_FRAGMENT_CHARS + 1):
        if holds:
            break
        window = folded[start : start + _KEY_FRAGMENT_CHARS]
        holds = any(window in key for key in keys_folded)
    del version, folded, public, secret, keys_folded, window
    return holds


def _build_client(
    keys: EvalKeys,
    factory: Callable[..., Any],
    *,
    httpx_client: httpx.Client,
    session: OtlpSession,
    meters: Any,
    provider: Any,
    commit: str,
) -> tuple[Any, RecordingExporter]:
    """Builds the OTLP exporter (with the authorization header) and the client. The plaintext
    keys and the header exist as names only in this frame and are deleted before it returns. Any
    build error is dropped and `client_build` is raised outside the `except`, so no SDK text and
    no `__context__` survives. Before that raise, the `Authorization` header is removed from the
    sender's `session` (in case the exporter put it there) and every local that holds the header
    or the keys (the OTLP exporter, its recording wrapper and any partly built client) is deleted,
    so neither the raising frame nor the session holds them."""
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    inner: Any = None
    client: Any = None
    exporter: RecordingExporter | None = None
    failed = False
    public = keys.public.get_secret_value()
    secret = keys.secret.get_secret_value()
    authorization = "Basic " + _b64(f"{public}:{secret}")
    try:
        inner = OTLPSpanExporter(
            endpoint=LANGFUSE_BASE_URL + OTLP_TRACES_PATH,
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
        exporter = RecordingExporter(
            inner, session, SpanExportResult.SUCCESS, SpanExportResult.FAILURE
        )
        client = factory(
            public_key=public,
            secret_key=secret,
            base_url=LANGFUSE_BASE_URL,
            timeout=CLIENT_TIMEOUT_SECONDS,
            httpx_client=httpx_client,
            debug=False,
            tracing_enabled=True,
            flush_at=FLUSH_AT,
            flush_interval=FLUSH_INTERVAL_SECONDS,
            environment=EXPERIMENT_ENVIRONMENT,
            release=commit,
            media_upload_thread_count=1,
            sample_rate=1.0,
            tracer_provider=provider,
            span_exporter=exporter,
        )
    except Exception:  # the build error is dropped; the label is raised below
        failed = True
    finally:
        del public, secret, authorization
    if failed or exporter is None:
        session.headers.pop("Authorization", None)
        del inner, exporter, client
        raise SenderRefused("client_build")
    del inner
    return client, exporter


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("ascii")).decode("ascii")


def _check_media_off(client: Any) -> None:
    """The client reports media upload off and started no media upload thread (pinned SDK
    internals; a client without them refuses)."""
    resources = getattr(client, "_resources", None)
    enabled = getattr(resources, "_media_upload_enabled", None)
    consumers = getattr(resources, "_media_upload_consumers", None)
    if enabled is not False or type(consumers) is not list or consumers:
        raise SenderRefused("media_upload")


def _only_eval_project(projects: Any) -> str:
    """The authenticated read shows `divesafe-eval` and only it."""
    data = getattr(projects, "data", None)
    if not isinstance(data, list) or len(data) != 1:
        raise SenderRefused("project")
    project_id = getattr(data[0], "id", None)
    if getattr(data[0], "name", None) != LANGFUSE_PROJECT or type(project_id) is not str:
        raise SenderRefused("project")
    return project_id


def _send_case(
    run: _Run,
    scenario: harness.Scenario,
    provider_factory: Callable[[], LLMProvider | None],
    judge: Judge | None,
    *,
    egress_basis: str,
    model_host: str,
) -> dict[str, Any]:
    """One case: run, check the record, send the dataset item, call the judge (none while any
    span is open or unconfirmed), then the case's span, its code scores before its judge scores,
    and confirm every item before returning."""
    inner = provider_factory()
    if inner is None:
        raise SenderRefused("provider_missing")
    provider = CapturingProvider(inner)
    record: AssessmentRecord | None = None
    refused = False
    try:
        record = harness.run_record(
            scenario,
            provider,
            egress_basis=egress_basis,
            model_host=model_host,
        )
    except SupervisorInvariantError:
        refused = True
    if record is not None:
        harness.check_record_egress(
            record, provider, egress_basis=egress_basis, model_host=model_host
        )
        check_record_content(record)
        code = code_scores_for_record(record, scenario.expected_deterministic, scenario_set=True)
    elif refused:
        code = code_scores_for_refusal()
    else:  # unreachable: `run_record` returns a record or raises
        raise SenderRefused("send_failed")

    item = run.api.call(
        DATASET_ITEM_CALL,
        lambda a: a.dataset_items.create(
            dataset_name=DATASET_NAME,
            id=f"{DATASET_NAME}-{scenario.name}",
            input={"case": scenario.name, "description": scenario.description},
            expected_output={"expected_rule_outcome": scenario.expected_deterministic.value},
            metadata={"case_set": "SCENARIOS"},
            request_options=dict(NO_RETRIES),
        ),
    )
    item_id = getattr(item, "id", None)
    if type(item_id) is not str or not item_id:
        raise SenderRefused("send_failed")

    judged: Sequence[JudgeScore] = ()
    if judge is not None and record is not None:
        judged = tuple(judge(scenario, record))

    span_id, trace_id = _send_span(run, scenario, provider, record, item_id)

    run.api.scores([_code_request(run.api, s, trace_id, span_id) for s in code])
    if judged:
        run.api.scores([_judge_request(run.api, s, trace_id, span_id) for s in judged])

    flushed = True
    try:
        run.client.flush()
    except Exception:  # the flush error is dropped; the label is raised below
        flushed = False
    if not flushed or not run.exporter.confirmed([span_id]):
        raise SenderRefused("send_failed")
    check_handlers(run.baseline)
    check_no_unlisted_loggers(run.baseline)
    return {
        "case": scenario.name,
        "code_scores": [{"name": s.name, "value": s.value} for s in code],
        "judge_scores": [{"name": s.name, "value": s.value} for s in judged],
    }


def _send_span(
    run: _Run,
    scenario: harness.Scenario,
    provider: CapturingProvider,
    record: AssessmentRecord | None,
    item_id: str,
) -> tuple[str, str]:
    """The case's span: started, given its experiment attributes and ended. A case Supervise
    refused (no record) sends `input=None` and `output=None`. Any SDK error is dropped and
    `send_failed` is raised outside the `except`."""
    ids: tuple[str, str] | None = None
    try:
        span = run.client.start_observation(
            name=CASE_SPAN_NAME,
            as_type="span",
            input={"prompts": provider.prompts} if record is not None else None,
            output=record_outputs(record) if record is not None else None,
            metadata={"case": scenario.name},
        )
        span_id = span.id
        trace_id = span.trace_id
        run.exporter.started(span_id)
        otel = span._otel_span
        for key, value in (
            (ATTR_ENVIRONMENT, EXPERIMENT_ENVIRONMENT),
            (ATTR_EXPERIMENT_ID, run.experiment),
            (ATTR_EXPERIMENT_NAME, run.run_name),
            (ATTR_DATASET_ID, run.dataset_id),
            (ATTR_ITEM_ID, item_id),
            (ATTR_ROOT_OBSERVATION_ID, span_id),
        ):
            otel.set_attribute(key, value)
        span.end()
        ids = (span_id, trace_id)
    except Exception:  # the SDK error is dropped; the label is raised below
        ids = None
    if ids is None:
        raise SenderRefused("send_failed")
    return ids


def _code_request(api: _Api, score: CodeScore, trace_id: str, span_id: str) -> Any:
    return api.score_request(
        trace_id=trace_id,
        observation_id=span_id,
        name=score.name,
        value=score.value,
        data_type=api.data_type(score.data_type),
    )


def _judge_request(api: _Api, score: JudgeScore, trace_id: str, span_id: str) -> Any:
    return api.score_request(
        trace_id=trace_id,
        observation_id=span_id,
        name=score.name,
        value=score.value,
        data_type=api.data_type(score.data_type),
        comment=score.comment,
    )


def _marker_fields(run: _Run, expected: int, confirmed: int) -> Mapping[str, Any]:
    return {
        "expected_cases": expected,
        "confirmed_cases": confirmed,
        "dataset_run_id": run.experiment,
        "commit": run.commit,
    }


def _send_completion_marker(run: _Run, expected: int, confirmed: int) -> None:
    """The run-level score marking the run complete: only the two case counts, the run's
    code-set id (the experiment id) and the commit."""
    fields = _marker_fields(run, expected, confirmed)
    run.api.scores(
        [
            run.api.score_request(
                dataset_run_id=run.experiment,
                name=COMPLETION_SCORE_NAME,
                value=float(confirmed),
                data_type=run.api.data_type("NUMERIC"),
                metadata={
                    "expected_cases": fields["expected_cases"],
                    "confirmed_cases": fields["confirmed_cases"],
                    "commit": fields["commit"],
                },
            )
        ]
    )


def _marker(run: _Run, expected: int, confirmed: int) -> dict[str, Any]:
    return {"completion_marker": dict(_marker_fields(run, expected, confirmed))}
