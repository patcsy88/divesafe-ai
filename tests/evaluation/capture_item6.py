"""ADR 0025 item 6: capture one recorded difficult run as labelled fixtures. OFFLINE BY DEFAULT.

    .venv/bin/python -m tests.evaluation.capture_item6              # dry run (the default)
    DIVESAFE_CAPTURE_CONFIRM=item6 DIVESAFE_CAPTURE_DIR=<outside repo> \
        DIVESAFE_ALLOW_EXTERNAL_LLM=true DIVESAFE_LLM_PROVIDER=openai_compatible \
        DIVESAFE_LLM_MODEL=claude-sonnet-5-5 DIVESAFE_LLM_BASE_URL=<endpoint> \
        .venv/bin/python -m tests.evaluation.capture_item6 --live   # the owner's live run

The dry run prints the plan and runs the egress gate offline. It prints no secret, no base URL,
no host and no token, and opens no connection. A live run needs `--live`, the owner's confirmation
and the provider settings above; it builds the production state (`api.state.build_default_state`
and `build_provider`), runs one assessment, and writes only what the recorder captured at the
payload boundary: provider reply text after parsing, and connector payloads as the connectors
received them. Raw HTTP, request URLs, headers, tokens, provider metadata, hosts and ports are never
written. Every file is scrubbed and screened before it is written; a rule that fires refuses the
capture. PHN payloads are excluded (ADR 0021).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.evaluation import item6_scrub
from tests.evaluation.egress_gate import egress_gate

from divesafe.agents.prompts import PROMPT_VERSION
from divesafe.config import Settings
from divesafe.data import Connector, DiveSite, JsonGetter
from divesafe.data.met_api_marine import SOURCE as MET_SOURCE
from divesafe.models.egress import EgressBasis
from divesafe.models.llm import LLMRequest, LLMResponse
from divesafe.models.openai_compatible import FAILURE_REASONS, failure_reason

REPO = item6_scrub.REPO
SITE_ID = "my-pahang-pulau-tioman"
PLANNED_START = datetime(2026, 10, 30, 11, 0, tzinfo=UTC)  # all times UTC (owner decision)
DURATION_MINUTES = 60
PROVIDER = "openai_compatible"
MODEL = "claude-sonnet-5-5"
CONFIRM_ENV = "DIVESAFE_CAPTURE_CONFIRM"
CONFIRM_VALUE = "item6"
DIR_ENV = "DIVESAFE_CAPTURE_DIR"
# The only connectors the live run may call: the agreed sources from the production connector set
# (api/state.py). An allow-list, not an exclusion, so a new connector is refused until it is added
# here on purpose. PHN (ADR 0021, no permission wording on file) is not on it.
ALLOWED_CONNECTORS = frozenset(
    {
        "open-meteo-marine",
        "open-meteo-wind",
        "data-gov-my-weather-forecast",
        "data-gov-my-weather-warning",
        "met-api-marine-forecast",
    }
)
# Minimum seconds between MET calls: MET allows 3 a minute (MET_API_QUOTA, the stricter pair), so
# spacing them 20 s apart keeps a run of three within one minute.
MET_MIN_SPACING_SECONDS = 20.0
MET_HOST = "api.met.gov.my"


class CaptureRefused(RuntimeError):
    """A refusal with a fixed message. Nothing has been connected to when it is raised."""


def utc_now() -> datetime:
    """The real UTC time. The only clock the live run reads for `now`."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Plan:
    site_id: str
    planned_start: datetime
    duration_minutes: int
    provider: str
    model: str

    def describe(self) -> dict[str, Any]:
        # No base URL, no host, no key: the plan is safe to print.
        return {
            "site_id": self.site_id,
            "planned_start_utc": self.planned_start.isoformat(),
            "duration_minutes": self.duration_minutes,
            "provider": self.provider,
            "model": self.model,
        }


def build_plan() -> Plan:
    return Plan(SITE_ID, PLANNED_START, DURATION_MINUTES, PROVIDER, MODEL)


