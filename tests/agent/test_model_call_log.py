"""The "model call finished" log line of `call_structured` (ADR 0018 amendment 2026-11-15).

Exactly one INFO record per model call, with exactly `CALL_LOG_FIELDS` as added keys and a fixed
`outcome` label. Logging never changes a result or an error. No live calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import pytest
from tests.agent.scripted_llm import fake, run_pipeline, task_of

from divesafe.agents import runner
from divesafe.agents.runner import (
    CALL_LOG_FIELDS,
    CALL_OUTCOMES,
    AgentError,
    call_structured,
)
from divesafe.agents.schemas import ProposalOutput
from divesafe.config.logging import _SENSITIVE, JsonFormatter
from divesafe.models import FakeProvider, LLMRequest, LLMResponse, TokenLimitError

MESSAGE = "model call finished"
# The attributes every LogRecord has; anything else on the record is an `extra` key.
_BASE = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


def _proposal(ids: list[str]) -> str:
    return json.dumps(
        {
            "recommendation": "INSUFFICIENT EVIDENCE",
            "rationale": "r",
            "evidence_ids": ids,
            "uncertainties": [],
        }
    )


GOOD = _proposal(["ev-1"])


class _Stub:
    """Returns a fixed text with an optional count, raises a given error, or waits forever."""

    name, model, external = "stub", "m", False

    def __init__(
        self,
        text: str = GOOD,
        *,
        output_tokens: int | None = None,
        raises: BaseException | None = None,
        hang: bool = False,
        call_timeout_seconds: float | None = None,
    ) -> None:
        self.text = text
        self.output_tokens = output_tokens
        self.raises = raises
        self.hang = hang
        if call_timeout_seconds is not None:
            self.call_timeout_seconds = call_timeout_seconds
        self.started = asyncio.Event() if hang else None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self.raises is not None:
            raise self.raises
        if self.hang:
            assert self.started is not None
            self.started.set()
            await asyncio.sleep(3600)
        return LLMResponse(
            text=self.text, provider=self.name, model=self.model, output_tokens=self.output_tokens
        )


class _CrashingOutput(ProposalOutput):
    """An output type whose validation raises an error `call_structured` does not expect."""

    @classmethod
    def model_validate(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise KeyError("unexpected")


def _call(provider: object, output: type[ProposalOutput] = ProposalOutput) -> ProposalOutput:
    return asyncio.run(
        call_structured(
            provider,  # type: ignore[arg-type]
            task="t",
            user_message="x",
            output=output,
            allowed_ids=frozenset({"ev-1"}),
        )
    )


def _call_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == MESSAGE]


def _one(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = _call_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.name == "divesafe.agents.runner"
    assert set(vars(record)) - _BASE == CALL_LOG_FIELDS
    assert record.task == "t"  # type: ignore[attr-defined]
    assert record.outcome in CALL_OUTCOMES  # type: ignore[attr-defined]
    duration = record.duration_ms  # type: ignore[attr-defined]
    assert type(duration) is int and duration >= 0
    for name in ("output_limit", "output_chars", "output_used"):
        value = getattr(record, name)
        assert value is None or (type(value) is int and value >= 0), (name, value)
    return record


def _counts(record: logging.LogRecord) -> tuple[object, object, object]:
    return (record.output_limit, record.output_chars, record.output_used)  # type: ignore[attr-defined]


def _cancelled() -> None:
    async def scenario() -> None:
        stub = _Stub(hang=True)
        task = asyncio.create_task(
            call_structured(
                stub,  # type: ignore[arg-type]
                task="t",
                user_message="x",
                output=ProposalOutput,
                allowed_ids=frozenset({"ev-1"}),
            )
        )
        assert stub.started is not None
        await stub.started.wait()
        task.cancel()
        await task

    asyncio.run(scenario())


def test_the_outcome_labels_and_fields_are_fixed() -> None:
    assert {
        "ok",
        "token_limit",
        "timed_out",
        "provider_error",
        "too_long",
        "schema",
        "unknown_evidence_id",
        "cancelled",
        "error",
    } == CALL_OUTCOMES
    assert {
        "task",
        "outcome",
        "output_limit",
        "output_chars",
        "output_used",
        "duration_ms",
    } == CALL_LOG_FIELDS


def test_ok(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        result = _call(_Stub(output_tokens=17))
    assert result.evidence_ids == ("ev-1",)
    record = _one(caplog)
    assert record.outcome == "ok"  # type: ignore[attr-defined]
    assert record.output_limit == runner.MAX_OUTPUT_TOKENS  # type: ignore[attr-defined]


def test_ok_logs_the_reply_chars_and_the_server_count(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        _call(_Stub(output_tokens=17))
    assert _counts(_one(caplog)) == (runner.MAX_OUTPUT_TOKENS, len(GOOD), 17)


def test_ok_with_the_fake_provider_logs_chars_and_no_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        _call(FakeProvider(GOOD))
    record = _one(caplog)
    assert record.outcome == "ok"  # type: ignore[attr-defined]
    assert _counts(record) == (runner.MAX_OUTPUT_TOKENS, len(GOOD), None)


def test_the_declared_output_limit_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    stub = _Stub()
    stub.max_output_tokens = 3000  # type: ignore[attr-defined]
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        _call(stub)
    assert _one(caplog).output_limit == 3000  # type: ignore[attr-defined]


# (outcome, provider, AgentError message, kind, label, output_chars, output_used)
FAILURES: list[tuple[str, Any, str, str, str, int | None, int | None]] = [
    (
        "token_limit",
        lambda: _Stub(raises=TokenLimitError(output_chars=42, output_tokens=1500)),
        "t: output stopped at the token limit",
        "failed",
        "",
        42,
        1500,
    ),
    (
        "timed_out",
        lambda: _SlowStub(),
        "t: provider timed out",
        "timed_out",
        "",
        None,
        None,
    ),
    (
        "provider_error",
        lambda: _Stub(raises=ConnectionError("SECRET provider text")),
        "t: provider error (ConnectionError)",
        "failed",
        "",
        None,
        None,
    ),
    (
        "schema",
        lambda: _Stub("not json", output_tokens=3),
        "t: output rejected (not valid for the schema)",
        "rejected",
        "schema",
        len("not json"),
        3,
    ),
    (
        "unknown_evidence_id",
        lambda: _Stub(_proposal(["ev-9", "ev-8"]), output_tokens=5),
        "t: output cites 2 unknown evidence id(s)",
        "rejected",
        "unknown_evidence_id",
        len(_proposal(["ev-9", "ev-8"])),
        5,
    ),
]


class _SlowStub(_Stub):
    """Sleeps past its own short call budget."""

    def __init__(self) -> None:
        super().__init__(call_timeout_seconds=0.02)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(0.5)
        return await super().complete(request)


@pytest.mark.parametrize(
    ("outcome", "make", "message", "kind", "label", "chars", "used"),
    FAILURES,
    ids=[f[0] for f in FAILURES],
)
def test_a_failed_call_logs_its_outcome_and_raises_the_same_error(
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    make: Any,
    message: str,
    kind: str,
    label: str,
    chars: int | None,
    used: int | None,
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(AgentError) as caught,
    ):
        _call(make())
    error = caught.value
    assert (str(error), error.kind, error.label) == (message, kind, label)
    assert error.__cause__ is None and error.__context__ is None
    assert error.__suppress_context__
    record = _one(caplog)
    assert record.outcome == outcome  # type: ignore[attr-defined]
    assert _counts(record) == (runner.MAX_OUTPUT_TOKENS, chars, used)


def test_too_long(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "MAX_OUTPUT_CHARS", 10)
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(AgentError) as caught,
    ):
        _call(_Stub(output_tokens=8))
    assert (str(caught.value), caught.value.kind, caught.value.label) == (
        "t: output too long",
        "rejected",
        "too_long",
    )
    record = _one(caplog)
    assert record.outcome == "too_long"  # type: ignore[attr-defined]
    assert _counts(record) == (runner.MAX_OUTPUT_TOKENS, len(GOOD), 8)


def test_cancelled(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(asyncio.CancelledError),
    ):
        _cancelled()
    record = _one(caplog)
    assert record.outcome == "cancelled"  # type: ignore[attr-defined]
    assert _counts(record) == (runner.MAX_OUTPUT_TOKENS, None, None)


def test_an_unexpected_error_in_validation_is_logged_as_error_and_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(KeyError, match="unexpected"),
    ):
        _call(_Stub(output_tokens=2), output=_CrashingOutput)
    record = _one(caplog)
    assert record.outcome == "error"  # type: ignore[attr-defined]
    assert _counts(record) == (runner.MAX_OUTPUT_TOKENS, None, None)


def test_a_keyboard_interrupt_from_the_provider_is_logged_as_error_and_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A BaseException is never swallowed or turned into an AgentError."""
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(KeyboardInterrupt),
    ):
        _call(_Stub(raises=KeyboardInterrupt()))
    record = _one(caplog)
    assert record.outcome == "error"  # type: ignore[attr-defined]
    assert _counts(record) == (runner.MAX_OUTPUT_TOKENS, None, None)


