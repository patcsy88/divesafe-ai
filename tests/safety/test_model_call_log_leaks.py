"""The "model call finished" log line carries counts and fixed labels only (AGENTS.md section 3:
never log prompts or free-text content; ADR 0018 amendment 2026-11-15).

Calls run through the real adapter (remote https host, injected transport, no network) for five
outcomes: accepted, token limit, schema rejection, unknown evidence id and a provider error event.
Every captured record is scanned (every attribute except the runtime numbers `logging` adds,
`tests.safety.log_fields`) for markers placed in the reply, the prompt, an evidence id, the
provider's error text, the model name and the server URL. Two mutants of `runner` (one logs the
reply text, one logs the prompt) prove the scan has teeth.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module
from tests.safety.log_fields import scanned_records

from divesafe.agents import runner
from divesafe.agents.schemas import ProposalOutput
from divesafe.models.openai_compatible import OpenAICompatibleProvider

pytestmark = pytest.mark.safety

REPLY_MARKER = "REPLY-MARKER-q7"
PROMPT_MARKER = "PROMPT-MARKER-k3"
EVIDENCE_ID = "ev-MARKER-z9"
UNKNOWN_ID = "ev-UNKNOWN-MARKER-w2"
PROVIDER_MARKER = "PROVIDER-ERROR-MARKER-p4"
MODEL = "model-marker"
HOST = "llm-marker-h5.example.org"
URL = f"https://{HOST}/v1"
MARKERS = (
    REPLY_MARKER,
    PROMPT_MARKER,
    EVIDENCE_ID,
    UNKNOWN_ID,
    PROVIDER_MARKER,
    MODEL,
    HOST,
    URL,
    "https://",
)
DONE = "data: [DONE]\n\n"


def _reply(ids: list[str]) -> str:
    return json.dumps(
        {
            "recommendation": "INSUFFICIENT EVIDENCE",
            "rationale": f"r {REPLY_MARKER}",
            "evidence_ids": ids,
            "uncertainties": [],
        }
    )


def _event(content: str | None = None, finish: str | None = None) -> str:
    delta = {} if content is None else {"content": content}
    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(chunk)}\n\n"


USAGE = 'data: {"choices": [], "usage": {"completion_tokens": 12}}\n\n'
STREAMS = {
    "ok": _event(_reply([EVIDENCE_ID])) + _event(None, "stop") + USAGE + DONE,
    "token_limit": _event(f'{{"rationale": "{REPLY_MARKER}') + USAGE + _event(None, "length"),
    "schema": _event(f"not json {REPLY_MARKER} {EVIDENCE_ID}")
    + _event(None, "stop")
    + USAGE
    + DONE,
    "unknown_evidence_id": _event(_reply([EVIDENCE_ID, UNKNOWN_ID]))
    + _event(None, "stop")
    + USAGE
    + DONE,
    "provider_error": _event(REPLY_MARKER)
    + USAGE
    + 'data: {"error": {"message": "'
    + f"{PROVIDER_MARKER} {MODEL} {URL}"
    + '"}}\n\n'
    + DONE,
}


def _provider(stream: str) -> OpenAICompatibleProvider:
    """The real adapter (a remote https host) with an injected transport: no network."""
    return OpenAICompatibleProvider(
        MODEL,
        URL,
        allowed_hosts=frozenset({HOST}),
        transport=lambda url, body, timeout: stream.encode(),
    )


def _run(module: Any, outcome: str, caplog: pytest.LogCaptureFixture) -> str:
    """One call through `module.call_structured`; checks the single record's shape and returns
    the scan text of EVERY record captured during the call."""
    provider = _provider(STREAMS[outcome])
    caplog.clear()
    try:
        with caplog.at_level(logging.DEBUG):
            call = module.call_structured(
                provider,
                task="risk_assessment",
                user_message=f"plan {PROMPT_MARKER} evidence {EVIDENCE_ID}",
                output=ProposalOutput,
                allowed_ids=frozenset({EVIDENCE_ID}),
            )
            if outcome == "ok":
                asyncio.run(call)
            else:
                with pytest.raises(module.AgentError) as caught:
                    asyncio.run(call)
                assert caught.value.__context__ is None
    finally:
        provider.close()
    records = [r for r in caplog.records if r.getMessage() == "model call finished"]
    assert len(records) == 1
    record = records[0]
    assert record.outcome == outcome  # type: ignore[attr-defined]
    assert record.outcome in runner.CALL_OUTCOMES  # type: ignore[attr-defined]
    for name in ("output_limit", "output_chars", "output_used", "duration_ms"):
        value = getattr(record, name)
        assert value is None or type(value) is int, (name, value)
    assert type(record.duration_ms) is int  # type: ignore[attr-defined]
    return scanned_records(caplog.records)


def _found(text: str) -> list[str]:
    return [marker for marker in MARKERS if marker in text]


@pytest.mark.parametrize("outcome", list(STREAMS))
def test_no_record_holds_reply_prompt_evidence_id_provider_text_model_or_url(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert _found(_run(runner, outcome, caplog)) == []
    record = next(r for r in caplog.records if r.getMessage() == "model call finished")
    base = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
    assert set(vars(record)) - base == runner.CALL_LOG_FIELDS


def test_the_markers_reach_the_call_so_the_scan_is_meaningful() -> None:
    """Sanity: the accepted call really saw the reply and the evidence id, and the provider-error
    stream really is refused by the adapter (not some other path)."""
    provider = _provider(STREAMS["ok"])
    try:
        parsed = asyncio.run(
            runner.call_structured(
                provider,
                task="risk_assessment",
                user_message=f"plan {PROMPT_MARKER}",
                output=ProposalOutput,
                allowed_ids=frozenset({EVIDENCE_ID}),
            )
        )
    finally:
        provider.close()
    assert REPLY_MARKER in parsed.rationale and parsed.evidence_ids == (EVIDENCE_ID,)
    provider = _provider(STREAMS["provider_error"])
    try:
        with pytest.raises(runner.AgentError) as caught:
            asyncio.run(
                runner.call_structured(
                    provider,
                    task="t",
                    user_message="m",
                    output=ProposalOutput,
                    allowed_ids=frozenset(),
                )
            )
    finally:
        provider.close()
    assert str(caught.value) == "t: provider error (OpenAICompatibleError)"


# ---- mutation checks: the scan catches a reply or a prompt in the record -----------------------

# Each target appears exactly once in `runner` (`mutated_module` asserts that).
_COUNTS = "    counts = _response_counts(response)\n"
_LOGGER = "logger = logging.getLogger(__name__)\n"
_DURATION = '            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),\n'
_STARTED = "    started = time.monotonic()\n"

# 1. Keep the reply text where the outcome is decided (`_call`), then log it.
REPLY_EDITS = (
    (_LOGGER, _LOGGER + "_LEAKED: list[str] = []\n"),
    (_COUNTS, _COUNTS + "    _LEAKED.append(response.text)\n"),
    (_DURATION, _DURATION + '            "reply_text": list(_LEAKED),\n'),
)
# 2. Keep the prompt (`user_message`) in `call_structured`, then log it.
PROMPT_EDITS = (
    (_LOGGER, _LOGGER + "_LEAKED: list[str] = []\n"),
    (_STARTED, _STARTED + "    _LEAKED.append(user_message)\n"),
    (_DURATION, _DURATION + '            "prompt_text": list(_LEAKED),\n'),
)


def _mutant(edits: tuple[tuple[str, str], ...]) -> Any:
    mutant = mutated_module(runner, *edits)
    mutant.logger = logging.getLogger(mutant.__name__)
    return mutant


@pytest.mark.parametrize("outcome", ["ok", "schema", "unknown_evidence_id"])
def test_a_mutant_that_logs_the_reply_text_is_caught(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert REPLY_MARKER in _found(_run(_mutant(REPLY_EDITS), outcome, caplog))
    assert _found(_run(runner, outcome, caplog)) == []


@pytest.mark.parametrize("outcome", list(STREAMS))
def test_a_mutant_that_logs_the_prompt_is_caught(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    found = _found(_run(_mutant(PROMPT_EDITS), outcome, caplog))
    assert PROMPT_MARKER in found and EVIDENCE_ID in found
    assert _found(_run(runner, outcome, caplog)) == []