def check_output_dir(directory: Path, repo: Path = REPO) -> Path:
    """Refuse a directory inside the repository working tree, or one that cannot be resolved.

    F7: each ancestor is compared with `os.path.samefile`, not by text. On macOS's case-insensitive
    volume, `/Users/.../divesafe ai/tests` is the same directory as the repository, and a text
    comparison would miss it. An existing file given as the output directory is refused."""
    resolved = directory.expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise CaptureRefused("the capture directory is an existing file; it must be a directory")
    root = repo.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.exists() and os.path.samefile(candidate, root):
            raise CaptureRefused(
                "the capture directory is inside the repository; it must be outside"
            )
    return resolved


def check_provider_settings(environ: Mapping[str, str]) -> None:
    """The live run is only for the owner's named provider and model, and only with the external
    opt-in, which the operator must set explicitly. Messages never echo a value."""
    if environ.get("DIVESAFE_LLM_PROVIDER") != PROVIDER:
        raise CaptureRefused(f"refused: DIVESAFE_LLM_PROVIDER must be {PROVIDER}")
    if environ.get("DIVESAFE_LLM_MODEL") != MODEL:
        raise CaptureRefused(f"refused: DIVESAFE_LLM_MODEL must be {MODEL}")
    if not environ.get("DIVESAFE_LLM_BASE_URL"):
        raise CaptureRefused("refused: DIVESAFE_LLM_BASE_URL is not set")
    if environ.get("DIVESAFE_ALLOW_EXTERNAL_LLM") != "true":
        raise CaptureRefused("refused: DIVESAFE_ALLOW_EXTERNAL_LLM=true is not set")


GUARDED_SETTINGS = (
    "DIVESAFE_LLM_PROVIDER",
    "DIVESAFE_LLM_MODEL",
    "DIVESAFE_ALLOW_EXTERNAL_LLM",
    "DIVESAFE_LLM_BASE_URL",
    "DIVESAFE_LLM_SERVER_IS_LOCAL",
)


def check_settings_match_guard(environ: Mapping[str, str] | None = None) -> None:
    """`check_provider_settings` reads exact-case names, but `Settings` reads DIVESAFE_* names
    case-insensitively, so a variant such as `divesafe_llm_model` can set a value the exact-case
    check never saw. A variant of a guarded name in the environment `Settings` reads is refused,
    whatever its value: the guard cannot tell which spelling wins for each name, so none is
    trusted. Only the name category is printed, never a name's value."""
    source = os.environ if environ is None else environ
    for name in GUARDED_SETTINGS:
        if any(key != name and key.upper() == name for key in source):
            raise CaptureRefused(
                "refused: a case variant of a guarded DIVESAFE_ setting is set; "
                "the live run needs the exact names"
            )


def offline_egress_basis(settings: Settings) -> EgressBasis:
    """The egress gate, offline. Builds a provider object without any connection; never calls it.
    The base URL is never printed."""
    from divesafe.models.openai_compatible import OpenAICompatibleProvider

    if settings.llm_base_url is None or settings.llm_model is None:
        raise CaptureRefused("DIVESAFE_LLM_BASE_URL and DIVESAFE_LLM_MODEL must be set")
    provider = OpenAICompatibleProvider(
        settings.llm_model,
        str(settings.llm_base_url),
        allowed_hosts=settings.llm_allowed_host_set(),
        api_key=settings.llm_api_key,
    )
    try:
        return egress_gate(
            provider,
            allow_external=settings.allow_external_llm,
            server_is_local=settings.llm_server_is_local,
        )
    except pytest.skip.Exception as exc:
        # egress_gate refuses by skipping. A skip is a BaseException, so it would escape main's
        # handlers as a traceback. It is turned into a refusal with a fixed text: the class name
        # only, never the configuration.
        raise CaptureRefused(
            f"refused: the egress check stopped ({type(exc).__name__}); see ADR 0023"
        ) from None


