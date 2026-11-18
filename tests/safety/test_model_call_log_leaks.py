"""The "model call finished" log line carries counts and fixed labels only (AGENTS.md section 3:
never log prompts or free-text content; ADR 0018 amendment 2026-11-15).

Calls run through the real adapter (remote https host, injected transport, no network) for five
outcomes: accepted, token limit, schema rejection, unknown evidence id and a provider error event.
Every captured record is scanned (every attribute except the runtime numbers `logging` adds,
`tests.safety.log_fields`) for markers placed in the reply, the prompt, an evidence id, the
provider's error text, the model name and the server URL. Mutants of `runner` (one logs the
reply text, one logs the prompt, others pass a provider's free-text `reason` through either label
filter) prove the scan has teeth. The provider failure label set is pinned as a literal here.
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
from divesafe.models.llm import PROVIDER_FAILURE_REASONS, ProviderError
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


def test_the_real_adapters_provider_error_logs_its_fixed_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _run(runner, "provider_error", caplog)
    record = next(r for r in caplog.records if r.getMessage() == "model call finished")
    assert record.provider_reason == "error_event"  # type: ignore[attr-defined]
    assert record.provider_reason in PROVIDER_FAILURE_REASONS  # type: ignore[attr-defined]


@pytest.mark.parametrize("outcome", [o for o in STREAMS if o != "provider_error"])
def test_no_other_outcome_logs_a_provider_label(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    _run(runner, outcome, caplog)
    record = next(r for r in caplog.records if r.getMessage() == "model call finished")
    assert record.provider_reason is None  # type: ignore[attr-defined]


class _MarkedProvider:
    """Raises a `ProviderError` whose `reason` is free text, as a careless adapter might."""

    name, model, external = "stub", "m", False

    async def complete(self, request: object) -> object:
        raise ProviderError("fixed", reason=PROVIDER_MARKER)


def _marked_reason(module: Any, caplog: pytest.LogCaptureFixture) -> tuple[object, list[str]]:
    """The logged label and the markers found in every record, for one call that raises a
    `ProviderError` with free text as its `reason`."""
    caplog.clear()
    with caplog.at_level(logging.DEBUG), pytest.raises(module.AgentError):
        asyncio.run(
            module.call_structured(
                _MarkedProvider(),
                task="t",
                user_message="m",
                output=ProposalOutput,
                allowed_ids=frozenset(),
            )
        )
    record = next(r for r in caplog.records if r.getMessage() == "model call finished")
    return record.provider_reason, _found(scanned_records(caplog.records))


def test_a_free_text_reason_is_logged_as_unclassified(caplog: pytest.LogCaptureFixture) -> None:
    assert _marked_reason(runner, caplog) == ("unclassified", [])


# The raw `reason` at the call site, bypassing `provider_failure_reason`.
_RAW_REASON = (
    "            reason=provider_failure_reason(exc),\n",
    '            reason=vars(exc).get("reason"),\n',
)
# The log line's own filter, passing any reason through.
_ANY_REASON = (
    "    if type(reason) is str and reason in PROVIDER_FAILURE_REASONS:\n"
    "        return next(label for label in PROVIDER_FAILURE_REASONS if label == reason)\n",
    "    if reason is not None:\n        return str(reason)\n",
)


# Literal, never imported (as the tracing export pins): a new label is a reviewed change here.
PINNED_REASONS = frozenset(
    {
        "unreachable",
        "read_timeout",
        "connection_error",
        "http_error",
        "deadline",
        "too_large",
        "slots_full",
        "closed",
        "bad_event_json",
        "error_event",
        "no_done_marker",
        "data_after_done",
        "event_line",
        "tool_or_refusal",
        "bad_shape",
        "empty_stream",
        "token_limit",
        "unclassified",
    }
)


def test_the_provider_failure_labels_equal_the_pinned_literal() -> None:
    from divesafe.models import openai_compatible

    assert PROVIDER_FAILURE_REASONS == PINNED_REASONS
    assert openai_compatible.FAILURE_REASONS is PROVIDER_FAILURE_REASONS


def test_the_call_site_filter_holds_without_the_log_lines_filter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With only `_provider_reason` widened, the call-site filter still holds; the marker leaks
    only when the call site is widened too (below)."""
    assert _marked_reason(_mutant((_ANY_REASON,)), caplog) == ("unclassified", [])


def test_a_widened_call_site_filter_leaks_without_the_log_lines_filter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With `_provider_reason` widened, a widened `provider_failure_reason` leaks the marker, so
    the call-site filter is what stops it in the test above."""
    from divesafe.models import llm

    widened = mutated_module(
        llm,
        (
            "            if type(reason) is str and reason in PROVIDER_FAILURE_REASONS:\n"
            "                return next(label for label in PROVIDER_FAILURE_REASONS if label == "
            "reason)\n",
            "            return reason  # type: ignore[no-any-return]\n",
        ),
    )
    mutant = _mutant((_ANY_REASON,))
    mutant.provider_failure_reason = widened.provider_failure_reason
    mutant.ProviderError = widened.ProviderError
    caplog.clear()

    class _Widened:
        name, model, external = "stub", "m", False

        async def complete(self, request: object) -> object:
            raise widened.ProviderError("fixed", reason=PROVIDER_MARKER)

    with caplog.at_level(logging.DEBUG), pytest.raises(mutant.AgentError):
        asyncio.run(
            mutant.call_structured(
                _Widened(),
                task="t",
                user_message="m",
                output=ProposalOutput,
                allowed_ids=frozenset(),
            )
        )
    assert PROVIDER_MARKER in _found(scanned_records(caplog.records))


def test_a_mutant_without_either_filter_is_caught(caplog: pytest.LogCaptureFixture) -> None:
    reason, found = _marked_reason(_mutant((_RAW_REASON, _ANY_REASON)), caplog)
    assert reason == PROVIDER_MARKER and PROVIDER_MARKER in found


def test_the_log_lines_own_filter_holds_when_the_call_site_passes_raw_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _marked_reason(_mutant((_RAW_REASON,)), caplog) == ("unclassified", [])


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
