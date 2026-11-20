"""Offline tests of ADR 0027 §2's dataset sender (ADR 0030 decision 3), with no network and no real
`langfuse`: ADR 0030 Testing, "Refusals" and "Partial run" (delivery confirmation, the case limit,
the run name, the environment and `git` checks, the separate refusals), ADR 0030 Known gaps (the
partial-run limits and the import-origin refusal) and ADR 0027 §2 Testing. The logger, handler and
warnings tests are in `test_langfuse_sender_logging_offline.py`.

The rig (`langfuse_rig.py`) loads a stub SDK through a `sys.meta_path` finder with a real spec,
never placing it in `sys.modules`, and records each stub module it actually executes, so "never
imported" is observable. The version read and the distribution lookup are stubbed through the
sender's seams; neither check is loosened. Langfuse's API is an `httpx.MockTransport` behind the
sender's real `httpx_client`, and the OTLP endpoint a `requests` adapter mounted on the sender's
real session, so both response hooks run.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import tomllib
import types
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace as trace_api
from tests.agent.scripted_llm import fake, scripted
from tests.evaluation import adversarial, harness, langfuse_checks, langfuse_sender
from tests.evaluation.code_scores import CODE_SCORE_NAMES, REFUSED
from tests.evaluation.langfuse_checks import (
    EVAL_SILENCED_LOGGERS,
    LANGFUSE_VERIFIED_VERSION,
    MEDIA_VARIABLE,
    UNSET_DIVESAFE_VARIABLES,
    SenderRefused,
)
from tests.evaluation.langfuse_rig import (
    COMMIT,
    MARKER,
    RUN_UUID,
    SERVER_VERSION,
    STARTED,
    Rig,
    _assert_media_variable_removed,
    _git_stub,
    make_rig,
)
from tests.evaluation.langfuse_sender import (
    COMPLETION_SCORE_NAME,
    LOCAL_FILE_NAME,
)

from divesafe.domain import AssessmentRecord
from divesafe.models import FakeProvider

pytestmark = pytest.mark.safety


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    yield from make_rig(tmp_path, monkeypatch)


# --- the happy path ---------------------------------------------------------------------------


def test_a_full_run_sends_every_case_and_the_completion_marker_last(rig: Rig) -> None:
    result = rig.send()

    count = len(harness.SCENARIOS)
    assert result.complete
    assert (result.expected_cases, result.confirmed_cases) == (count, count)
    assert result.run_name == f"eval-{COMMIT}-20261119T083000000000Z-{RUN_UUID}"
    assert rig.finder.executed[0] == "langfuse"
    # One judge call per case, each before that case's span (never while a span is open).
    assert rig.judge_calls == [s.name for s in harness.SCENARIOS]
    events = [e for e in rig.control.events if e in ("judge", "span_started")]
    assert events == ["judge", "span_started"] * count
    assert "queued_create_score" not in rig.control.events
    assert rig.control.events[-1] == "shutdown"
    # The client was built with the sender's own provider, exporter, client and fixed settings.
    built = rig.control.built[0].kwargs
    assert built["base_url"] == "http://127.0.0.1:3000"
    assert built["debug"] is False
    assert built["release"] == COMMIT
    # Every request went to the one origin; one OTLP export per case.
    assert {host for _, host, _ in rig.server.requests_seen} == {"127.0.0.1"}
    assert rig.adapter.urls == ["http://127.0.0.1:3000/api/public/otel/v1/traces"] * count
    # Per case: code scores, then judge scores; the completion marker is the last batch.
    batches = rig.server.score_batches
    assert len(batches) == 2 * count + 1
    for index in range(count):
        code, judged = batches[2 * index], batches[2 * index + 1]
        assert [s["name"] for s in code] == list(CODE_SCORE_NAMES)
        assert [s["name"] for s in judged] == ["judge.example_flag"]
        assert {s["observation_id"] for s in code} == {s["observation_id"] for s in judged}
    marker = batches[-1]
    assert [s["name"] for s in marker] == [COMPLETION_SCORE_NAME]
    assert set(marker[0]["metadata"]) == {"expected_cases", "confirmed_cases", "commit"}
    # The local file's last entry is the same completion marker.
    written = json.loads(result.output_file.read_text())
    assert result.output_file.name == LOCAL_FILE_NAME
    assert written["run_name"] == result.run_name
    last = written["entries"][-1]["completion_marker"]
    assert (last["expected_cases"], last["confirmed_cases"]) == (count, count)
    assert last["dataset_run_id"] == marker[0]["dataset_run_id"]
    _assert_media_variable_removed()


def test_no_prompt_reply_or_confidence_reaches_the_local_file(rig: Rig) -> None:
    result = rig.send()

    text = result.output_file.read_text()
    assert "confidence" not in text
    assert "content" not in text


# --- the case limit ---------------------------------------------------------------------------


def test_a_limited_run_sends_the_first_case_only_with_no_judge_call_and_no_marker(rig: Rig) -> None:
    result = rig.send(judge=None, case_limit=1)

    assert (result.expected_cases, result.confirmed_cases) == (len(harness.SCENARIOS), 1)
    assert not result.complete
    assert not rig.judge_calls
    names = [s["name"] for batch in rig.server.score_batches for s in batch]
    assert names == list(CODE_SCORE_NAMES)
    entries = json.loads(result.output_file.read_text())["entries"]
    assert [e["case"] for e in entries] == [harness.SCENARIOS[0].name]
    assert not any("completion_marker" in e for e in entries)


def test_a_limited_run_ignores_a_given_judge(rig: Rig) -> None:
    rig.send(case_limit=1)

    assert not rig.judge_calls


@pytest.mark.parametrize("limit", [True, 0, 2, -1, 1.0, "1"])
def test_a_case_limit_other_than_exactly_one_refuses_before_anything(
    rig: Rig, limit: object
) -> None:
    with pytest.raises(SenderRefused) as refused:
        rig.send(case_limit=limit)

    assert refused.value.label == "case_limit"
    assert rig.nothing_happened()


def test_a_full_run_without_a_judge_refuses(rig: Rig) -> None:
    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None)

    assert refused.value.label == "judge_missing"
    assert rig.nothing_happened()


# --- start-up refusals, before the SDK is imported --------------------------------------------


def test_cases_off_the_allow_list_refuse_even_when_equal(rig: Rig) -> None:
    with pytest.raises(SenderRefused) as refused:
        rig.send(cases=list(harness.SCENARIOS))

    assert refused.value.label == "cases_not_allow_listed"
    assert rig.nothing_happened()


def test_the_adversarial_set_refuses_until_its_runner_is_built(rig: Rig) -> None:
    with pytest.raises(SenderRefused) as refused:
        rig.send(cases=adversarial.CASES)

    assert refused.value.label == "case_set_runner_not_built"
    assert rig.nothing_happened()


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_langfuse_debug_refuses_before_the_import(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LANGFUSE_DEBUG", value)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "debug"
    assert rig.nothing_happened()


@pytest.mark.parametrize("value", ["true", "false"])
def test_an_operator_media_variable_refuses_and_is_left_alone(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:

    monkeypatch.setenv(MEDIA_VARIABLE, value)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "sdk_environment"
    assert os.environ[MEDIA_VARIABLE] == value
    assert rig.nothing_happened()


def test_a_different_installed_version_refuses_before_the_import(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert LANGFUSE_VERIFIED_VERSION == "5.2.0"
    monkeypatch.setattr(langfuse_checks, "read_installed_version", lambda: "5.2.1")

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "langfuse_version"
    assert rig.nothing_happened()


def test_no_installed_version_refuses_and_never_skips(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> str:
        raise importlib.metadata.PackageNotFoundError("langfuse")

    monkeypatch.setattr(langfuse_checks, "read_installed_version", missing)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "langfuse_not_installed"
    assert rig.nothing_happened()


def test_an_import_origin_other_than_the_distribution_refuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(langfuse_checks, "distribution_init_file", lambda: tmp_path / "other.py")

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "import_origin"
    assert rig.nothing_happened()


def test_langfuse_already_imported_refuses(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "langfuse", types.ModuleType("langfuse"))

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "already_imported"
    assert rig.nothing_happened()


def test_a_dirty_worktree_refuses_the_live_check(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langfuse_checks, "run_git", _git_stub(COMMIT, "?? probe.py\n"))

    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None, case_limit=1)

    assert refused.value.label == "git_worktree"
    assert rig.nothing_happened()


def test_a_missing_model_host_refuses(rig: Rig) -> None:
    with pytest.raises(SenderRefused) as refused:
        rig.send(model_host=None)

    assert refused.value.label == "model_host_missing"
    assert rig.nothing_happened()


# --- failures partway -------------------------------------------------------------------------


def test_a_score_reply_of_207_fails_the_run_with_a_fixed_label_and_no_marker(rig: Rig) -> None:
    rig.server.score_status = 207

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    assert refused.value.__context__ is None
    assert refused.value.__cause__ is None
    # The first case's judge call happened; no later one follows the failed send.
    assert rig.judge_calls == [harness.SCENARIOS[0].name]
    assert len(rig.server.score_batches) == 1
    assert "shutdown" not in rig.control.events
    assert not rig.output_dir.exists() or not any(rig.output_dir.iterdir())
    _assert_media_variable_removed()


def test_a_dataset_reply_differing_only_in_status_refuses_before_any_case(rig: Rig) -> None:
    rig.server.dataset_status = 201

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    assert not rig.judge_calls
    assert not rig.server.score_batches


def test_an_otlp_reply_other_than_200_fails_the_case(rig: Rig) -> None:
    rig.adapter.status = 302

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    assert rig.judge_calls == [harness.SCENARIOS[0].name]
    assert not rig.output_dir.exists() or not any(rig.output_dir.iterdir())


def test_a_live_factory_returning_none_refuses_the_run(rig: Rig) -> None:
    providers: list[FakeProvider | None] = [fake(), None]

    with pytest.raises(SenderRefused) as refused:
        rig.send(provider_factory=lambda: providers.pop(0) if providers else None)

    assert refused.value.label == "provider_missing"
    assert rig.judge_calls == [harness.SCENARIOS[0].name]
    assert COMPLETION_SCORE_NAME not in {
        s["name"] for batch in rig.server.score_batches for s in batch
    }


# --- shared observers -------------------------------------------------------------------------


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every log line that reaches a handler other than a `NullHandler` (`NullHandler.handle` is
    its own no-op; every other handler, pytest's included, runs `logging.Handler.handle`), with
    the root and every listed logger at DEBUG, so nothing is filtered out by level."""
    lines: list[str] = []
    original = logging.Handler.handle
    formatter = logging.Formatter()

    def handle(self: logging.Handler, record: logging.LogRecord) -> Any:
        text = record.getMessage()
        if record.exc_info:
            text += formatter.formatException(record.exc_info)
        lines.append(text)
        return original(self, record)

    monkeypatch.setattr(logging.Handler, "handle", handle)
    loggers = [logging.getLogger(), *(logging.getLogger(n) for n in EVAL_SILENCED_LOGGERS)]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def test_the_log_observer_sees_a_line_that_reaches_a_real_handler(
    emitted: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("divesafe.observer_probe").warning("probe %s", MARKER)

    assert any(MARKER in line for line in emitted)


def _names(batch: list[dict[str, Any]]) -> list[str]:
    return [s["name"] for s in batch]


def _output_files(rig: Rig) -> list[Path]:
    if not rig.output_dir.exists():
        return []
    return [p for p in rig.output_dir.rglob("*") if p.is_file()]


def _assert_no_marker_anywhere(rig: Rig) -> None:
    """No completion marker in Langfuse or in any file in the output directory."""
    sent = {s["name"] for batch in rig.server.score_batches for s in batch}
    assert COMPLETION_SCORE_NAME not in sent
    for path in _output_files(rig):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "completion_marker" not in text
        assert COMPLETION_SCORE_NAME not in text


def _assert_code_before_judge(rig: Rig) -> None:
    """No case holds judge scores without its code scores, sent before them."""
    coded: set[str] = set()
    for batch in rig.server.score_batches:
        ids = {s["observation_id"] for s in batch if "observation_id" in s}
        if batch and batch[0]["name"].startswith("code."):
            coded |= ids
        elif batch and batch[0]["name"].startswith("judge."):
            assert ids and ids <= coded


def _code_batches(rig: Rig) -> list[list[dict[str, Any]]]:
    return [b for b in rig.server.score_batches if _names(b) == list(CODE_SCORE_NAMES)]


def _assert_stopped(
    rig: Rig,
    emitted: list[str],
    capsys: pytest.CaptureFixture[str],
    *,
    judged: int,
    coded: int,
) -> None:
    """The partial-run limits (ADR 0030 Known gaps): no judge call after the failure, the marker
    of the send error nowhere, no completion marker, code before judge, one thing at a time."""
    names = [s.name for s in harness.SCENARIOS]
    assert rig.judge_calls == names[:judged]
    assert len(_code_batches(rig)) == coded
    _assert_code_before_judge(rig)
    _assert_no_marker_anywhere(rig)
    for path in _output_files(rig):
        assert MARKER not in path.read_text(encoding="utf-8", errors="replace")
    assert not any(MARKER in line for line in emitted)
    out = capsys.readouterr()
    assert MARKER not in out.out and MARKER not in out.err
    assert "shutdown" not in rig.control.events
    assert rig.server.max_in_progress == 1
    _assert_media_variable_removed()


def _patch_run_record(
    monkeypatch: pytest.MonkeyPatch,
    at: int,
    *,
    update: dict[str, Any] | None = None,
    **changes: Any,
) -> None:
    """The case at index `at` runs with changed egress arguments, or its stored record is replaced
    by a copy with `update` (a form validation would refuse, set without validation)."""
    real = harness.run_record
    calls: list[str] = []

    def run(
        scenario: harness.Scenario, provider: Any, *, egress_basis: Any, model_host: Any
    ) -> AssessmentRecord:
        index = len(calls)
        calls.append(scenario.name)
        arguments = {"egress_basis": egress_basis, "model_host": model_host}
        if index == at:
            arguments.update(changes)
        record = real(scenario, provider, **arguments)
        if index == at and update is not None:
            record = record.model_copy(update=update(record) if callable(update) else update)
        return record

    monkeypatch.setattr(harness, "run_record", run)


# --- partial run (ADR 0030 Testing, "Partial run"; Known gaps, the partial-run limits) ----------


class _Stop(BaseException):
    """A `BaseException` that is not an `Exception` (as `KeyboardInterrupt` is)."""


STOP_AT = 2


def test_a_base_exception_from_the_judge_stops_the_run_partway(
    rig: Rig, emitted: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rig.judge_raises[STOP_AT] = _Stop(MARKER)

    with pytest.raises(_Stop):
        rig.send()

    _assert_stopped(rig, emitted, capsys, judged=STOP_AT + 1, coded=STOP_AT)


def test_a_refused_host_for_a_later_case_stops_the_run_before_its_judge_call(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    emitted: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The record itself refuses a declared-local basis with a non-local host.
    _patch_run_record(monkeypatch, STOP_AT, model_host="example.org")

    with pytest.raises(Exception) as stopped:
        rig.send()

    assert "model_host" in str(stopped.value)
    _assert_stopped(rig, emitted, capsys, judged=STOP_AT, coded=STOP_AT)
    assert len(rig.server.items_seen) == STOP_AT


def test_a_record_failing_the_post_run_check_stops_the_run_before_anything_for_it_is_sent(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    emitted: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An unrecognised basis is stored as `none`, so the sender's real post-run check fails.
    _patch_run_record(monkeypatch, STOP_AT, egress_basis="not-a-basis")

    with pytest.raises(harness.RecordEgressMismatch):
        rig.send()

    _assert_stopped(rig, emitted, capsys, judged=STOP_AT, coded=STOP_AT)
    assert len(rig.server.items_seen) == STOP_AT
    assert len(rig.adapter.urls) == STOP_AT


def test_a_factory_returning_none_for_a_later_case_stops_the_run(
    rig: Rig, emitted: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    providers: list[FakeProvider | None] = [fake() for _ in range(STOP_AT)]

    with pytest.raises(SenderRefused) as refused:
        rig.send(provider_factory=lambda: providers.pop(0) if providers else None)

    assert refused.value.label == "provider_missing"
    _assert_stopped(rig, emitted, capsys, judged=STOP_AT, coded=STOP_AT)


def test_a_dataset_item_send_raised_by_the_sink_for_a_later_case_stops_the_run(
    rig: Rig, emitted: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rig.server.fail_item_index = STOP_AT

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    _assert_stopped(rig, emitted, capsys, judged=STOP_AT, coded=STOP_AT)


def test_a_score_send_raised_by_the_sink_for_a_later_case_stops_the_run(
    rig: Rig, emitted: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Score batches: per case code then judge, so batch 2 * STOP_AT is that case's code batch.
    rig.server.fail_score_batch = (2 * STOP_AT, 500, MARKER)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    # The failed case was judged (before its span) but holds no judge scores.
    _assert_stopped(rig, emitted, capsys, judged=STOP_AT + 1, coded=STOP_AT + 1)
    assert len(rig.server.score_batches) == 2 * STOP_AT + 1


def test_a_send_failing_only_at_flush_for_a_later_case_stops_the_run(
    rig: Rig, emitted: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The case's span export is refused with the marker as its reason; the exporter itself
    # reports it only at flush (and logs it to a silenced logger).
    rig.adapter.fail_at = {STOP_AT: (400, MARKER)}

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    _assert_stopped(rig, emitted, capsys, judged=STOP_AT + 1, coded=STOP_AT + 1)
    assert len(rig.adapter.urls) == STOP_AT + 1


def test_a_failed_completion_marker_send_writes_no_local_marker(
    rig: Rig, emitted: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    count = len(harness.SCENARIOS)
    rig.server.fail_score_batch = (2 * count, 500, MARKER)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    # The marker was offered to Langfuse and refused; nothing local claims completion.
    assert _names(rig.server.score_batches[-1]) == [COMPLETION_SCORE_NAME]
    assert _output_files(rig) == []
    assert "queued_create_score" not in rig.control.events
    assert rig.judge_calls == [s.name for s in harness.SCENARIOS]
    assert not any(MARKER in line for line in emitted)
    _assert_media_variable_removed()


def test_at_most_one_judge_call_or_send_is_in_progress(rig: Rig) -> None:
    rig.judge_seconds = 0.02

    result = rig.send()

    assert result.complete
    assert rig.server.max_in_progress == 1
    _assert_code_before_judge(rig)


# --- delivery confirmation (ADR 0030 Testing, "Partial run", decision 3) ----------------------


def _assert_failed_before_next_judge(rig: Rig, refused: SenderRefused, judged: int) -> None:
    assert refused.label == "send_failed"
    assert refused.__context__ is None and refused.__cause__ is None
    assert rig.judge_calls == [s.name for s in harness.SCENARIOS][:judged]
    _assert_no_marker_anywhere(rig)
    assert _output_files(rig) == []
    assert "shutdown" not in rig.control.events
    _assert_media_variable_removed()


def _span_never_seen(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.control.never_end = True


def _foreign_span_in_batch(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.control.extra_span = True


def _wrapped_export_raises(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    def boom(self: Any, spans: Any) -> Any:
        raise RuntimeError(MARKER)

    monkeypatch.setattr(OTLPSpanExporter, "export", boom)


def _last_status_302(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.adapter.status = 302


def _exported_twice(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.control.double_export = True


def _score_207(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.server.score_status = 207


def _score_200_right_type(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.server.score_status = 200


def _score_202_other_type(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.server.score_body = {"results": []}


def _score_answered_twice(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.control.double = {"scores"}


def _score_other_path(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    other = "/api/public/x/scores"
    rig.control.path_override = {"scores": other}
    rig.server.aliases = {other: "/api/public/scores"}  # answered as the expected path would be


# Each fails the first case after its judge call and before the next one.
CASE_FAILURES = {
    "span_never_seen": _span_never_seen,
    "foreign_span_in_batch": _foreign_span_in_batch,
    "wrapped_export_raises": _wrapped_export_raises,
    "last_status_302": _last_status_302,
    "span_exported_twice": _exported_twice,
    "score_207": _score_207,
    "score_200_right_type": _score_200_right_type,
    "score_202_other_type": _score_202_other_type,
    "score_answered_twice": _score_answered_twice,
    "score_other_path": _score_other_path,
}


@pytest.mark.parametrize("failure", sorted(CASE_FAILURES))
def test_an_unconfirmed_span_or_score_fails_the_case_before_the_next_judge_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    CASE_FAILURES[failure](rig, monkeypatch)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_failed_before_next_judge(rig, refused.value, judged=1)


def _differs(key: str, how: str, rig: Rig) -> None:
    path = {"dataset": "/api/public/v2/datasets", "dataset_item": "/api/public/dataset-items"}[key]
    if how == "status":
        if key == "dataset":
            rig.server.dataset_status = 201
        else:
            rig.server.item_status = 201
    elif how == "path":
        other = path.replace("/api/public/", "/api/public/x/")
        rig.control.path_override = {key: other}
        rig.server.aliases = {other: path}  # answered exactly as the expected path would be
    elif how == "type":
        rig.control.type_override = {key: "Unexpected"}
    elif how == "twice":
        rig.control.double = {key}


@pytest.mark.parametrize("key", ["dataset", "dataset_item"])
@pytest.mark.parametrize("how", ["status", "path", "type", "twice"])
def test_a_dataset_or_item_reply_differing_in_one_respect_fails_before_any_judge_call(
    rig: Rig, key: str, how: str
) -> None:
    _differs(key, how, rig)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_failed_before_next_judge(rig, refused.value, judged=0)
    assert not rig.server.score_batches
    assert not rig.adapter.urls


@pytest.mark.parametrize(
    "base", ["http://example.org:3000", "http://localhost:3000", "http://127.0.0.1:3001"]
)
def test_an_api_request_to_another_origin_is_refused_before_it_is_sent(rig: Rig, base: str) -> None:
    rig.control.api_base = base

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_failed_before_next_judge(rig, refused.value, judged=0)
    assert rig.server.requests_seen == []


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org:3000/api/public/otel/v1/traces",
        "http://localhost:3000/api/public/otel/v1/traces",
        "http://127.0.0.1:3001/api/public/otel/v1/traces",
        "https://127.0.0.1:3000/api/public/otel/v1/traces",
    ],
)
def test_the_otlp_session_refuses_another_origin_before_any_connection(url: str) -> None:
    from tests.evaluation.langfuse_rig import OtlpAdapter

    adapter = OtlpAdapter()
    session = langfuse_sender.OtlpSession(adapter)
    session.mount("https://", adapter)  # so only the origin check can stop the request

    with pytest.raises(langfuse_sender.OriginRefused):
        session.post(url, data=b"span")

    assert adapter.urls == []
    session.post("http://127.0.0.1:3000/api/public/otel/v1/traces", data=b"span")
    assert adapter.urls == ["http://127.0.0.1:3000/api/public/otel/v1/traces"]


def test_the_positive_run_uses_its_own_provider_media_off_and_no_queued_score(rig: Rig) -> None:
    """ADR 0030 Testing: the stub client sets the global provider only when built without
    `tracer_provider=`, reports media upload on unless the variable is exactly `false` at its
    build, and records any use of its queued `create_score`."""
    from opentelemetry.sdk.trace import TracerProvider

    seen: list[str | None] = []
    rig.control.on_build.append(lambda: seen.append(os.environ.get(MEDIA_VARIABLE)))

    result = rig.send()

    assert result.complete
    built = rig.control.built[0]
    assert isinstance(built.kwargs["tracer_provider"], TracerProvider)
    assert trace_api._TRACER_PROVIDER is None
    assert seen == ["false"]
    assert built._resources._media_upload_enabled is False
    assert "queued_create_score" not in rig.control.events
    assert _names(rig.server.score_batches[-1]) == [COMPLETION_SCORE_NAME]
    entries = json.loads(result.output_file.read_text())["entries"]
    assert "completion_marker" in entries[-1]
    _assert_media_variable_removed()


# --- the case limit and the run name ----------------------------------------------------------


def test_two_runs_in_one_process_get_exactly_the_built_names(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    second = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
    monkeypatch.setattr(uuid, "uuid4", iter([first, second]).__next__)
    stamp = STARTED.strftime("%Y%m%dT%H%M%S%fZ")
    expected = [f"eval-{COMMIT}-{stamp}-{first}", f"eval-{COMMIT}-{stamp}-{second}"]

    results = []
    exports: list[list[bytes]] = []
    for index in range(2):
        before = len(rig.adapter.bodies)
        results.append(rig.send(output_dir=tmp_path / f"run{index}"))
        exports.append(rig.adapter.bodies[before:])
        # A new process imports the SDK afresh; the sender refuses an SDK already imported.
        for name in [n for n in sys.modules if n == "langfuse" or n.startswith("langfuse.")]:
            del sys.modules[name]

    assert [r.run_name for r in results] == expected
    for index, result in enumerate(results):
        name, other = expected[index], expected[1 - index]
        assert json.loads(result.output_file.read_text())["run_name"] == name
        assert exports[index] and all(name.encode() in body for body in exports[index])
        assert not any(other.encode() in body for body in exports[index])
        assert set(rig.control.built[index].kwargs).isdisjoint({"run_name", "name"})


def test_the_entry_point_takes_no_run_name() -> None:
    assert list(inspect.signature(langfuse_sender.run_send).parameters) == [
        "cases",
        "provider_factory",
        "judge",
        "egress_basis",
        "model_host",
        "output_dir",
        "case_limit",
        "client_factory",
        "httpx_transport",
        "otlp_adapter",
    ]


# --- AST checks of the sender's modules -------------------------------------------------------

_SENDER_MODULES = (
    Path(langfuse_sender.__file__),
    Path(langfuse_checks.__file__),
    Path(langfuse_sender.__file__).with_name("code_scores.py"),
)
# Adding a function here is a reviewed change to this test (ADR 0030 Testing).
ENVIRONMENT_FUNCTIONS = frozenset(
    {
        "refuse_sdk_environment",
        "refuse_divesafe_variables",
        "eval_secrets_dir",
        "set_media_variable",
        "remove_media_variable",
        "check_post_build_environment",
        "git_environment",
    }
)
_ENVIRONMENT_NAMES = frozenset(
    {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv", "argv", "orig_argv"}
)


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str | None]:
    found: dict[ast.AST, str | None] = {}

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else function
            )
            found[child] = inner
            visit(child, inner)

    visit(tree, None)
    return found


def environment_uses(source: str) -> list[tuple[int, str | None]]:
    """Each use of the process environment or the command line (however imported), with the
    function it is in (None at module level)."""
    tree = ast.parse(source)
    where = _enclosing_functions(tree)
    # Names bound by `from os import environ as e` and the like (a parameter named `argv` is not
    # the command line).
    imported = {
        a.asname or a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in ("os", "sys", "posix", "nt")
        for a in node.names
        if a.name in _ENVIRONMENT_NAMES
    }
    uses: list[tuple[int, str | None]] = []
    for node in ast.walk(tree):
        hit = (
            (isinstance(node, ast.Attribute) and node.attr in _ENVIRONMENT_NAMES)
            or (isinstance(node, ast.Name) and node.id in imported)
            or (
                isinstance(node, ast.ImportFrom)
                and any(a.name in _ENVIRONMENT_NAMES for a in node.names)
            )
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in _ENVIRONMENT_NAMES
            )
        )
        if hit:
            uses.append((getattr(node, "lineno", 0), where.get(node)))
    return uses


def test_every_environment_read_is_in_the_fixed_list_of_functions() -> None:
    seen: set[str | None] = set()
    for path in _SENDER_MODULES:
        for line, function in environment_uses(path.read_text(encoding="utf-8")):
            assert function in ENVIRONMENT_FUNCTIONS, f"{path.name}:{line} in {function}"
            seen.add(function)
    # The checks' module really reads the environment where it says it does.
    assert seen == ENVIRONMENT_FUNCTIONS


@pytest.mark.parametrize(
    "snippet",
    [
        "import os\ndef f():\n    return os.environ.get('X')\n",
        "from os import environ\ndef f():\n    return environ['X']\n",
        "from os import getenv as g\ndef f():\n    return g('X')\n",
        "import sys\ndef f():\n    return sys.argv[1]\n",
        "import os\nX = os.getenv('X')\n",
        "import os\ndef f():\n    return getattr(os, 'environ')\n",
    ],
)
def test_the_environment_scan_sees_each_form(snippet: str) -> None:
    assert any(function not in ENVIRONMENT_FUNCTIONS for _, function in environment_uses(snippet))


def test_the_run_name_is_assigned_once_from_the_builder_with_the_git_commit() -> None:
    tree = ast.parse(Path(langfuse_sender.__file__).read_text(encoding="utf-8"))
    assigned: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.value is None:
            continue  # a dataclass field declaration, not an assignment
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            if "run_name" in names:
                assigned.append(node)
    assert len(assigned) == 1
    (only,) = assigned
    assert isinstance(only, ast.Assign) and len(only.targets) == 1
    call = only.value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "build_run_name"
    builders = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name | ast.Attribute)
        and getattr(n.func, "id", getattr(n.func, "attr", None)) == "build_run_name"
    ]
    assert builders == [call]
    # The commit passed to the builder is the `git rev-parse HEAD` read, assigned once.
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == "commit"
    run_send = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_send"
    )
    commits = [
        n
        for n in ast.walk(run_send)
        if isinstance(n, ast.Assign | ast.AnnAssign)
        and any(
            isinstance(t, ast.Name) and t.id == "commit"
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
        )
    ]
    assert len(commits) == 1
    value = commits[0].value
    assert isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
    assert value.func.id == "git_commit" and not value.args and not value.keywords
    checks = ast.parse(Path(langfuse_checks.__file__).read_text(encoding="utf-8"))
    git_commit = next(
        n for n in ast.walk(checks) if isinstance(n, ast.FunctionDef) and n.name == "git_commit"
    )
    assert "rev-parse" in ast.unparse(git_commit) and "HEAD" in ast.unparse(git_commit)


def _imports(path: Path) -> list[tuple[str, int, bool]]:
    """(module, line, at top level) for each import in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = set(tree.body)
    found: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno, node in top) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.lineno, node in top))
            # `from divesafe.api import state` imports `divesafe.api.state`.
            found += [
                (f"{node.module or ''}.{a.name}", node.lineno, node in top) for a in node.names
            ]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
            and getattr(node.func, "attr", getattr(node.func, "id", "")) == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            found.append((str(node.args[0].value), node.lineno, False))
    return found


def _is_langfuse(module: str) -> bool:
    return module == "langfuse" or module.startswith("langfuse.")


def test_only_the_senders_set_up_imports_langfuse() -> None:
    sender, checks, scores = _SENDER_MODULES
    assert not any(_is_langfuse(m) for m, _, _ in _imports(checks))
    assert not any(_is_langfuse(m) for m, _, _ in _imports(scores))
    sender_imports = [(m, top) for m, _, top in _imports(sender) if _is_langfuse(m)]
    assert sender_imports and not any(top for _, top in sender_imports)
    tree = ast.parse(sender.read_text(encoding="utf-8"))
    set_up = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_set_up"
    )
    inside = [m for m, _, _ in _imports_in(set_up) if _is_langfuse(m)]
    assert len(inside) == len(sender_imports)


def _imports_in(node: ast.AST) -> list[tuple[str, int, bool]]:
    found: list[tuple[str, int, bool]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            found += [(a.name, child.lineno, False) for a in child.names]
        elif isinstance(child, ast.ImportFrom):
            found.append((child.module or "", child.lineno, False))
        elif (
            isinstance(child, ast.Call)
            and getattr(child.func, "attr", getattr(child.func, "id", "")) == "import_module"
            and child.args
            and isinstance(child.args[0], ast.Constant)
        ):
            found.append((str(child.args[0].value), child.lineno, False))
    return found


def test_the_sender_modules_never_import_api_state_or_the_item6_capture() -> None:
    for path in _SENDER_MODULES:
        for module, line, _ in _imports(path):
            assert not module.endswith("api.state"), f"{path.name}:{line}"
            assert "capture_item6" not in module, f"{path.name}:{line}"
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s.*\bcapture_item6\b", text, re.MULTILINE)
        assert not re.search(r"^\s*from\s+\S*\bapi\s+import\s.*\bstate\b", text, re.MULTILINE)


@pytest.mark.parametrize(
    "line",
    [
        "from divesafe.api import state",
        "from divesafe.api.state import AppState",
        "import divesafe.api.state",
        "from tests.evaluation import capture_item6",
        "from tests.evaluation.capture_item6 import run",
    ],
)
def test_the_import_scan_sees_each_form(tmp_path: Path, line: str) -> None:
    path = tmp_path / "probe.py"
    path.write_text(f"def f():\n    {line}\n")
    modules = [m for m, _, _ in _imports(path)]
    assert any(m.endswith("api.state") or "capture_item6" in m for m in modules)


def test_a_scripted_refused_reply_with_a_marker_never_reaches_the_sink(rig: Rig) -> None:
    """ADR 0027 §2 Testing: the reply is never kept, so never sent."""
    providers: list[FakeProvider] = []

    def factory() -> FakeProvider:
        providers.append(FakeProvider(f"I refuse to answer in JSON. {MARKER}"))
        return providers[-1]

    result = rig.send(provider_factory=factory)

    assert result.complete
    assert providers and all(p.requests for p in providers)
    assert not any(MARKER.encode() in body for body in rig.adapter.bodies)
    assert MARKER not in json.dumps(rig.server.score_batches)
    assert MARKER not in json.dumps(rig.server.items_seen)
    assert MARKER not in result.output_file.read_text()
    # The prompts were sent (so the span input is not simply empty).
    assert any(b"untrusted" in body.lower() or b"evidence" in body for body in rig.adapter.bodies)


# --- git (ADR 0030 Testing: the live check's provenance) --------------------------------------


class GitRecorder:
    """A `run_git` stand-in recording each command and its environment. The status reply holds an
    untracked file only when `--untracked-files=all` is present."""

    def __init__(self, *, commit: str | None = COMMIT + "\n", untracked: str = "") -> None:
        self.commit = commit
        self.untracked = untracked
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: dict[str, str]) -> str | None:
        self.calls.append((list(argv), dict(env)))
        if "rev-parse" in argv:
            return self.commit
        return self.untracked if "--untracked-files=all" in argv else ""


def _live_check(rig: Rig) -> Any:
    return rig.send(judge=None, case_limit=1)


def test_an_untracked_file_seen_only_with_the_full_flag_refuses_the_live_check(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = GitRecorder(untracked="?? probe.py\n")
    monkeypatch.setattr(langfuse_checks, "run_git", git)

    with pytest.raises(SenderRefused) as refused:
        _live_check(rig)

    assert refused.value.label == "git_worktree"
    assert rig.nothing_happened()
    (status,) = [argv for argv, _ in git.calls if "status" in argv]
    root = str(Path(langfuse_checks.__file__).resolve().parents[2])
    assert status == [
        "git",
        "-C",
        root,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "--no-optional-locks",
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ]


def test_a_failing_git_status_refuses_the_live_check(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: Sequence[str], env: dict[str, str]) -> str | None:
        return COMMIT if "rev-parse" in argv else None

    monkeypatch.setattr(langfuse_checks, "run_git", run)

    with pytest.raises(SenderRefused) as refused:
        _live_check(rig)

    assert refused.value.label == "git_worktree"
    assert rig.nothing_happened()


@pytest.mark.parametrize(
    "out",
    [None, "", COMMIT[:39] + "\n", COMMIT.upper() + "\n", COMMIT + "0\n", "HEAD\n", "x" * 40],
)
def test_a_rev_parse_output_that_is_not_a_full_commit_id_refuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, out: str | None
) -> None:
    monkeypatch.setattr(langfuse_checks, "run_git", GitRecorder(commit=out))

    with pytest.raises(SenderRefused) as refused:
        _live_check(rig)

    assert refused.value.label == "git_commit"
    assert rig.nothing_happened()


def test_git_gets_no_git_variable_and_the_root_from_the_module_path(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    git = GitRecorder()
    monkeypatch.setattr(langfuse_checks, "run_git", git)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))
    monkeypatch.setenv("git_work_tree", str(tmp_path))
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    _live_check(rig)

    root = str(Path(langfuse_checks.__file__).resolve().parents[2])
    assert root == str(Path(__file__).resolve().parents[2])
    kinds = sorted("rev-parse" if "rev-parse" in argv else "status" for argv, _ in git.calls)
    assert kinds == ["rev-parse", "status"]
    for argv, env in git.calls:
        assert argv[:3] == ["git", "-C", root]
        assert str(elsewhere) not in argv
        assert not any(name.upper().startswith("GIT_") for name in env)
        assert env  # the rest of the environment is passed on


def test_a_dirty_worktree_refuses_a_full_run_too(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    git = GitRecorder(untracked="?? probe.py\n")
    monkeypatch.setattr(langfuse_checks, "run_git", git)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "git_worktree"
    assert rig.nothing_happened()
    (status,) = [argv for argv, _ in git.calls if "status" in argv]
    assert status[3:] == list(langfuse_checks.GIT_STATUS_ARGS)


def test_a_full_run_with_a_clean_worktree_reads_the_status_and_the_commit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = GitRecorder()
    monkeypatch.setattr(langfuse_checks, "run_git", git)

    assert rig.send().complete
    kinds = ["rev-parse" if "rev-parse" in argv else "status" for argv, _ in git.calls]
    assert kinds == ["status", "rev-parse"]


# --- separate refusals (ADR 0030 Testing) -----------------------------------------------------


def _assert_refused_after_build(rig: Rig, refused: SenderRefused, label: str) -> None:
    assert refused.label == label
    assert rig.control.built  # refused after the build...
    assert not rig.judge_calls  # ...with no judge call and no send
    assert rig.server.requests_seen == []
    assert rig.adapter.urls == []
    _assert_media_variable_removed()


def test_a_client_setting_a_global_tracer_provider_refuses(rig: Rig) -> None:
    from opentelemetry.sdk.trace import TracerProvider

    rig.control.on_build.append(lambda: trace_api.set_tracer_provider(TracerProvider()))

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "global_tracer_provider")


def test_a_client_built_without_the_senders_provider_sets_the_global_one(rig: Rig) -> None:
    """The stub's own contract, which the mutant "client built without `tracer_provider=`" relies
    on: without it the stub sets its own provider as the global one."""
    from tests.evaluation.langfuse_rig import _write_stub  # noqa: F401  (the same stub package)

    rig.send()
    stub = sys.modules["langfuse"]
    before = trace_api._TRACER_PROVIDER
    client = stub.Langfuse(  # type: ignore[attr-defined]
        span_exporter=rig.control.built[0].kwargs["span_exporter"],
        httpx_client=None,
        base_url="http://127.0.0.1:3000",
    )
    assert before is None and trace_api._TRACER_PROVIDER is client._provider


def test_a_client_reporting_media_upload_on_refuses(rig: Rig) -> None:
    def media_on() -> None:
        rig.control.built[-1]._resources._media_upload_enabled = True

    rig.control.on_build.append(media_on)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "media_upload")


def test_a_client_with_a_media_upload_thread_refuses(rig: Rig) -> None:
    def consumer() -> None:
        rig.control.built[-1]._resources._media_upload_consumers.append(object())

    rig.control.on_build.append(consumer)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_refused_after_build(rig, refused.value, "media_upload")


def test_a_client_setting_the_media_variable_to_true_refuses_even_with_media_reported_off(
    rig: Rig,
) -> None:
    def set_true() -> None:
        os.environ[MEDIA_VARIABLE] = "true"

    rig.control.on_build.append(set_true)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    built = rig.control.built[0]
    assert built._resources._media_upload_enabled is False
    assert built._resources._media_upload_consumers == []
    _assert_refused_after_build(rig, refused.value, "media_upload")


def test_a_client_setting_an_otel_variable_refuses(rig: Rig) -> None:
    def set_otel() -> None:
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://example.org/v1/traces"

    rig.control.on_build.append(set_otel)
    try:
        with pytest.raises(SenderRefused) as refused:
            rig.send()
    finally:
        os.environ.pop("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", None)

    _assert_refused_after_build(rig, refused.value, "sdk_environment")


def test_the_media_variable_is_set_exactly_false_while_the_sdk_is_imported_and_built(
    rig: Rig,
) -> None:
    seen: list[str | None] = []
    rig.control.on_build.append(lambda: seen.append(os.environ.get(MEDIA_VARIABLE)))

    rig.send()

    assert seen == ["false"]
    assert rig.control.built[0]._resources._media_upload_enabled is False


@pytest.mark.parametrize("label", ["refused_after_build", "send_failed", "base_exception"])
def test_the_media_variable_is_removed_after_a_run_that_fails(rig: Rig, label: str) -> None:
    if label == "refused_after_build":
        rig.control.on_build.append(lambda: setattr(rig.control.built[-1], "_resources", None))
        expected: type[BaseException] = SenderRefused
    elif label == "send_failed":
        rig.server.fail_item_index = 1
        expected = SenderRefused
    else:
        rig.judge_raises[1] = _Stop()
        expected = _Stop

    with pytest.raises(expected):
        rig.send()

    _assert_media_variable_removed()


# --- refusals (ADR 0030 Testing, "Refusals"; ADR 0027 §2) -------------------------------------


class RecordingProvider(FakeProvider):
    """An in-memory provider that records each call (the refusal tests expect zero)."""

    made: list[RecordingProvider] = []

    def __init__(self) -> None:
        super().__init__(fake().__dict__["_reply"])
        RecordingProvider.made.append(self)


@pytest.fixture
def recording() -> Iterator[list[RecordingProvider]]:
    RecordingProvider.made = []
    yield RecordingProvider.made


def _assert_refused_before_anything(
    rig: Rig, refused: SenderRefused, label: str, made: list[RecordingProvider]
) -> None:
    assert refused.label == label
    assert refused.__context__ is None and refused.__cause__ is None
    assert rig.nothing_happened()
    assert sum(len(p.requests) for p in made) == 0
    assert not made  # not even built


START_UP_REFUSALS: dict[str, tuple[str, Any]] = {
    "debug_lowercase": ("debug", lambda m, t: m.setenv("langfuse_debug", "1")),
    "debug_true": ("debug", lambda m, t: m.setenv("LANGFUSE_DEBUG", "True")),
    "sdk_variable": ("sdk_environment", lambda m, t: m.setenv("LANGFUSE_HOST", "http://x")),
    "otel_variable": ("sdk_environment", lambda m, t: m.setenv("OTEL_SDK_DISABLED", "true")),
    "proxy_variable": ("sdk_environment", lambda m, t: m.setenv("HTTPS_PROXY", "http://x")),
    "system_proxy": (
        "sdk_environment",
        lambda m, t: m.setattr("urllib.request.getproxies", lambda: {"http": "http://x"}),
    ),
    "media_variable_false": ("sdk_environment", lambda m, t: m.setenv(MEDIA_VARIABLE, "false")),
    **{
        f"unset_{name.lower()}": (
            "divesafe_variable_set",
            (lambda n: lambda m, t: m.setenv(n, ""))(name),
        )
        for name in UNSET_DIVESAFE_VARIABLES
    },
    "already_imported_submodule": (
        "already_imported",
        lambda m, t: m.setitem(sys.modules, "langfuse.api", types.ModuleType("langfuse.api")),
    ),
    "version_other": (
        "langfuse_version",
        lambda m, t: m.setattr(langfuse_checks, "read_installed_version", lambda: "5.3.0"),
    ),
    "origin_elsewhere": (
        "import_origin",
        lambda m, t: m.setattr(langfuse_checks, "distribution_init_file", lambda: t / "x.py"),
    ),
    "secrets_missing": (
        "secrets_dir",
        lambda m, t: m.delenv(langfuse_checks.SECRETS_DIR_VARIABLE),
    ),
    "key_with_colon": (
        "eval_keys",
        lambda m, t: (t / "secrets" / langfuse_checks.PUBLIC_KEY_FILE).write_text("pk:x\n"),
    ),
}


@pytest.mark.parametrize("case", sorted(START_UP_REFUSALS))
def test_each_start_up_refusal_stops_before_any_import_judge_call_or_provider_call(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recording: list[RecordingProvider],
    case: str,
) -> None:
    label, arrange = START_UP_REFUSALS[case]
    arrange(monkeypatch, tmp_path)

    with pytest.raises(SenderRefused) as refused:
        rig.send(provider_factory=RecordingProvider)

    _assert_refused_before_anything(rig, refused.value, label, recording)


@pytest.mark.parametrize("name", UNSET_DIVESAFE_VARIABLES)
def test_each_divesafe_variable_refuses_when_present_even_empty(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, recording: list[RecordingProvider], name: str
) -> None:
    monkeypatch.setenv(name, "")

    with pytest.raises(SenderRefused) as refused:
        rig.send(provider_factory=RecordingProvider)

    _assert_refused_before_anything(rig, refused.value, "divesafe_variable_set", recording)


def test_the_four_divesafe_variables_are_pinned() -> None:
    assert UNSET_DIVESAFE_VARIABLES == (
        "DIVESAFE_TRACING_ENABLED",
        "DIVESAFE_DATABASE_URL",
        "DIVESAFE_ADMIN_DATABASE_URL",
        "DIVESAFE_EVAL_KNOWLEDGE_HOST",
    )


def _shadow(tmp_path: Path) -> tuple[Path, Path]:
    """A shadow `langfuse.py` that records whether it ever ran."""
    directory = tmp_path / "shadow"
    directory.mkdir()
    ran = tmp_path / "shadow_ran"
    (directory / "langfuse.py").write_text(
        f"from pathlib import Path\nPath({str(ran)!r}).write_text('ran')\n"
    )
    return directory, ran


@pytest.mark.parametrize("finder", ["shadow_first", "spec_none", "spec_raises"])
def test_an_import_origin_other_than_the_distribution_never_runs_the_module(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recording: list[RecordingProvider],
    finder: str,
) -> None:
    directory, ran = _shadow(tmp_path)
    if finder == "shadow_first":
        # The stub finder goes; a shadow file on `sys.path` is what `find_spec` returns.
        sys.meta_path.remove(rig.finder)
        monkeypatch.syspath_prepend(str(directory))
    elif finder == "spec_none":
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    else:

        def boom(name: str, *args: Any) -> Any:
            raise ValueError(MARKER)

        monkeypatch.setattr(importlib.util, "find_spec", boom)

    try:
        with pytest.raises(SenderRefused) as refused:
            rig.send(provider_factory=RecordingProvider)
    finally:
        if rig.finder not in sys.meta_path:
            sys.meta_path.insert(0, rig.finder)

    _assert_refused_before_anything(rig, refused.value, "import_origin", recording)
    assert not ran.exists()
    assert "langfuse" not in sys.modules


def test_the_version_and_debug_refusals_never_import_the_stub(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, recording: list[RecordingProvider]
) -> None:
    for arrange, label in (
        (
            lambda: monkeypatch.setattr(langfuse_checks, "read_installed_version", lambda: "5.1.9"),
            "langfuse_version",
        ),
        (lambda: monkeypatch.setenv("LANGFUSE_DEBUG", "true"), "debug"),
    ):
        with monkeypatch.context() as m:  # noqa: F841
            arrange()
            with pytest.raises(SenderRefused) as refused:
                rig.send(provider_factory=RecordingProvider)
            assert refused.value.label == label
            assert rig.finder.executed == []
        monkeypatch.delenv("LANGFUSE_DEBUG", raising=False)
        monkeypatch.setattr(langfuse_checks, "read_installed_version", lambda: "5.2.0")
    assert not recording


def test_the_client_is_built_with_debug_off(rig: Rig) -> None:
    rig.send()

    assert rig.control.built[0].kwargs["debug"] is False


def _with_item(
    record: AssessmentRecord,
    *,
    source: str | None = None,
    knowledge: bool = False,
    kb_id: bool = False,
    withheld: bool = False,
) -> dict[str, Any]:
    from divesafe.domain import DataKind

    item = record.evidence[0]
    changes: dict[str, Any] = {}
    if source is not None:
        changes["source"] = source
    if knowledge:
        changes["data_kind"] = DataKind.KNOWLEDGE
    if kb_id:
        changes["id"] = "kb:doc-1:1:0"
    changed = item.model_copy(update=changes)
    if withheld:
        return {"withheld_evidence": (*record.withheld_evidence, changed)}
    return {"evidence": (changed, *record.evidence[1:])}


def _tide_source(which: str) -> str:
    """The PHN source, or the other refused tide source (ADR 0029's), taken from
    `REFUSED_SOURCES` so this file never names it: only the files the tide-name scan of
    `test_capture_item6_live_offline.py` exempts may. That file pins which source it is."""
    from divesafe.data import phn_etide

    others = sorted(langfuse_checks.REFUSED_SOURCES - {phn_etide.SOURCE})
    assert phn_etide.SOURCE in langfuse_checks.REFUSED_SOURCES and len(others) == 1
    return phn_etide.SOURCE if which == "phn" else others[0]


RECORD_REFUSALS: dict[str, tuple[str, Any]] = {
    "phn_item": ("record_tide_item", lambda r: _with_item(r, source=_tide_source("phn"))),
    "other_tide_item": (
        "record_tide_item",
        lambda r: _with_item(r, source=_tide_source("other")),
    ),
    "other_tide_withheld": (
        "record_tide_item",
        lambda r: _with_item(r, source=_tide_source("other"), withheld=True),
    ),
    "knowledge_item": ("record_knowledge", lambda r: _with_item(r, knowledge=True)),
    "kb_id": ("record_knowledge", lambda r: _with_item(r, kb_id=True)),
    "kb_withheld": ("record_knowledge", lambda r: _with_item(r, kb_id=True, withheld=True)),
    "withheld_knowledge": (
        "record_knowledge",
        lambda r: {
            "withheld_knowledge": (
                __import__("divesafe.domain.models", fromlist=["KnowledgeRef"]).KnowledgeRef(
                    document_id="doc-1", version=1, chunk=1
                ),
            )
        },
    ),
}


@pytest.mark.parametrize("case", sorted(RECORD_REFUSALS))
@pytest.mark.parametrize("at", [0, STOP_AT])
def test_a_never_sent_item_in_a_record_refuses_before_that_cases_judge_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, case: str, at: int
) -> None:
    label, update = RECORD_REFUSALS[case]
    _patch_run_record(monkeypatch, at, update=update)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == label
    assert rig.judge_calls == [s.name for s in harness.SCENARIOS][:at]
    assert len(rig.server.items_seen) == at
    assert len(_code_batches(rig)) == at
    _assert_no_marker_anywhere(rig)


def test_a_supervise_refusal_sends_code_refused_alone_and_no_judge_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.orchestration.supervisor import SupervisorInvariantError

    real = harness.run_record
    calls: list[str] = []

    def run(scenario: harness.Scenario, provider: Any, **egress: Any) -> AssessmentRecord:
        calls.append(scenario.name)
        if len(calls) == 1:
            raise SupervisorInvariantError()
        return real(scenario, provider, **egress)

    monkeypatch.setattr(harness, "run_record", run)

    result = rig.send()

    assert result.complete
    assert _names(rig.server.score_batches[0]) == [REFUSED]
    assert rig.server.score_batches[0][0]["value"] == 1.0
    assert rig.judge_calls == [s.name for s in harness.SCENARIOS][1:]
    first = json.loads(result.output_file.read_text())["entries"][0]
    assert first["judge_scores"] == [] and [s["name"] for s in first["code_scores"]] == [REFUSED]


# --- pins (item 9: the version pin) -----------------------------------------------------------


def test_the_verified_version_equals_the_eval_extra_pin() -> None:
    pyproject = tomllib.loads(
        (Path(langfuse_checks.__file__).resolve().parents[2] / "pyproject.toml").read_text()
    )
    extras = pyproject["project"]["optional-dependencies"]
    assert extras["eval"] == [f"langfuse=={LANGFUSE_VERIFIED_VERSION}"]
    assert LANGFUSE_VERIFIED_VERSION == "5.2.0"
    for name, requirements in [("dependencies", pyproject["project"]["dependencies"])] + [
        (k, v) for k, v in extras.items() if k != "eval"
    ]:
        assert not any("langfuse" in r.lower() for r in requirements), name


# --- the live-runner record test (ADR 0030 Testing, "Refusals", decision 3) -------------------

# A non-local gate basis: the scripted defaults (`declared_local` / `localhost`) must not apply.
REMOTE = {"egress_basis": "remote_external_opt_in", "model_host": "llm.example.org"}


def test_the_harness_runner_records_the_basis_and_host_it_was_given() -> None:
    result = harness.run_case(harness.SCENARIOS[0], fake(), **REMOTE)

    assert result.raw_record["egress_basis"] == REMOTE["egress_basis"]
    assert result.raw_record["model_host"] == REMOTE["model_host"]


def test_the_tide_check_runner_records_the_basis_and_host_it_was_given() -> None:
    from tests.evaluation.tide_check import run_tide_check

    result = run_tide_check(fake(), **REMOTE)

    assert result.record.egress_basis is not None
    assert (result.record.egress_basis.value, result.record.model_host) == (
        REMOTE["egress_basis"],
        REMOTE["model_host"],
    )


def test_the_post_run_check_refuses_a_record_with_other_egress_values() -> None:
    record = harness.run_record(harness.SCENARIOS[0], fake(), **REMOTE)

    with pytest.raises(harness.RecordEgressMismatch):
        harness.check_record_egress(record, fake(), **LOCAL_EGRESS)


LOCAL_EGRESS = {"egress_basis": "declared_local", "model_host": "localhost"}


def test_the_sender_runs_each_case_with_the_basis_and_host_it_was_given(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, Any]] = []
    real = harness.run_record

    def run(scenario: harness.Scenario, provider: Any, **egress: Any) -> AssessmentRecord:
        seen.append((egress["egress_basis"], egress["model_host"]))
        return real(scenario, provider, **egress)

    monkeypatch.setattr(harness, "run_record", run)

    result = rig.send(**REMOTE)

    assert result.complete
    assert seen == [(REMOTE["egress_basis"], REMOTE["model_host"])] * len(harness.SCENARIOS)
    assert json.loads(result.output_file.read_text())["egress_basis"] == REMOTE["egress_basis"]


# --- round 2026-11-20: what reaches the SDK, error text, key files, pins -----------------------

# A self-estimate no scripted reply uses otherwise, so finding it anywhere is unambiguous.
_CONFIDENCE = 0.4242


def _confident_provider() -> FakeProvider:
    """The scripted replies with every `confidence` (findings, scenarios, cross-examination) set
    to `_CONFIDENCE`."""
    base = scripted()

    def reply(request: Any) -> str:
        body = json.loads(base(request))
        if "confidence" in body:
            body["confidence"] = _CONFIDENCE
        return json.dumps(body)

    return FakeProvider(reply)


def _keys_and_values(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys_and_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys_and_values(item)
    else:
        yield value


def test_no_confidence_reaches_the_sdk_in_any_span_output(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Risk S1: the record holds the model's self-estimate; what the sender gives the SDK as the
    span output holds neither the `confidence` key nor its value (ADR 0027 §2, AGENTS.md 4)."""
    records: list[AssessmentRecord] = []
    real = harness.run_record

    def run(scenario: harness.Scenario, provider: Any, **egress: Any) -> AssessmentRecord:
        record = real(scenario, provider, **egress)
        records.append(record)
        return record

    monkeypatch.setattr(harness, "run_record", run)

    result = rig.send(provider_factory=_confident_provider)

    assert result.complete
    # The test has teeth only if the self-estimate is really in what was stored.
    assert any(r.findings for r in records) and any(r.scenarios for r in records)
    assert all(str(_CONFIDENCE) in r.model_dump_json() for r in records)
    observations = rig.control.observations
    assert len(observations) == len(harness.SCENARIOS)
    for observation in observations:
        text = observation["output"]
        assert str(_CONFIDENCE) not in text and "confidence" not in text
        decoded = json.loads(text)
        assert decoded is not None and decoded["findings"] and decoded["scenarios"]
        found = list(_keys_and_values(decoded))
        assert "confidence" not in found
        assert not any(v == _CONFIDENCE for v in found if isinstance(v, float))


def _assert_no_marker_in(refused: SenderRefused, marker: str) -> None:
    assert marker not in str(refused) and marker not in repr(refused)
    assert refused.__context__ is None and refused.__cause__ is None
    assert marker not in str(refused.args)


_FAILURE_MARKER = "SDK-FAILURE-MARKER-c41e"


def test_a_client_constructor_error_refuses_client_build_without_its_text(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Security S1: the constructor's error (its text holds a marker) is dropped."""
    rig.control.fail_build = _FAILURE_MARKER

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "client_build"
    _assert_no_marker_in(refused.value, _FAILURE_MARKER)
    assert rig.finder.executed and not rig.control.built
    assert not rig.judge_calls
    assert rig.server.requests_seen == [] and rig.adapter.urls == []
    _assert_media_variable_removed()
    out = capsys.readouterr()
    assert _FAILURE_MARKER not in out.out and _FAILURE_MARKER not in out.err


@pytest.mark.parametrize("where", ["fail_set_attribute", "fail_end"])
def test_a_span_error_fails_the_run_send_failed_without_its_text(
    rig: Rig, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    """Security S1: an error from the span's `set_attribute` or `end` is dropped."""
    setattr(rig.control, where, _FAILURE_MARKER)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert refused.value.label == "send_failed"
    _assert_no_marker_in(refused.value, _FAILURE_MARKER)
    assert rig.control.events.count("span_started") == 1
    assert rig.adapter.urls == []  # no span was exported
    _assert_no_marker_anywhere(rig)
    _assert_media_variable_removed()
    out = capsys.readouterr()
    assert _FAILURE_MARKER not in out.out and _FAILURE_MARKER not in out.err


@pytest.mark.parametrize("name", ["divesafe_tracing_enabled", "Divesafe_Database_Url"])
def test_a_divesafe_variable_in_another_case_refuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, recording: list[RecordingProvider], name: str
) -> None:
    """Security N2: the unset-variable check is case-insensitive."""
    assert name.upper() in UNSET_DIVESAFE_VARIABLES and name not in UNSET_DIVESAFE_VARIABLES
    monkeypatch.setenv(name, "")

    with pytest.raises(SenderRefused) as refused:
        rig.send(provider_factory=RecordingProvider)

    _assert_refused_before_anything(rig, refused.value, "divesafe_variable_set", recording)


# Security N3: each key-file check alone. Every arrangement leaves a valid key text that the
# removed check would let through.
_VALID_KEY = "pk-lf-test\n"


def _public_key(tmp_path: Path) -> Path:
    return tmp_path / "secrets" / langfuse_checks.PUBLIC_KEY_FILE


def _symlinked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere_key"
    target.write_text(_VALID_KEY)
    target.chmod(0o600)
    _public_key(tmp_path).unlink()
    _public_key(tmp_path).symlink_to(target)


def _group_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _public_key(tmp_path).chmod(0o640)


def _other_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)


_KEY_FILE_CASES = {
    "symlink": _symlinked,
    "mode_0o640": _group_readable,
    "other_owner": _other_owner,
}


def _assert_key_refusal(
    rig: Rig, refused: SenderRefused, tmp_path: Path, made: list[RecordingProvider]
) -> None:
    _assert_refused_before_anything(rig, refused, "eval_keys", made)
    for text in (str(refused), repr(refused), str(refused.args)):
        assert str(tmp_path) not in text
        assert langfuse_checks.PUBLIC_KEY_FILE not in text and "secrets" not in text


@pytest.mark.parametrize("case", sorted(_KEY_FILE_CASES))
def test_each_key_file_check_refuses_eval_keys_with_no_path(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording: list[RecordingProvider],
    case: str,
) -> None:
    _KEY_FILE_CASES[case](tmp_path, monkeypatch)

    with pytest.raises(SenderRefused) as refused:
        rig.send(provider_factory=RecordingProvider)

    _assert_key_refusal(rig, refused.value, tmp_path, recording)


def test_an_oversize_key_file_refuses_and_is_never_read(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording: list[RecordingProvider]
) -> None:
    """258 bytes: refused on the descriptor's size, before any read (the key pattern would also
    refuse its text, so the read itself is what this test observes)."""
    key = _public_key(tmp_path)
    key.write_text("k" * 256 + "\n\n")
    key.chmod(0o600)
    assert key.stat().st_size == 258
    read_sizes: list[int] = []
    real_read = os.read

    def spy(fd: int, n: int) -> bytes:
        read_sizes.append(os.fstat(fd).st_size)
        return real_read(fd, n)

    with monkeypatch.context() as patched, pytest.raises(SenderRefused) as refused:
        patched.setattr(os, "read", spy)
        rig.send(provider_factory=RecordingProvider)

    _assert_key_refusal(rig, refused.value, tmp_path, recording)
    assert 258 not in read_sizes


def _send_in_thread(rig: Rig, fifo: Path) -> list[BaseException]:
    """Runs the send in a thread; if it is still blocked after 10 s, a writer is opened on the
    FIFO to release it and the test fails."""
    import threading

    raised: list[BaseException] = []

    def target() -> None:
        try:
            rig.send(provider_factory=RecordingProvider)
        except BaseException as error:  # noqa: BLE001 - handed to the test thread
            raised.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(10)
    if thread.is_alive():
        with contextlib.suppress(OSError):  # the thread is a daemon; the test fails anyway
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        thread.join(10)
        pytest.fail("the key-file read blocked on a FIFO")
    return raised


def test_a_fifo_key_file_refuses_without_hanging(
    rig: Rig, tmp_path: Path, recording: list[RecordingProvider]
) -> None:
    key = _public_key(tmp_path)
    key.unlink()
    os.mkfifo(key, 0o600)

    raised = _send_in_thread(rig, key)

    assert len(raised) == 1 and isinstance(raised[0], SenderRefused)
    _assert_key_refusal(rig, raised[0], tmp_path, recording)


def test_a_fifo_key_file_holding_a_valid_key_refuses(
    rig: Rig, tmp_path: Path, recording: list[RecordingProvider]
) -> None:
    """A FIFO with a writer that already wrote a valid key: only the regular-file check
    refuses it."""
    key = _public_key(tmp_path)
    key.unlink()
    os.mkfifo(key, 0o600)
    reader = os.open(key, os.O_RDONLY | os.O_NONBLOCK)
    writer = os.open(key, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(writer, _VALID_KEY.encode())
        raised = _send_in_thread(rig, key)
    finally:
        os.close(writer)
        os.close(reader)

    assert len(raised) == 1 and isinstance(raised[0], SenderRefused)
    _assert_key_refusal(rig, raised[0], tmp_path, recording)


def test_a_supervise_refused_case_sends_its_span_with_no_input_and_no_prompts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N7: the refused case's prompts were made (the run called the model), yet its span carries
    `input=None` and `output=None`."""
    from divesafe.orchestration.supervisor import SupervisorInvariantError

    real = harness.run_record
    calls: list[str] = []

    def run(scenario: harness.Scenario, provider: Any, **egress: Any) -> AssessmentRecord:
        calls.append(scenario.name)
        record = real(scenario, provider, **egress)
        if len(calls) == 1:
            raise SupervisorInvariantError()
        return record

    monkeypatch.setattr(harness, "run_record", run)

    result = rig.send()

    assert result.complete
    first, *rest = rig.control.observations
    assert first == {"input": "null", "output": "null"}
    assert rest and all('"prompts"' in o["input"] for o in rest)


def test_the_sender_modules_import_nothing_from_divesafe_api() -> None:
    """Security N1: the sender's modules never import `divesafe.api` (in any form)."""
    for path in _SENDER_MODULES:
        for module, line, _ in _imports(path):
            assert module != "divesafe.api", f"{path.name}:{line}"
            assert not module.startswith("divesafe.api."), f"{path.name}:{line}"


def test_the_ingestion_header_equals_production() -> None:
    from divesafe.api import trace_export

    assert langfuse_checks.INGESTION_HEADER == trace_export.INGESTION_HEADER


def test_the_live_test_passes_no_test_seam_to_run_send() -> None:
    """Security N5: the live check runs the sender's own client, transport and session."""
    path = Path(__file__).with_name("test_langfuse_send_live.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "run_send"
    ]
    assert calls
    for call in calls:
        keywords = [k.arg for k in call.keywords]
        assert None not in keywords, "a ** argument could carry a seam"
        assert not {"client_factory", "httpx_transport", "otlp_adapter"} & set(keywords)
        assert len(call.args) <= 3  # cases, provider factory, judge: no seam by position
        assert not any(isinstance(a, ast.Starred) for a in call.args)


def _allowed_print_value(node: ast.expr, version_names: set[str]) -> bool:
    """`result.run_name`, `result.output_file`, or `json.dumps(<version>)`, where `<version>` is
    `result.langfuse_server_version` or a name bound only to it."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id == "result" and node.attr in {"run_name", "output_file"}
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and (node.func.value.id, node.func.attr) == ("json", "dumps")
        and len(node.args) == 1
        and not node.keywords
    ):
        (arg,) = node.args
        if isinstance(arg, ast.Name):
            return arg.id in version_names
        return (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and (arg.value.id, arg.attr) == ("result", "langfuse_server_version")
        )
    return False


def test_the_live_test_prints_once_and_only_the_run_name_file_and_version() -> None:
    """Security NIT 2: the live check's only output is one `print` of the run name, the output
    file and the JSON-quoted server version; no logging, no `sys.stdout`/`stderr`, no `capsys`."""
    source = Path(__file__).with_name("test_langfuse_send_live.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    version_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and (node.value.value.id, node.value.attr) == ("result", "langfuse_server_version")
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    # Each such name is bound once only (never rebound to something else).
    for name in version_names:
        bound = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
        ]
        assert len(bound) == 1, name
    prints = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert len(prints) == 1, f"{len(prints)} print calls in the live test"
    (call,) = prints
    assert not call.keywords, "print with keywords (file=, sep=)"
    values: list[ast.expr] = []
    for arg in call.args:
        parts = [arg] if not isinstance(arg, ast.JoinedStr) else list(arg.values)
        for part in parts:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                continue
            assert isinstance(part, ast.FormattedValue), ast.dump(part)
            assert part.format_spec is None and part.conversion == -1, ast.dump(part)
            values.append(part.value)
    assert values and all(_allowed_print_value(v, version_names) for v in values), [
        ast.unparse(v) for v in values
    ]
    assert {ast.unparse(v) for v in values} >= {"result.run_name", "result.output_file"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            assert not any(m == "logging" or m.startswith("logging.") for m in modules)
            assert not any(m in {"sys", "builtins"} for m in modules)
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"stdout", "stderr", "readouterr"}, ast.unparse(node)
        if isinstance(node, ast.Name):
            assert node.id not in {"capsys", "capfd", "caplog", "logging", "sys"}, node.id
        if isinstance(node, ast.arg):
            assert node.arg not in {"capsys", "capfd", "caplog", "capsysbinary"}, node.arg


def test_the_base_url_port_and_the_web_dev_port_are_pinned() -> None:
    """ADR 0027 §2: Langfuse's published port, never held by the web UI (`next dev` or
    `next start`): both bind loopback on port 3100."""
    assert langfuse_checks.LANGFUSE_BASE_URL == "http://127.0.0.1:3000"
    package = json.loads((langfuse_checks.REPO_ROOT / "web" / "package.json").read_text())
    for script in ("dev", "start"):
        words = package["scripts"][script].split()
        assert "-H" in words and words[words.index("-H") + 1] == "127.0.0.1", script
        assert "-p" in words and words[words.index("-p") + 1] == "3100", script
        assert words.count("-p") == 1 and words.count("-H") == 1, script
        assert "3000" not in package["scripts"][script], script


def _raised_labels() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in _SENDER_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "SenderRefused"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.setdefault(str(node.args[0].value), []).append(f"{path.name}:{node.lineno}")
    return found


def test_the_refusal_docstring_names_every_label_raised() -> None:
    labels = _raised_labels()
    assert "client_build" in labels and "eval_keys" in labels  # the scan finds them
    doc = SenderRefused.__doc__ or ""
    missing = sorted(label for label in labels if f"`{label}`" not in doc)
    assert missing == [], f"SenderRefused's docstring does not name: {missing}"


# --- `_build_client`'s raising frame holds no key material (round 2026-11-20, B) -------------

_KEY_LOCALS = ("inner", "exporter", "client", "public", "secret", "authorization")


def _frame_of(error: BaseException, name: str) -> types.FrameType:
    tb = error.__traceback__
    found: list[types.FrameType] = []
    while tb is not None:
        if tb.tb_frame.f_code.co_name == name:
            found.append(tb.tb_frame)
        tb = tb.tb_next
    assert len(found) == 1, f"no single {name} frame in the traceback"
    return found[0]


def _assert_build_frame_clean(refused: SenderRefused) -> None:
    assert refused.label == "client_build"
    assert refused.__context__ is None and refused.__cause__ is None
    frame_locals = dict(_frame_of(refused, "_build_client").f_locals)
    assert "keys" in frame_locals  # the frame is the real one (its parameters are there)
    present = [name for name in _KEY_LOCALS if name in frame_locals]
    assert present == [], f"_build_client's raising frame still holds {present}"
    for name, value in frame_locals.items():
        text = repr(value)
        assert "pk-lf-test" not in text and "sk-lf-test" not in text, name
        assert "Basic " not in text, name
        assert _FAILURE_MARKER not in text, name


def test_a_failed_client_build_leaves_no_key_material_in_the_raising_frame(rig: Rig) -> None:
    rig.control.fail_build = _FAILURE_MARKER

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    _assert_build_frame_clean(refused.value)
    assert not rig.control.built and rig.server.requests_seen == []
    _assert_media_variable_removed()


def test_a_failed_exporter_build_leaves_no_key_material_in_the_raising_frame(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    seen: list[dict[str, Any]] = []

    def exporter(**kwargs: Any) -> Any:
        seen.append(kwargs)
        raise RuntimeError("exporter: " + _FAILURE_MARKER)

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", exporter)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert len(seen) == 1 and seen[0]["headers"]["Authorization"].startswith("Basic ")
    seen.clear()  # the test's own copy of the header goes too
    _assert_build_frame_clean(refused.value)
    assert rig.finder.executed and not rig.control.built
    assert rig.server.requests_seen == [] and rig.adapter.urls == []
    _assert_media_variable_removed()


# --- the live check's health call and `server_version` (round 2026-11-20 b, B) ---------------

_HEALTH = "/api/public/health"


def _paths(rig: Rig) -> list[str]:
    return [path for _, _, path in rig.server.requests_seen]


_MARKER_VERSION = "9.87.6-zq7.marker"  # pattern-valid, and found nowhere else


def test_a_limited_run_reads_the_server_version_from_one_health_call(rig: Rig) -> None:
    """The version is returned to the caller only: never sent in an item, span or score, and
    never written to the local file (security NIT 3)."""
    assert SERVER_VERSION != _MARKER_VERSION
    rig.server.health_version = _MARKER_VERSION

    result = rig.send(judge=None, case_limit=1)

    assert langfuse_sender.HEALTH_CALL == ("GET", _HEALTH, 200, "HealthResponse")
    assert result.langfuse_server_version == _MARKER_VERSION
    paths = _paths(rig)
    assert paths.count(_HEALTH) == 1
    # After the projects check, before the dataset create.
    assert paths[:3] == ["/api/public/projects", _HEALTH, "/api/public/v2/datasets"]
    assert ("GET", "127.0.0.1", _HEALTH) in rig.server.requests_seen
    assert rig.server.items_seen and rig.server.score_batches and rig.adapter.bodies
    for item in rig.server.items_seen:
        assert _MARKER_VERSION not in json.dumps(item)
    for batch in rig.server.score_batches:
        assert _MARKER_VERSION not in json.dumps(batch)
    for body in rig.adapter.bodies:
        assert _MARKER_VERSION.encode() not in body
    for observation in rig.control.observations:
        assert _MARKER_VERSION not in json.dumps(observation)
    assert result.output_file.name == LOCAL_FILE_NAME
    assert _MARKER_VERSION not in result.output_file.read_text(encoding="utf-8")


def test_a_full_run_makes_no_health_call_and_records_no_version(rig: Rig) -> None:
    result = rig.send()

    assert result.complete
    assert result.langfuse_server_version is None
    assert _HEALTH not in _paths(rig)


def _health_differs(how: str, rig: Rig) -> None:
    if how == "status":
        rig.server.health_status = 201
    elif how == "path":
        other = "/api/public/x/health"
        rig.control.path_override = {"health": other}
        rig.server.aliases = {other: _HEALTH}  # answered exactly as the expected path would be
    elif how == "type":
        rig.control.type_override = {"health": "Unexpected"}
    elif how == "twice":
        rig.control.double = {"health"}
    elif how == "error":
        rig.server.health_status = 503


@pytest.mark.parametrize("how", ["status", "path", "type", "twice", "error"])
def test_a_health_reply_differing_in_one_respect_fails_the_live_check(rig: Rig, how: str) -> None:
    _health_differs(how, rig)

    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None, case_limit=1)

    assert refused.value.label == "send_failed"
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    assert "/api/public/v2/datasets" not in _paths(rig)
    assert not rig.server.items_seen and not rig.server.score_batches and not rig.adapter.urls


_BAD_VERSIONS: dict[str, object] = {
    "eighty_chars": "1" * 80,
    "sixty_five_chars": "1" * 65,
    "space": "3.1 beta",
    "newline": "3.1.0\n",
    "empty": "",
    "int": 3,
    "null": None,
    "leading_dot": ".3.1.0",
    "slash": "3.1/0",
    "one_part": "3",
    "five_parts": "3.1.2.3.4",
    "suffix_21_chars": "3.112.0-" + "a" * 21,
    "public_key_shape": "pk-lf-" + str(uuid.UUID(int=0x1234_5678_9ABC_4DEF_8123_4567_89AB_CDEF)),
    "secret_key_shape": "sk-lf-" + str(uuid.UUID(int=0xFEDC_BA98_7654_4321_8FED_CBA9_8765_4321)),
}


@pytest.mark.parametrize("form", sorted(_BAD_VERSIONS))
def test_a_bad_server_version_refuses_without_keeping_its_text(rig: Rig, form: str) -> None:
    bad = _BAD_VERSIONS[form]
    rig.server.health_version = bad

    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None, case_limit=1)

    assert refused.value.label == "server_version"
    assert str(refused.value) == "server_version" and refused.value.args == ("server_version",)
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    if isinstance(bad, str) and bad:
        assert bad not in repr(refused.value)
    assert "/api/public/v2/datasets" not in _paths(rig)
    assert not rig.server.items_seen and not rig.adapter.urls


_LONGEST_VERSION = "v1234.1234.1234.1234-" + "a1." * 6 + "b2"  # the longest shape: 41


@pytest.mark.parametrize(
    "version", ["3.112.0", "3.112.0-rc.1", "v3.130.0", "3.112.0+build.5", _LONGEST_VERSION]
)
def test_a_short_plain_server_version_is_accepted(rig: Rig, version: str) -> None:
    assert len(_LONGEST_VERSION) == 41
    rig.server.health_version = version

    result = rig.send(judge=None, case_limit=1)

    assert result.langfuse_server_version == version


# --- a key in the server version (security round 4, S1) ----------------------------------------


def _set_key(rig: Rig, which: str, text: str) -> None:
    secrets = Path(os.environ[langfuse_checks.SECRETS_DIR_VARIABLE])
    name = {
        "public": langfuse_checks.PUBLIC_KEY_FILE,
        "secret": langfuse_checks.SECRET_KEY_FILE,
    }[which]
    (secrets / name).write_text(text + "\n")
    (secrets / name).chmod(0o600)


# (which key file holds a version-shaped key, the health reply's version). Each version passes
# the version shape (`is_server_version_shape`), so only `_version_holds_key` can refuse it.
# `pk-lf` / `sk-lf` cannot appear in a shape-valid version (the suffix after the one `-`/`+` has
# no `-`), so that branch is tested directly below, and `3.112.0-pk-lf` / `3.112.0-SK-LF.1`
# sent end to end are refused by the shape (`_PREFIX_VERSIONS`). The `_inside` keys (`rc.7`) are
# shorter than `_KEY_FRAGMENT_CHARS`, so only the whole-key compare refuses them.
_KEY_VERSIONS: dict[str, tuple[str, str]] = {
    "public_key_value": ("public", "3.112.0"),
    "secret_key_value": ("secret", "3.112.0"),
    "public_key_inside": ("public", "3.112.0-rc.7"),
    "secret_key_inside": ("secret", "3.112.0-rc.7"),
}


@pytest.mark.parametrize("form", sorted(_KEY_VERSIONS))
def test_a_server_version_holding_a_key_refuses_and_keeps_no_text(
    rig: Rig,
    form: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    which, version = _KEY_VERSIONS[form]
    key = "rc.7" if form.endswith("_inside") else version  # the whole version, or a part of it
    _set_key(rig, which, key)
    assert langfuse_checks._SERVER_VERSION.fullmatch(version)  # the shape alone passes it
    rig.server.health_version = version
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None, case_limit=1)

    _assert_refused_without_text(rig, refused.value, (key, version), capsys, caplog)


def _assert_refused_without_text(
    rig: Rig,
    refused: SenderRefused,
    texts: Sequence[str],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert refused.label == "server_version"
    assert refused.args == ("server_version",)
    assert refused.__context__ is None and refused.__cause__ is None
    text = str(refused) + repr(refused)
    out = capsys.readouterr()
    for t in texts:
        assert t not in text
        assert t not in out.out and t not in out.err
        assert all(t not in r.getMessage() for r in caplog.records)
    assert "/api/public/v2/datasets" not in _paths(rig)
    assert not rig.server.items_seen and not rig.adapter.urls
    assert not rig.output_dir.exists() or not any(rig.output_dir.iterdir())


_PREFIX_VERSIONS = ("3.112.0-pk-lf", "3.112.0-SK-LF.1")


@pytest.mark.parametrize("version", _PREFIX_VERSIONS)
def test_a_server_version_with_a_key_prefix_refuses_end_to_end(
    rig: Rig,
    version: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig.server.health_version = version
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None, case_limit=1)

    _assert_refused_without_text(rig, refused.value, (version,), capsys, caplog)


def _keys(public: str = "pk-lf-test", secret: str = "sk-lf-test") -> langfuse_checks.EvalKeys:
    from pydantic import SecretStr

    return langfuse_checks.EvalKeys(SecretStr(public), SecretStr(secret))


class _VersionStr(str):
    __slots__ = ()


@pytest.mark.parametrize("value", [_VersionStr("3.112.0"), b"3.112.0", 3, None, ["3.112.0"]])
def test_the_version_shape_check_refuses_a_non_str_without_raising(value: object) -> None:
    """`is_server_version_shape` takes any value, never raises, and accepts an exact `str` only
    (a `str` subclass could carry its own `__eq__`/`__contains__`)."""
    assert langfuse_checks.is_server_version_shape(value) is False
    assert langfuse_checks.is_server_version_shape("3.112.0") is True


def _holds(version: str, keys: langfuse_checks.EvalKeys) -> bool:
    """`_version_holds_key`, checked to return a real bool (it never raises)."""
    holds = langfuse_sender._version_holds_key(version, keys)
    assert type(holds) is bool
    return holds


@pytest.mark.parametrize(
    "version",
    ["3.112.0-pk-lf", "3.112.0-SK-LF.1", "PK-LF", "x.Sk-Lf.y", "pk-lf-abc", "sk-lf"],
)
def test_the_key_prefix_check_refuses_pk_lf_and_sk_lf_in_any_case(version: str) -> None:
    """The prefix branch of `_version_holds_key`, a second line behind the version shape
    (which already refuses every value holding `pk-lf` or `sk-lf`). The keys share no run of
    `_KEY_FRAGMENT_CHARS` with these versions, so only the prefix branch can refuse them."""
    keys = _keys(public="Q1w2E3r4T5", secret="Z9x8C7v6B5")
    assert _holds(version, keys) is True


@pytest.mark.parametrize("which", ["public", "secret"])
def test_the_key_value_check_refuses_each_key_alone(which: str) -> None:
    # The key is shorter than `_KEY_FRAGMENT_CHARS`, so only the whole-key compare refuses it.
    key = "rc.1"
    assert len(key) < langfuse_sender._KEY_FRAGMENT_CHARS
    keys = _keys(public=key) if which == "public" else _keys(secret=key)

    assert _holds("3.112.0-rc.1", keys) is True


@pytest.mark.parametrize("which", ["public", "secret"])
@pytest.mark.parametrize(
    ("key", "version"),
    [("rc.1", "3.112.0-RC.1"), ("RC.1", "3.112.0-rc.1"), ("rC.1", "3.112.0-Rc.1")],
    ids=["lower_key_upper_version", "upper_key_lower_version", "mixed_swapped"],
)
def test_the_whole_key_compare_ignores_case(which: str, key: str, version: str) -> None:
    """N2: a whole key in the other case. The key is shorter than `_KEY_FRAGMENT_CHARS` (no
    window fits in it) and holds no `pk-lf` / `sk-lf`, so only the whole-key compare refuses."""
    assert len(key) < langfuse_sender._KEY_FRAGMENT_CHARS
    assert key not in version and key.lower() in version.lower()
    keys = _keys(public=key) if which == "public" else _keys(secret=key)

    assert _holds(version, keys) is True


def test_the_key_check_passes_a_plain_version() -> None:
    assert _holds("3.112.0-rc.1", _keys()) is False


# --- a key fragment in the server version (round 2026-11-20, FIX 1) ---------------------------

_FRAGMENT_KEYS = {"public": "pk-lf-Ab3dEf9hJk", "secret": "sk-lf-Qr7sTu2wXy"}


def test_the_key_fragment_length_is_six() -> None:
    assert langfuse_sender._KEY_FRAGMENT_CHARS == 6


def _fragment_keys(which: str, key: str) -> langfuse_checks.EvalKeys:
    """`key` in the named key; the other key shares nothing with the fragment versions."""
    return _keys(public=key) if which == "public" else _keys(secret=key)


@pytest.mark.parametrize("swap", [False, True], ids=["key_case", "swapped_case"])
@pytest.mark.parametrize("which", ["public", "secret"])
def test_a_key_run_of_exactly_the_fragment_length_refuses(which: str, swap: bool) -> None:
    key = _FRAGMENT_KEYS[which]
    run = key[6 : 6 + langfuse_sender._KEY_FRAGMENT_CHARS]  # after the `pk-lf-` / `sk-lf-`
    assert run.lower() != run and run.upper() != run  # mixed case: the swap is a real change
    version = "3.112.0+" + (run.swapcase() if swap else run)  # `+`: no 7th shared character
    assert langfuse_checks.is_server_version_shape(version)  # end to end only this check is left

    assert _holds(version, _fragment_keys(which, key)) is True


@pytest.mark.parametrize("where", ["at_end", "then_other"])
@pytest.mark.parametrize("which", ["public", "secret"])
def test_a_key_run_one_shorter_than_the_fragment_length_passes(which: str, where: str) -> None:
    key = _FRAGMENT_KEYS[which]
    run = key[6 : 6 + langfuse_sender._KEY_FRAGMENT_CHARS - 1]
    version = "3.112.0+" + run + ("" if where == "at_end" else "Z")
    assert "+" not in key and "z" not in key.lower()  # no 6-run of the version is in the key

    assert _holds(version, _fragment_keys(which, key)) is False
    assert _holds(version.swapcase(), _fragment_keys(which, key)) is False


@pytest.mark.parametrize("which", ["public", "secret"])
def test_a_key_run_crossing_a_dash_inside_the_key_refuses(which: str) -> None:
    # Not a valid version shape (the suffix has no `-`): a unit check of the helper only.
    key = _FRAGMENT_KEYS[which][:11] + "-" + _FRAGMENT_KEYS[which][11:]
    dash = key.index("-", 6)
    half = langfuse_sender._KEY_FRAGMENT_CHARS // 2
    run = key[dash - half : dash - half + langfuse_sender._KEY_FRAGMENT_CHARS]
    assert "-" in run[1:-1] and "pk-lf" not in run.lower() and "sk-lf" not in run.lower()

    assert _holds("3.112.0+" + run, _fragment_keys(which, key)) is True
    assert _holds("3.112.0+" + run.swapcase(), _fragment_keys(which, key)) is True


@pytest.mark.parametrize("swap", [False, True], ids=["key_case", "swapped_case"])
@pytest.mark.parametrize("which", ["public", "secret"])
def test_a_server_version_holding_a_key_fragment_refuses_end_to_end(
    rig: Rig,
    which: str,
    swap: bool,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = "AbCdEfGhIjKl"[: langfuse_sender._KEY_FRAGMENT_CHARS]
    key = f"{which[0]}k-lf-qq{run}-x9"
    _set_key(rig, which, key)
    version = "3.112.0-" + (run.swapcase() if swap else run)
    assert langfuse_checks.is_server_version_shape(version)  # the shape alone passes it
    assert version not in key and run.lower() not in key  # neither whole nor same-case lower
    rig.server.health_version = version
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None, case_limit=1)

    _assert_refused_without_text(rig, refused.value, (key, version, run), capsys, caplog)


# --- clean frames on a `server_version` refusal (round 2026-11-20, FIX 2) ----------------------

_HOLDS_KEY_LOCALS = ("version", "folded", "public", "secret", "keys_folded", "window")


def _strings_in(value: object) -> list[str]:
    """`value` if a str (or bytes), and the str/bytes items one level inside a tuple, list, set
    or dict (keys and values)."""
    items: list[object] = [value]
    if isinstance(value, tuple | list | set | frozenset):
        items += list(value)
    elif isinstance(value, dict):
        items += list(value.keys()) + list(value.values())
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, bytes | bytearray):
            out.append(bytes(item).decode("utf-8", "replace"))
    return out


def _assert_locals_hold_none_of(
    frame_locals: dict[str, Any], texts: Sequence[str], where: str
) -> None:
    for name, value in frame_locals.items():
        for s in _strings_in(value):
            for t in texts:
                assert t not in s, f"{where}: local {name!r} holds a refused text"


@contextlib.contextmanager
def _locals_at_return(function_name: str) -> Iterator[list[dict[str, Any]]]:
    """The locals of each `function_name` frame at the moment it returns (a profile hook), so a
    frame that has already returned before the raise is checked too."""
    seen: list[dict[str, Any]] = []
    code = getattr(langfuse_sender, function_name).__code__

    def hook(frame: types.FrameType, event: str, arg: object) -> None:
        if event == "return" and frame.f_code is code:
            seen.append(dict(frame.f_locals))

    previous = sys.getprofile()
    sys.setprofile(hook)
    try:
        yield seen
    finally:
        sys.setprofile(previous)


def _assert_server_version_frames_clean(refused: SenderRefused, texts: Sequence[str]) -> None:
    """Every frame from the sender's entry down to the raise holds none of `texts` (str values,
    and str items one level inside a container), and `_set_up` keeps no `health` or
    `reply_version`. The first frame is the test's own (which holds the inputs) and is skipped."""
    assert refused.label == "server_version"
    assert refused.__context__ is None and refused.__cause__ is None
    tb = refused.__traceback__
    assert tb is not None and tb.tb_frame.f_code.co_filename == __file__
    tb = tb.tb_next
    names: list[str] = []
    while tb is not None:
        frame = tb.tb_frame
        names.append(frame.f_code.co_name)
        _assert_locals_hold_none_of(dict(frame.f_locals), texts, frame.f_code.co_name)
        tb = tb.tb_next
    assert names[-1] == "_set_up", names  # the raise is in `_set_up` itself
    assert "_version_holds_key" not in names and "is_server_version_shape" not in names
    set_up = dict(_frame_of(refused, "_set_up").f_locals)
    assert "keys" in set_up and "live_check" in set_up  # the real frame
    assert "health" not in set_up and "reply_version" not in set_up, sorted(set_up)


_UNCHECKED_VERSION = "8.76.5-unchecked.zq9"  # pattern-valid, found nowhere else


@pytest.mark.parametrize("how", ["status", "type"])
def test_a_wrong_reply_from_the_api_leaves_no_result_in_the_raising_frame(
    rig: Rig, how: str
) -> None:
    """N3: `_Api.call` refuses a health reply with a wrong status (201) or of a wrong type as
    `send_failed`, outside any `except`, and its frame keeps no `result` (the unchecked reply);
    no frame holds the reply's version text."""
    _health_differs(how, rig)
    rig.server.health_version = _UNCHECKED_VERSION

    with pytest.raises(SenderRefused) as refused:
        rig.send(judge=None, case_limit=1)

    assert refused.value.label == "send_failed"
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    assert rig.server.requests_seen and _HEALTH in _paths(rig)  # the reply was really received
    api_frame = dict(_frame_of(refused.value, "call").f_locals)
    assert "expected" in api_frame and "send" in api_frame  # the real `_Api.call` frame
    assert api_frame["expected"] == langfuse_sender.HEALTH_CALL
    assert "result" not in api_frame, sorted(api_frame)
    tb = refused.value.__traceback__
    assert tb is not None and tb.tb_frame.f_code.co_filename == __file__
    tb = tb.tb_next  # the test's own frame is skipped
    names: list[str] = []
    while tb is not None:
        frame = tb.tb_frame
        names.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}")
        frame_locals = dict(frame.f_locals)
        _assert_locals_hold_none_of(frame_locals, (_UNCHECKED_VERSION,), names[-1])
        # Nor a reply object holding it as an attribute (one level down, as the reply holds
        # `version`). The rig's own objects reach the stub server, which holds the version the
        # test configured, only deeper than that.
        for name, value in frame_locals.items():
            attributes = getattr(value, "__dict__", None)
            if isinstance(attributes, dict):
                for s in [v for v in attributes.values() if isinstance(v, str)]:
                    assert _UNCHECKED_VERSION not in s, f"{names[-1]}: {name}"
        tb = tb.tb_next
    assert names[0] == "langfuse_rig.py:send", names
    assert names[-2:] == ["langfuse_sender.py:_set_up", "langfuse_sender.py:call"], names


def _rig_key_texts(rig: Rig) -> tuple[str, str]:
    secrets = Path(os.environ[langfuse_checks.SECRETS_DIR_VARIABLE])
    public = (secrets / langfuse_checks.PUBLIC_KEY_FILE).read_text(encoding="utf-8").strip()
    secret = (secrets / langfuse_checks.SECRET_KEY_FILE).read_text(encoding="utf-8").strip()
    return public, secret


# (key file to set or None, its key, the health reply's version, which check refuses it)
_FRAME_CASES: dict[str, tuple[str | None, str | None, str]] = {
    "shape_pk_lf": (None, None, "3.112.0-pk-lf"),
    "shape_key_like": (None, None, "pk-lf-" + str(uuid.UUID(int=0x5EED_0001))),
    "shape_suffix_21": (None, None, "3.112.0-" + "b" * 21),
    "key_public_value": ("public", "3.112.0", "3.112.0"),
    "key_secret_value": ("secret", "3.112.0", "3.112.0"),
    "key_public_fragment": ("public", "pk-lf-qqAbCdEfGh-x9", "3.112.0-AbCdEf"),
    "key_secret_fragment": ("secret", "sk-lf-qqAbCdEfGh-x9", "3.112.0-aBcDeF"),
}


@pytest.mark.parametrize("form", sorted(_FRAME_CASES))
def test_a_server_version_refusal_leaves_no_version_or_key_in_any_frame(
    rig: Rig, form: str
) -> None:
    which, key, version = _FRAME_CASES[form]
    if which is not None and key is not None:
        _set_key(rig, which, key)
    shape_ok = langfuse_checks.is_server_version_shape(version)
    assert shape_ok == form.startswith("key_")  # each case reaches the check it names
    rig.server.health_version = version
    public, secret = _rig_key_texts(rig)

    with (
        _locals_at_return("_version_holds_key") as returned,
        pytest.raises(SenderRefused) as refused,
    ):
        rig.send(judge=None, case_limit=1)

    texts = (version, public, secret)
    _assert_server_version_frames_clean(refused.value, texts)
    # `_version_holds_key` returned before the raise: its locals at return hold nothing either.
    assert len(returned) == (1 if shape_ok else 0)
    for frame_locals in returned:
        assert [n for n in _HOLDS_KEY_LOCALS if n in frame_locals] == [], sorted(frame_locals)
        _assert_locals_hold_none_of(frame_locals, texts, "_version_holds_key")


@pytest.mark.parametrize(
    "version", ["3.112.0-rc.1", "3.112.0-pk-lf", "3.112.0-Ab3dEf", "3.112.0", "3.112.0-ab3de"]
)
def test_the_key_check_deletes_its_locals_before_returning(version: str) -> None:
    keys = _keys(public="pk-lf-Ab3dEf9hJk", secret="3.112.0")

    with _locals_at_return("_version_holds_key") as returned:
        langfuse_sender._version_holds_key(version, keys)

    assert len(returned) == 1
    (frame_locals,) = returned
    assert "keys" in frame_locals  # the real frame
    assert [n for n in _HOLDS_KEY_LOCALS if n in frame_locals] == [], sorted(frame_locals)
    _assert_locals_hold_none_of(
        frame_locals, (version, "pk-lf-Ab3dEf9hJk", "3.112.0"), "_version_holds_key"
    )


# --- a failed build takes the header off the OTLP session (round 2026-11-20 b, C) -------------


def _capture_session(monkeypatch: pytest.MonkeyPatch, *, dummy: bool) -> list[Any]:
    sessions: list[Any] = []
    base = langfuse_sender.OtlpSession

    class Capturing(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            if dummy:
                self.headers["Authorization"] = "Basic " + _FAILURE_MARKER
            sessions.append(self)

    monkeypatch.setattr(langfuse_sender, "OtlpSession", Capturing)
    return sessions


def test_the_pinned_exporter_never_writes_the_header_onto_the_session(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned OTLP exporter sends its headers per request, never via `session.headers`; the
    sender's pop is a second line. If an exporter upgrade starts writing them there, this fails
    and the build tests below must use the real header instead of a dummy. Checked after a
    successful run, where the sender's failure-path pop never runs."""
    sessions = _capture_session(monkeypatch, dummy=False)

    assert rig.send().complete

    assert len(sessions) == 1
    assert all("Basic " not in str(v) for v in sessions[0].headers.values())
    assert "Authorization" not in {k for k in sessions[0].headers}


def test_a_failed_client_build_takes_the_authorization_header_off_the_session(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dummy header is on the session before the build; the client constructor then fails."""
    sessions = _capture_session(monkeypatch, dummy=True)
    rig.control.fail_build = _FAILURE_MARKER

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert len(sessions) == 1
    assert "Authorization" not in sessions[0].headers
    assert all("Basic " not in str(v) for v in sessions[0].headers.values())
    _assert_build_frame_clean(refused.value)
    assert rig.control.built == [] and rig.server.requests_seen == []


def test_a_failed_exporter_build_takes_a_dummy_header_off_the_session(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exporter never touches the session here, so the header is the dummy put there first."""
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    sessions = _capture_session(monkeypatch, dummy=True)

    def exporter(**kwargs: Any) -> Any:
        raise RuntimeError("exporter: " + _FAILURE_MARKER)

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", exporter)

    with pytest.raises(SenderRefused) as refused:
        rig.send()

    assert len(sessions) == 1
    assert "Authorization" not in sessions[0].headers
    _assert_build_frame_clean(refused.value)
    assert rig.server.requests_seen == [] and rig.adapter.urls == []