def run_dry(settings: Settings) -> dict[str, Any]:
    """Offline: plan, egress check, and the output-dir check when one is set. No network."""
    report: dict[str, Any] = {"mode": "dry_run", "plan": build_plan().describe()}
    report["egress_basis"] = offline_egress_basis(settings)
    raw = os.environ.get(DIR_ENV)
    if raw:
        report["output_dir"] = "set and outside the repository"
        check_output_dir(Path(raw))
    else:
        report["output_dir"] = f"not set ({DIR_ENV})"
    report["note"] = "dry run: no connection was made; pass --live with the confirmation to capture"
    return report


def require_live(argv_live: bool, environ: Mapping[str, str]) -> Path:
    """Every guard for the live run. Returns the resolved output directory."""
    if not argv_live:
        raise CaptureRefused("refused: --live was not given (dry run only)")
    if environ.get(CONFIRM_ENV) != CONFIRM_VALUE:
        raise CaptureRefused(f"refused: {CONFIRM_ENV}={CONFIRM_VALUE} is not set by the owner")
    check_provider_settings(environ)
    raw = environ.get(DIR_ENV)
    if not raw:
        raise CaptureRefused(f"refused: {DIR_ENV} is not set")
    return check_output_dir(Path(raw))


# A task label is code, not text: `TASK: <label>` is the first line of every agent user message.
_LABEL_PREFIX = "TASK: "
_LABEL_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]+")
UNLABELLED = "unlabelled"


def fixed_label(text: str) -> str:
    """The leading code token of a label, or `unlabelled`. Anything after the first space (free
    text, exception wording) is dropped, so a label can never carry a reply or an error message."""
    match = _LABEL_TOKEN.match(text)
    token = match.group(0).rstrip(":") if match else ""
    return token[:128] if token else UNLABELLED


def task_label(request: LLMRequest) -> str:
    for message in request.messages:
        if message.role == "user":
            first = message.content.split("\n", 1)[0]
            if first.startswith(_LABEL_PREFIX):
                return fixed_label(first[len(_LABEL_PREFIX) :])
    return UNLABELLED


@dataclass
class Recorder:
    """Keeps what may be written: provider reply text keyed by task label, a fixed code for each
    failed or refused call, and connector payloads. Nothing else.

    Keyed by label, not by completion order, so parallel calls cannot swap replies. Two calls with
    the same label (not expected in this run) get `#2`, `#3` in arrival order."""

    connector_payloads: dict[str, list[Any]] = field(default_factory=dict)
    replies: dict[str, str] = field(default_factory=dict)
    call_codes: dict[str, str] = field(default_factory=dict)

    def keep_payload(self, source: str, payload: Any) -> None:
        self.connector_payloads.setdefault(source, []).append(payload)

    def _key(self, label: str) -> str:
        key, number = label, 2
        while key in self.replies or key in self.call_codes:
            key, number = f"{label}#{number}", number + 1
        return key

    def keep_reply(self, label: str, text: str) -> None:
        self.replies[self._key(label)] = text

    def keep_code(self, label: str, code: str) -> None:
        self.call_codes[self._key(label)] = code


class RecordingGetter:
    """Wraps a JSON getter and records the parsed payload it returns. The URL, the parameters and
    the headers are never kept. Forwards to the wrapped getter unchanged."""

    def __init__(self, inner: JsonGetter, recorder: Recorder, source: str) -> None:
        self._inner = inner
        self._recorder = recorder
        self._source = source

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        payload = await self._inner.get_json(url, params)
        self._recorder.keep_payload(self._source, payload)
        return payload


class PacedGetter:
    """Spaces calls to one host by at least `spacing` seconds on an injected clock. A call that
    would come too soon waits on the injected `sleep`, so the spacing is testable with no real
    sleep. Used for MET only (MET_API_QUOTA allows 3 a minute)."""

    def __init__(
        self,
        inner: JsonGetter,
        *,
        host: str,
        spacing: float,
        clock: Callable[[], float],
        sleep: Callable[[float], Any],
    ) -> None:
        self._inner = inner
        self._host = host
        self._spacing = spacing
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        from urllib.parse import urlsplit

        if (urlsplit(url).hostname or "") == self._host and self._last is not None:
            wait = self._spacing - (self._clock() - self._last)
            if wait > 0:
                await self._sleep(wait)
        if (urlsplit(url).hostname or "") == self._host:
            self._last = self._clock()
        return await self._inner.get_json(url, params)


