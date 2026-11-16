"""`input_used` (the server's `usage.prompt_tokens`, ADR 0027 amendment step 4) reaches the
run's trace collector only: the "model call finished" log line keeps exactly
`CALL_LOG_FIELDS`, and the count never changes a result.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Any

import pytest

from divesafe.agents import runner
from divesafe.agents.runner import CALL_LOG_FIELDS, AgentError, call_structured
from divesafe.agents.schemas import ProposalOutput
from divesafe.models import LLMRequest, LLMResponse, TokenLimitError
from divesafe.observability import ModelCallEntry, start_collecting

MESSAGE = "model call finished"
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
    name, model, external = "stub", "m", False

    def __init__(
        self,
        text: str = GOOD,
        *,
        output_tokens: object = None,
        input_tokens: object = None,
        raises: BaseException | None = None,
        call_timeout_seconds: float | None = None,
    ) -> None:
        self.text, self.output_tokens, self.input_tokens = text, output_tokens, input_tokens
        self.raises = raises
        if call_timeout_seconds is not None:
            self.call_timeout_seconds = call_timeout_seconds

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if hasattr(self, "call_timeout_seconds"):
            await asyncio.sleep(0.5)
        if self.raises is not None:
            raise self.raises
        return LLMResponse(
            text=self.text,
            provider=self.name,
            model=self.model,
            output_tokens=self.output_tokens,  # type: ignore[arg-type]
            input_tokens=self.input_tokens,  # type: ignore[arg-type]
        )


def _run(provider: object, call: Any = call_structured) -> tuple[object, list[ModelCallEntry]]:
    """The call's result (or its `AgentError`) and the collector's entries, in a fresh context."""

    async def scenario() -> tuple[object, list[ModelCallEntry]]:
        collector = start_collecting()
        try:
            result: object = await call(
                provider,
                task="t",
                user_message="x",
                output=ProposalOutput,
                allowed_ids=frozenset({"ev-1"}),
            )
        except AgentError as exc:
            result = (str(exc), exc.kind, exc.label)
        return result, list(collector.finish("a" * 32, "stored").model_calls)

    return contextvars.Context().run(asyncio.run, scenario())


def _log_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = [r for r in caplog.records if r.getMessage() == MESSAGE]
    assert len(records) == 1
    return records[0]


def test_the_trace_gets_input_used_and_the_log_line_does_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="divesafe.agents.runner"):
        _, calls = _run(_Stub(output_tokens=5, input_tokens=40))
    record = _log_record(caplog)
    assert set(vars(record)) - _BASE == CALL_LOG_FIELDS
    assert "input_used" not in CALL_LOG_FIELDS
    assert not hasattr(record, "input_used")
    assert 40 not in vars(record).values()
    (entry,) = calls
    assert set(entry.fields) == CALL_LOG_FIELDS | {"input_used"}
    assert (entry.fields["output_used"], entry.fields["input_used"]) == (5, 40)
    for name in CALL_LOG_FIELDS:
        assert entry.fields[name] == getattr(record, name), name


def test_a_token_limit_stop_carries_input_used_to_the_trace() -> None:
    error = TokenLimitError(output_chars=42, output_tokens=1500, input_tokens=900)
    result, calls = _run(_Stub(raises=error))
    assert result == ("t: output stopped at the token limit", "failed", "")
    (entry,) = calls
    assert (entry.fields["outcome"], entry.fields["input_used"]) == ("token_limit", 900)


@pytest.mark.parametrize(
    "stub",
    [
        _Stub("not json", input_tokens=40),
        _Stub(_proposal(["ev-9"]), input_tokens=40),
    ],
    ids=["schema", "unknown_evidence_id"],
)
def test_a_refused_reply_still_carries_input_used(stub: _Stub) -> None:
    result, calls = _run(stub)
    assert isinstance(result, tuple)
    (entry,) = calls
    assert entry.fields["input_used"] == 40


@pytest.mark.parametrize("bad", [None, -1, True, 1.0, "40", 10**9 + 1], ids=repr)
def test_no_or_a_bad_count_gives_none(bad: object) -> None:
    _, calls = _run(_Stub(input_tokens=bad))
    (entry,) = calls
    assert entry.fields["input_used"] is None


# Each factory takes the input count (None or 40) and builds the stub for one outcome.
NEVER_CHANGES: dict[str, Any] = {
    "ok": lambda n: _Stub(input_tokens=n),
    "schema": lambda n: _Stub("not json", input_tokens=n),
    "unknown_evidence_id": lambda n: _Stub(_proposal(["ev-9"]), input_tokens=n),
    "too_long": lambda n: _Stub(GOOD + " " * 64, input_tokens=n),
    "token_limit": lambda n: _Stub(raises=TokenLimitError(output_chars=1, input_tokens=n)),
    "provider_error": lambda n: _Stub(raises=ConnectionError("x"), input_tokens=n),
    "timed_out": lambda n: _Stub(input_tokens=n, call_timeout_seconds=0.02),
}


@pytest.mark.parametrize("name", list(NEVER_CHANGES))
def test_the_input_count_never_changes_a_result(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if name == "too_long":
        monkeypatch.setattr(runner, "MAX_OUTPUT_CHARS", len(GOOD))
    without, calls_without = _run(NEVER_CHANGES[name](None))
    with_count, calls_with = _run(NEVER_CHANGES[name](40))
    assert calls_without[0].fields["outcome"] == calls_with[0].fields["outcome"] == name
    assert repr(with_count) == repr(without)


def test_a_mutant_that_logs_input_used_is_caught(caplog: pytest.LogCaptureFixture) -> None:
    from tests.safety.kb_fakes import mutated_module

    mutant = mutated_module(
        runner,
        (
            '            "output_used": plain_count(output_used),\n',
            '            "output_used": plain_count(output_used),\n'
            '            "input_used": plain_count(input_used),\n',
        ),
    )
    mutant.logger = logging.getLogger(mutant.__name__)
    with caplog.at_level(logging.INFO):
        _run(_Stub(input_tokens=40), mutant.call_structured)
    records = [r for r in caplog.records if r.getMessage() == MESSAGE]
    assert len(records) == 1 and getattr(records[0], "input_used") == 40  # noqa: B009
    assert set(vars(records[0])) - _BASE != CALL_LOG_FIELDS


def test_a_mutant_that_drops_input_used_from_the_trace_is_caught() -> None:
    from tests.safety.kb_fakes import mutated_module

    mutant = mutated_module(
        runner,
        (
            '            record_model_call({**fields, "input_used": plain_count(input_used)}, '
            "started_ns)\n",
            "            record_model_call(fields, started_ns)\n",
        ),
    )
    _, calls = _run(_Stub(input_tokens=40), mutant.call_structured)
    (entry,) = calls
    assert "input_used" not in entry.fields