def test_a_cancellation_from_inside_the_provider_is_logged_as_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(asyncio.CancelledError),
    ):
        _call(_Stub(raises=asyncio.CancelledError()))
    assert _one(caplog).outcome == "cancelled"  # type: ignore[attr-defined]


def test_every_outcome_is_covered_by_a_test_here() -> None:
    covered = {f[0] for f in FAILURES} | {"ok", "too_long", "cancelled", "error"}
    assert covered == CALL_OUTCOMES


def test_a_prompt_too_large_is_refused_before_the_call_and_logs_nothing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "MAX_PROMPT_CHARS", 0)
    stub = FakeProvider(GOOD)
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(AgentError, match="prompt too large"),
    ):
        _call(stub)
    assert _call_records(caplog) == [] and stub.requests == []


def test_two_calls_log_two_records(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        _call(_Stub())
        with pytest.raises(AgentError):
            _call(_Stub("not json"))
    assert [r.outcome for r in _call_records(caplog)] == ["ok", "schema"]  # type: ignore[attr-defined]


# ---- logging never changes the result or the error ---------------------------------------------


def _raising_info(*args: object, **kwargs: object) -> None:
    raise RuntimeError("logging is broken")


def test_a_raising_logger_does_not_change_an_accepted_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _call(_Stub())
    monkeypatch.setattr(runner.logger, "info", _raising_info)
    assert _call(_Stub()) == expected


@pytest.mark.parametrize(
    ("outcome", "make", "message", "kind", "label", "chars", "used"),
    FAILURES,
    ids=[f[0] for f in FAILURES],
)
def test_a_raising_logger_does_not_change_the_error(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    make: Any,
    message: str,
    kind: str,
    label: str,
    chars: int | None,
    used: int | None,
) -> None:
    monkeypatch.setattr(runner.logger, "info", _raising_info)
    with pytest.raises(AgentError) as caught:
        _call(make())
    assert (type(caught.value), str(caught.value), caught.value.kind, caught.value.label) == (
        AgentError,
        message,
        kind,
        label,
    )
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert caught.value.__suppress_context__


def test_a_raising_logger_does_not_change_an_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.logger, "info", _raising_info)
    with pytest.raises(asyncio.CancelledError):
        _cancelled()
    with pytest.raises(KeyError):
        _call(_Stub(), output=_CrashingOutput)
    with pytest.raises(KeyboardInterrupt):
        _call(_Stub(raises=KeyboardInterrupt()))