# The provider settings the runner reads from the provider object (F9). They are passed through so
# the run uses the same budgets as the recorded settings, not the runner's defaults.
PASSED_PROVIDER_SETTINGS = (
    "call_timeout_seconds",
    "stage_timeout_seconds",
    "max_output_tokens",
)
CALL_FAILED = "call_failed"
CALL_CANCELLED = "call_cancelled"


def failure_code(exc: BaseException) -> str:
    """The fixed code kept for a failed call: `call_failed:<label>`. The label is one of
    FAILURE_REASONS, or "unclassified". Exception text and the class name are never kept."""
    label = failure_reason(exc)
    if label not in FAILURE_REASONS:
        label = "unclassified"
    return f"{CALL_FAILED}:{label}"


class RecordingProvider:
    """Wraps the provider and keeps only the parsed reply text, keyed by task label, or a fixed
    code when a call fails. Model metadata and exception text are not kept."""

    def __init__(self, inner: Any, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder
        self.name = inner.name
        self.model = inner.model
        self.external = inner.external
        for attribute in PASSED_PROVIDER_SETTINGS:
            if hasattr(inner, attribute):
                setattr(self, attribute, getattr(inner, attribute))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        label = task_label(request)
        try:
            response: LLMResponse = await self._inner.complete(request)
        except asyncio.CancelledError:
            # A cancelled call (for example a wait_for timeout) keeps a fixed code only. The
            # cancellation always propagates: it is never swallowed.
            self._recorder.keep_code(label, CALL_CANCELLED)
            raise
        except Exception as exc:
            self._recorder.keep_code(label, failure_code(exc))
            raise
        self._recorder.keep_reply(label, response.text)
        return response


def build_live_connectors(
    state_connectors: list[Connector],
    recorder: Recorder,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], Any],
) -> list[Connector]:
    """Keep only the connectors that are not excluded, and wrap their getters so payloads are
    recorded and MET is paced. The connectors themselves are the production objects."""
    kept: list[Connector] = []
    for connector in state_connectors:
        # Not on the allow-list: never kept, and its getter is never touched (F1).
        if connector.name not in ALLOWED_CONNECTORS:
            continue
        getter = getattr(connector, "_getter", None)
        # A connector with no getter cannot be recorded or paced, so it is refused, not called
        # through an unwrapped path (F1).
        if getter is None:
            raise CaptureRefused("refused: a connector has no getter to record")
        wrapped: JsonGetter = RecordingGetter(getter, recorder, connector.name)
        if connector.name == MET_SOURCE:
            wrapped = PacedGetter(
                wrapped,
                host=MET_HOST,
                spacing=MET_MIN_SPACING_SECONDS,
                clock=clock,
                sleep=sleep,
            )
        connector._getter = wrapped  # type: ignore[attr-defined]
        kept.append(connector)
    return kept


CAPTURED_KEY = "captured_at_utc"


def captured_at_utc(now: datetime) -> str:
    """`now` as ISO 8601 in UTC (`+00:00`). A naive datetime, or one with any non-zero offset, is
    refused with a fixed message, as `live_clock` refuses a bad reading: the capture time is UTC or
    nothing."""
    # `tzinfo != UTC` refuses a named zone with a zero offset in winter (such as Europe/London) as
    # well as naive and offset values; `timezone(timedelta(0))` equals UTC and is accepted.
    if now.tzinfo is None or now.tzinfo != UTC:
        raise CaptureRefused("refused: the capture time is not a UTC datetime")
    return now.isoformat()


