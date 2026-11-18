"""Operational metrics in the Prometheus text format (ADR 0027 item 2).

The only module in `src` that imports `prometheus_client`. A private `CollectorRegistry` holds
exactly the metrics below; the global registry, its default process, platform and GC collectors,
multiprocess mode, exemplars and the library's own HTTP servers are never used. Every label value
comes from a code enum or a fixed set in code, never from a request, a record's free text, an
exception or an id. Counts are per process.

Run counts are taken after the run has stored its record or its failure code, from what the run
already produced (the stored record's stage trace and the run's collector), inside the caller's
`suppress`: metrics never change a run. A run that fails before it is stored gives only its
failure code and its model calls, never stage, connector or knowledge counts.

`disable_created_metrics()` below is process-wide in the library: any later user of
`prometheus_client` in this process gets no `_created` series either. Start-up refuses any
`PROMETHEUS_*` variable (`check_metrics_startup`), because the library takes its storage mode
and other settings from the environment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily
from prometheus_client.registry import Collector

from divesafe.agents.runner import CALL_OUTCOMES
from divesafe.domain import AssessmentRecord, Recommendation, StageName, StageOutcome
from divesafe.domain.stages import QUALIFIER_PATTERN, UNNAMED_QUALIFIER
from divesafe.models.llm import PROVIDER_FAILURE_REASONS
from divesafe.observability import DROP_REASONS, RunTrace, drop_counts
from divesafe.services.runs import FailureCode

logger = logging.getLogger(__name__)

CONTENT_TYPE = CONTENT_TYPE_LATEST
METRICS_REFUSAL = (
    "refusing to start: a PROMETHEUS_* variable is set; /metrics takes no library setting from "
    "the environment (ADR 0027 item 2)"
)
# A timestamp series per labelled child would double the output and say nothing operational.
disable_created_metrics()  # type: ignore[no-untyped-call]

# Operational histogram buckets in seconds (owner, 2026-11-17); not safety thresholds.
STAGE_BUCKETS: tuple[float, ...] = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

FINISHED_OUTCOMES: tuple[str, ...] = (
    *(r.value for r in Recommendation),
    *(f"failed_{c.value}" for c in FailureCode),
)
KNOWLEDGE_OUTCOMES: tuple[str, ...] = ("ok", "not_configured", "failed", "timed_out", "issue")
CONNECTOR_KINDS: tuple[str, ...] = ("failed", "timed_out")
OTHER_CONNECTOR = "other"

_KNOWLEDGE_CODES: Mapping[str, str] = {
    "retrieve.knowledge_not_configured": "not_configured",
    "retrieve.not_configured": "not_configured",
    "retrieve.knowledge_failed": "failed",
    "retrieve.knowledge_timed_out": "timed_out",
    "retrieve.knowledge_issue": "issue",
}
_CONNECTOR_PREFIXES: Mapping[str, str] = {
    "observe.connector_failed:": "failed",
    "observe.connector_timed_out:": "timed_out",
}


def check_metrics_startup(environ: Mapping[str, str]) -> None:
    """Refuse every `PROMETHEUS_*` variable, in any case. Called before the first metric is
    built: the library creates no value storage until then."""
    if any(name.upper().startswith("PROMETHEUS_") for name in environ):
        raise RuntimeError(METRICS_REFUSAL)


class _DropCollector(Collector):
    """The trace drop counts, read at scrape time from `observability`, by their fixed reasons."""

    def collect(self) -> Iterable[CounterMetricFamily]:
        family = CounterMetricFamily(
            "divesafe_trace_dropped",
            "Trace data dropped, by fixed reason.",
            labels=["reason"],
        )
        counts = drop_counts()
        for reason in DROP_REASONS:
            family.add_metric([reason], float(counts.get(reason, 0)))
        yield family


class Metrics:
    """The fixed metric set. `connector_names` are the code names of the configured
    connectors; any other qualifier is counted as `other`."""

    def __init__(self, connector_names: Iterable[str], in_flight: Callable[[], int]) -> None:
        self._connectors = frozenset(
            n
            for n in connector_names
            if type(n) is str
            and QUALIFIER_PATTERN.fullmatch(n)
            and not n.startswith(UNNAMED_QUALIFIER)
            and n != OTHER_CONNECTOR
        )
        registry = CollectorRegistry(auto_describe=True)
        self.registry = registry
        self._created = Counter(
            "divesafe_assessments_created",
            "Assessment runs started.",
            registry=registry,
        )
        self._finished = Counter(
            "divesafe_assessments_finished",
            "Assessment runs ended: the system's final recommendation at storage (before any "
            "human decision), or failed_<code>.",
            ["outcome"],
            registry=registry,
        )
        self._stage_seconds = Histogram(
            "divesafe_stage_duration_seconds",
            "Stage wall time of stored assessments.",
            ["stage", "outcome"],
            buckets=STAGE_BUCKETS,
            registry=registry,
        )
        self._connector = Counter(
            "divesafe_connector_failures",
            "Connector failures and timeouts in stored assessments.",
            ["connector", "kind"],
            registry=registry,
        )
        self._knowledge = Counter(
            "divesafe_knowledge_retrieval",
            "Knowledge retrieval outcome of stored assessments.",
            ["outcome"],
            registry=registry,
        )
        self._calls = Counter(
            "divesafe_model_calls",
            "Model calls, by the fixed call outcome.",
            ["outcome"],
            registry=registry,
        )
        self._provider_failures = Counter(
            "divesafe_model_call_provider_failures",
            "Model calls with outcome provider_error, by the adapter's fixed failure label.",
            ["reason"],
            registry=registry,
        )
        self._auth = Counter(
            "divesafe_auth_failures",
            "Failed authentications (401).",
            registry=registry,
        )
        running = Gauge(
            "divesafe_runs_in_flight", "Assessment runs in this process.", registry=registry
        )
        running.set_function(lambda: float(in_flight()))
        registry.register(_DropCollector())
        for outcome in FINISHED_OUTCOMES:
            self._finished.labels(outcome)
        for outcome in KNOWLEDGE_OUTCOMES:
            self._knowledge.labels(outcome)
        for call in sorted(CALL_OUTCOMES):
            self._calls.labels(call)
        for reason in sorted(PROVIDER_FAILURE_REASONS):
            self._provider_failures.labels(reason)
        for name in sorted(self._connectors | {OTHER_CONNECTOR}):
            for kind in CONNECTOR_KINDS:
                self._connector.labels(name, kind)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def assessment_created(self) -> None:
        self._created.inc()

    def auth_failure(self) -> None:
        self._auth.inc()

    def run_finished(
        self,
        record: AssessmentRecord | None,
        failure: FailureCode | None,
        trace: RunTrace | None,
    ) -> None:
        """One run's counts. `record` is the stored record (None when the run failed)."""
        if failure is not None:
            self._finished.labels(f"failed_{FailureCode(failure).value}").inc()
        elif record is not None:
            self._finished.labels(Recommendation(record.final_recommendation).value).inc()
        if trace is not None:
            for call in trace.model_calls:
                outcome = call.fields.get("outcome")
                if type(outcome) is str and outcome in CALL_OUTCOMES:
                    self._calls.labels(outcome).inc()
                if type(outcome) is str and outcome == "provider_error":
                    reason = call.fields.get("provider_reason")
                    if type(reason) is not str or reason not in PROVIDER_FAILURE_REASONS:
                        reason = "unclassified"
                    self._provider_failures.labels(reason).inc()
        if failure is None and record is not None:
            self._stored(record)

    def _stored(self, record: AssessmentRecord) -> None:
        for stage in record.stages:
            name, outcome = StageName(stage.name).value, StageOutcome(stage.outcome).value
            self._stage_seconds.labels(name, outcome).observe(max(0, stage.duration_ms) / 1000)
            if stage.name is StageName.OBSERVE:
                for code in stage.issues:
                    for prefix, kind in _CONNECTOR_PREFIXES.items():
                        if code.startswith(prefix):
                            connector = code.removeprefix(prefix)
                            if connector not in self._connectors:
                                connector = OTHER_CONNECTOR
                            self._connector.labels(connector, kind).inc()
            elif stage.name is StageName.RETRIEVE:
                found = [_KNOWLEDGE_CODES[c] for c in stage.issues if c in _KNOWLEDGE_CODES]
                self._knowledge.labels(found[0] if found else "ok").inc()