# ---- the JSON formatter shows the counts -------------------------------------------------------


def test_no_field_name_is_masked_by_the_formatter() -> None:
    assert [name for name in CALL_LOG_FIELDS if _SENSITIVE.search(name)] == []


def test_the_json_formatter_renders_the_counts_as_ints(caplog: pytest.LogCaptureFixture) -> None:
    """A token-limit call carries all three counts."""
    with (
        caplog.at_level(logging.INFO, logger="divesafe.agents.runner"),
        pytest.raises(AgentError),
    ):
        _call(_Stub(raises=TokenLimitError(output_chars=42, output_tokens=1500)))
    payload = json.loads(JsonFormatter().format(_one(caplog)))
    assert payload["message"] == MESSAGE
    assert payload["output_limit"] == runner.MAX_OUTPUT_TOKENS
    assert payload["output_used"] == 1500
    assert payload["output_chars"] == 42
    for name in ("output_limit", "output_used", "output_chars", "duration_ms"):
        assert type(payload[name]) is int, (name, payload[name])
    assert (payload["task"], payload["outcome"]) == ("t", "token_limit")
    assert "[REDACTED]" not in json.dumps(payload)


def test_the_json_formatter_renders_an_accepted_call_unmasked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        _call(_Stub(output_tokens=17))
    payload = json.loads(JsonFormatter().format(_one(caplog)))
    assert type(payload["output_limit"]) is int and type(payload["duration_ms"]) is int
    assert set(CALL_LOG_FIELDS) <= set(payload)
    assert "[REDACTED]" not in json.dumps(payload)


def test_the_json_formatter_renders_an_accepted_calls_count_as_an_int(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        _call(_Stub(output_tokens=17))
    payload = json.loads(JsonFormatter().format(_one(caplog)))
    assert (payload["output_used"], payload["output_chars"]) == (17, len(GOOD))


# ---- a full assessment logs only fixed task names ----------------------------------------------

_TASK_NAME = re.compile(r"^(risk_assessment|specialist:\w+|cross_examination:\w+|scenario:\w+)$")


def test_a_full_fake_assessment_logs_one_record_per_call_with_fixed_task_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = fake()
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        record = run_pipeline(provider)
    assert record.proposed_recommendation is not None
    logged = _call_records(caplog)
    assert len(logged) == len(provider.requests) > 0
    tasks = [r.task for r in logged]  # type: ignore[attr-defined]
    assert [t for t in tasks if not _TASK_NAME.fullmatch(t)] == []
    assert sorted(tasks) == sorted(task_of(r) for r in provider.requests)
    assert {r.outcome for r in logged} == {"ok"}  # type: ignore[attr-defined]
    assert any(t.startswith("specialist:") for t in tasks)
    assert any(t.startswith("scenario:") for t in tasks)
    assert "risk_assessment" in tasks