def assemble_document(recorder: Recorder, record_summary: Mapping[str, Any]) -> dict[str, Any]:
    """The only shape that is written. Everything here was already kept by the recorder. The
    capture time is carried in the summary under `CAPTURED_KEY` and lifted to the top level here, so
    it is never written inside `result`."""
    summary = dict(record_summary)
    captured = summary.pop(CAPTURED_KEY, None)
    document: dict[str, Any] = {
        "item": "ADR 0025 item 6",
        "site_id": SITE_ID,
        "planned_start_utc": PLANNED_START.isoformat(),
        "duration_minutes": DURATION_MINUTES,
        "synthetic": False,
        "replies": dict(recorder.replies),
        "call_codes": dict(recorder.call_codes),
        "connector_payloads": {k: v for k, v in recorder.connector_payloads.items()},
        "result": summary,
    }
    if captured is not None:
        # The instant `now` was read for this assessment (the same reading, never a second one).
        document[CAPTURED_KEY] = captured
    return document


def configured_secret_values(settings: Settings) -> dict[str, str]:
    """Each configured value that must never be written, keyed by its category name, taken from
    the `Settings` object the run actually uses (not the raw environment, so a lowercase or
    differently cased variable cannot configure an unscreened value). The host is lowercased on
    both sides of the comparison. The values are used only to search the text; they are never
    returned in a message."""
    values: dict[str, str] = {}
    if settings.llm_base_url is not None:
        base_url = str(settings.llm_base_url)
        values["base_url"] = base_url
        from urllib.parse import urlsplit

        parts = urlsplit(base_url)
        if parts.hostname:
            values["host"] = parts.hostname.lower()
        if parts.port is not None:
            values["port"] = str(parts.port)
    if settings.llm_api_key is not None:
        values["api_key"] = settings.llm_api_key.get_secret_value()
    if settings.met_api_token is not None:
        values["met_token"] = settings.met_api_token.get_secret_value()
    return values


def screen_configured_values(text: str, settings: Settings) -> list[str]:
    """Category names of every configured value found in the text. Only the category is returned,
    never the value (S2). A value shorter than 4 characters is not searched, so a port such as 80
    cannot refuse every capture on digits that happen to match. The host is matched
    case-insensitively: the text is lowercased as well."""
    categories: list[str] = []
    lowered = text.lower()
    for category, value in configured_secret_values(settings).items():
        if len(value) < 4:
            continue
        if category == "host":
            if value in lowered:
                categories.append(category)
        elif value in text:
            categories.append(category)
    return categories


def write_capture(
    directory: Path, name: str, payload: dict[str, Any], settings: Settings | None = None
) -> Path:
    """Scrub, screen, then write. Any finding refuses the whole capture: no partial file. The
    configured values come from `settings` (the run's own Settings); with none given, a fresh
    `Settings` is read from the environment, as the run itself does."""
    findings = item6_scrub.find_forbidden_keys(payload)
    text = json.dumps(payload, indent=1, ensure_ascii=False)
    hits = item6_scrub.scan_text(text)
    configured = screen_configured_values(
        text, settings if settings is not None else Settings(_env_file=None)
    )
    if findings or hits or configured:
        raise CaptureRefused(
            "refused to write: the scrub check fired ("
            + ", ".join(sorted(set(hits + findings + [f"configured:{c}" for c in configured])))
            + ")"
        )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    check_new_output(path)
    # "x" is O_EXCL: the create fails if anything is at the name, including a symlink.
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
    return path


def check_new_output(path: Path) -> None:
    """Refuse a name that already exists, or any symlink at it, dangling or not (F6). Called
    before any MET or LLM call, so nothing is fetched for a capture that cannot be written."""
    if path.is_symlink() or path.exists():
        raise CaptureRefused("refused: the capture file name is already in use")


async def run_live_assessment(
    *,
    state: Any,
    recorder: Recorder,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Any],
) -> dict[str, Any]:
    """One assessment for the item 6 plan, through the production `assess_dive`. Returns the
    record's summary fields only (no raw record, no provider metadata)."""
    from divesafe.data.sites import SITES
    from divesafe.domain import DivePlan
    from divesafe.orchestration import RunOptions, assess_dive

    site: DiveSite = SITES[SITE_ID]
    plan = DivePlan(
        site_id=site.id,
        planned_start=PLANNED_START,
        planned_duration_minutes=DURATION_MINUTES,
        max_depth_m=18,
    )
    connectors = build_live_connectors(
        list(state.connectors), recorder, clock=lambda: clock().timestamp(), sleep=sleep
    )
    now = clock()
    # Refused before anything else reads the connectors or the provider: a naive or non-UTC reading
    # never reaches the assessment.
    captured = captured_at_utc(now)
    if now >= PLANNED_START:
        raise CaptureRefused("refused: the planned start is not after the real UTC time")
    record = await assess_dive(
        assessment_id="item6-capture",
        plan=plan,
        site=site,
        connectors=connectors,
        engine=state.engine,
        now=now,
        provider=RecordingProvider(state.provider, recorder),
        clock=clock,
        options=RunOptions(cross_examination=False),
    )
    # Fixed codes and labels only: no reply text, no issue wording from a connector or a model.
    return {
        CAPTURED_KEY: captured,
        "final_recommendation": record.final_recommendation.value,
        "status": record.status,
        "deterministic_recommendation": record.deterministic_recommendation.value,
        "evidence_items": len(record.evidence),
        "evidence_issues": sorted(_fixed_codes(record.evidence_issues)),
        "withheld_evidence": len(record.withheld_evidence),
        "agent_issues": sorted(_fixed_codes(record.agent_issues)),
        "max_depth_m": plan.max_depth_m,
        "ruleset_version": record.ruleset_version,
        "prompt_version": PROMPT_VERSION,
        "model_version": record.model_version,
        "data_versions": {k: str(v) for k, v in sorted(record.data_versions.items())},
        "stages": [s.name.value for s in record.stages],
        "stage_issues": sorted({code for s in record.stages for code in s.issues}),
        "connector_outcomes": _connector_outcomes(record.stages),
        "connectors_run": sorted(c.name for c in connectors),
        "connector_outcomes_note": CONNECTOR_OUTCOMES_NOTE,
        "egress_basis": state.egress_basis,
    }


def egress_basis_label(provider: Any, settings: Settings) -> str:
    """The egress basis as one of the four fixed `EgressBasis` labels (ADR 0023), from the same
    `check_egress` the start-up gate uses. The label is a literal, never the base URL or host."""
    from divesafe.models.egress import check_egress

    basis: str = check_egress(provider, settings)
    if basis not in EGRESS_BASES:
        raise CaptureRefused("refused: the egress basis is not a known label")
    return basis


EGRESS_BASES = frozenset(
    {"none", "declared_local", "loopback_external_opt_in", "remote_external_opt_in"}
)


CONNECTOR_OUTCOMES_NOTE = "connector_outcomes lists only connectors with problems"


_OBSERVE_OUTCOME_KINDS = {
    "connector_failed": "failed",
    "connector_timed_out": "timed_out",
    "connector_issue": "issues",
}


def _connector_outcomes(stages: Any) -> dict[str, str]:
    """Per-connector outcome from the Observe stage's trace codes (`observe.<kind>:<name>`), the
    only per-connector result the record holds. A connector with a code is reported as failed, timed
    out or issues. A connector that is healthy has no code, so it is NOT listed: the trace records
    problems, not successes. Names that were not a fixed qualifier appear as `unnamed_<n>`."""
    outcomes: dict[str, str] = {}
    for stage in stages:
        if stage.name.value != "observe":
            continue
        for code in stage.issues:
            kind, _, qualifier = code.partition(":")
            kind = kind.split(".", 1)[-1]
            if kind in _OBSERVE_OUTCOME_KINDS and qualifier:
                outcomes.setdefault(qualifier, _OBSERVE_OUTCOME_KINDS[kind])
    return dict(sorted(outcomes.items()))


def _fixed_codes(values: tuple[str, ...]) -> list[str]:
    """Each issue string reduced to its leading code token. Free text after the code is dropped."""
    return sorted({fixed_label(v) for v in values})


@dataclass
class _LiveParts:
    connectors: list[Connector]
    engine: Any
    provider: Any
    egress_basis: str = "unknown"


def build_live_state(settings: Settings) -> Any:
    """Connectors, engine and provider only: the same getters and engine construction as
    `api.state.build_default_state`, without its repository, authenticator or pool settings, so no
    database is ever constructed (F2). Mirrors state.py's connector list; PHN is never built."""
    from divesafe.api.state import build_engine, build_provider
    from divesafe.data import (
        MET_API_HOSTS,
        MET_API_QUOTA,
        CachingRateLimitedGetter,
        DataGovMyForecastConnector,
        DataGovMyWarningConnector,
        MetApiMarineConnector,
        OpenMeteoMarineConnector,
        OpenMeteoWindConnector,
        UrllibJsonGetter,
    )

    getter = CachingRateLimitedGetter(UrllibJsonGetter())
    connectors: list[Connector] = [
        OpenMeteoMarineConnector(getter),
        OpenMeteoWindConnector(getter),
        DataGovMyForecastConnector(getter),
        DataGovMyWarningConnector(getter),
    ]
    if settings.met_api_token is not None:
        met_getter = CachingRateLimitedGetter(
            UrllibJsonGetter(
                allowed_hosts=MET_API_HOSTS,
                auth_header=(
                    "Authorization",
                    f"METToken {settings.met_api_token.get_secret_value()}",
                ),
            ),
            min_interval_seconds={"api.met.gov.my": 0.0},
            quotas=(MET_API_QUOTA,),
        )
        connectors.append(MetApiMarineConnector(met_getter))
    provider = build_provider(settings)
    basis = egress_basis_label(provider, settings) if provider is not None else "none"
    return _LiveParts(
        connectors=connectors,
        engine=build_engine(settings),
        provider=provider,
        egress_basis=basis,
    )


def _real_timestamp() -> float:
    """The real UTC time as a POSIX timestamp. A float, so a non-finite value can be seen."""
    return datetime.now(UTC).timestamp()


def live_clock(source: Callable[[], float] = _real_timestamp) -> Callable[[], datetime]:
    """Real UTC, checked on every reading (R1). The source is the real clock by default; a test may
    inject one, so a real NaN or a backwards value can be fed through this same guard. A NaN, a
    non-finite value, or a reading earlier than the one before it refuses the run with a fixed
    message, and nothing is written."""
    last: list[float] = []

    def read() -> datetime:
        stamp = source()
        if not math.isfinite(stamp):
            raise CaptureRefused("refused: the clock returned a non-finite value")
        if last and stamp < last[-1]:
            raise CaptureRefused("refused: the clock went backwards")
        last.append(stamp)
        return datetime.fromtimestamp(stamp, UTC)

    return read


def run_live(environ: Mapping[str, str]) -> Path:
    """The live run. Rechecks every guard first (F4), then builds connectors, engine and provider
    only, runs one assessment, and writes one scrubbed file."""
    directory = require_live(True, environ)
    check_new_output(directory / "item6_capture.json")
    settings = Settings(_env_file=None)
    check_settings_match_guard()
    parts = build_live_state(settings)
    if parts.engine is None:
        raise CaptureRefused(
            "refused: no rules engine is configured (DIVESAFE_EVIDENCE_MAX_AGE_MINUTES)"
        )
    if parts.provider is None:
        raise CaptureRefused("refused: no provider is configured (DIVESAFE_LLM_PROVIDER)")
    recorder = Recorder()
    summary = asyncio.run(
        run_live_assessment(
            state=parts,
            recorder=recorder,
            clock=live_clock(),
            sleep=asyncio.sleep,
        )
    )
    document = assemble_document(recorder, summary)
    return write_capture(directory, "item6_capture.json", document, settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="make the live run (also needs env)")
    args = parser.parse_args(argv)
    try:
        if not args.live:
            print(json.dumps(run_dry(Settings(_env_file=None)), indent=1))
            return 0
        path = run_live(dict(os.environ))
        print(f"capture written: {path.name}")
        return 0
    except CaptureRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001  (F5: any unexpected error stops the run, fixed text)
        # The class name only: no message, no traceback, which can carry a URL or a key.
        print(f"capture stopped: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
