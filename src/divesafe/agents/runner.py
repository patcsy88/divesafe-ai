"""Runs one bounded, validated LLM call.

Fail-closed contract: any problem (provider error, timeout, oversized or malformed output,
unknown or missing evidence ids) raises `AgentError`. Output is never repaired or partially
used. Provider error text is not propagated (it may contain prompt content).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from divesafe.agents.prompts import SYSTEM_PROMPT
from divesafe.domain import MAX_EVIDENCE_ITEMS as _MAX_EVIDENCE_ITEMS
from divesafe.models import ChatMessage, LLMProvider, LLMRequest
from divesafe.models.llm import TokenLimitError, plain_count
from divesafe.observability import record_model_call

logger = logging.getLogger(__name__)

CALL_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_TOKENS = 1500
MAX_OUTPUT_TOKENS_CEILING = 8000
MAX_OUTPUT_CHARS = 20_000
MAX_EVIDENCE_ITEMS = _MAX_EVIDENCE_ITEMS  # one source: the record's per-stage bound
MAX_PROMPT_CHARS = 200_000
STAGE_TIMEOUT_SECONDS = 150.0


CALL_TIMEOUT_CEILING_SECONDS = 600.0
STAGE_TIMEOUT_CEILING_SECONDS = 1800.0


def _declared(provider: object, attribute: str, default: float, ceiling: float) -> float:
    """A provider's declared budget, or the default when it is missing, not a plain positive
    finite number, or absurd. A budget above the ceiling is capped, never trusted."""
    value = getattr(provider, attribute, None)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    try:
        number = float(value)
    except OverflowError:
        return default
    return min(number, ceiling) if 0 < number < float("inf") else default


def call_timeout(provider: object) -> float:
    """A provider may declare a longer budget for one call (a slow self-hosted model)."""
    return _declared(
        provider, "call_timeout_seconds", CALL_TIMEOUT_SECONDS, CALL_TIMEOUT_CEILING_SECONDS
    )


def stage_timeout(provider: object) -> float:
    return _declared(
        provider, "stage_timeout_seconds", STAGE_TIMEOUT_SECONDS, STAGE_TIMEOUT_CEILING_SECONDS
    )


def output_tokens(provider: object) -> int:
    """The output-token cap for one call. A reasoning model spends part of it thinking, so a
    provider may declare a larger one; anything that is not a whole number in range is ignored
    and a value above the ceiling is capped."""
    value = getattr(provider, "max_output_tokens", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return MAX_OUTPUT_TOKENS
    return min(value, MAX_OUTPUT_TOKENS_CEILING)


_FENCE = "```"

T = TypeVar("T", bound=BaseModel)


AgentErrorKind = Literal["failed", "timed_out", "rejected", "skipped"]


class AgentError(Exception):
    """An agent call failed or its output was rejected. `kind` is a fixed label the callers may
    branch on; the message text is never used for that. `label` is a fixed reason for a
    rejection (`schema`, `unknown_evidence_id`, `too_long`); empty when there is none."""

    def __init__(self, message: str, *, kind: AgentErrorKind = "failed", label: str = "") -> None:
        super().__init__(message)
        self.kind: AgentErrorKind = kind
        self.label: str = label


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A reply that repeats a key is ambiguous; it is refused, never resolved to the last value."""
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate key")
    return dict(pairs)


def _json_text(raw: str) -> str:
    text = raw.strip()
    # String handling, not a regex: overlapping whitespace repeats can backtrack for minutes.
    if len(text) >= 2 * len(_FENCE) and text.startswith(_FENCE) and text.endswith(_FENCE):
        inner = text[len(_FENCE) : -len(_FENCE)]
        return inner.removeprefix("json").strip()
    return text


# The fixed `outcome` labels of the "model call finished" log line (ADR 0018 amendment
# 2026-11-15): `ok` (accepted), `token_limit` (stopped at the output-token limit), `timed_out`,
# `provider_error`, the three rejections `too_long`, `schema` and `unknown_evidence_id`,
# `cancelled` (an `asyncio.CancelledError`: the stage deadline or shutdown) and `error` (any
# other unexpected exception, including a `BaseException` such as KeyboardInterrupt).
CallOutcome = Literal[
    "ok",
    "token_limit",
    "timed_out",
    "provider_error",
    "too_long",
    "schema",
    "unknown_evidence_id",
    "cancelled",
    "error",
]
CALL_OUTCOMES: frozenset[str] = frozenset(CallOutcome.__args__)  # type: ignore[attr-defined]
# Exactly the `extra` keys of that line. Nothing else: never a prompt, reply or partial text,
# header, key, URL, evidence id, plan field or provider error message. `output_limit` is the
# output-token limit sent with the request and `output_used` the server's reported output
# tokens (`usage.completion_tokens`), both counted in tokens. The names avoid "token" because the
# JSON formatter masks any field whose name matches `config.logging._SENSITIVE`.
CALL_LOG_FIELDS: frozenset[str] = frozenset(
    {"task", "outcome", "output_limit", "output_chars", "output_used", "duration_ms"}
)


Counts = tuple[int | None, int | None, int | None]
NO_COUNTS: Counts = (None, None, None)


def _response_counts(response: object) -> Counts:
    """(characters, server output tokens, server input tokens) of a response; None for any count
    not readable."""
    try:
        text = getattr(response, "text", None)
        chars = plain_count(len(text)) if type(text) is str else None
        return (
            chars,
            plain_count(getattr(response, "output_tokens", None)),
            plain_count(getattr(response, "input_tokens", None)),
        )
    except Exception:
        return NO_COUNTS


def _token_limit_counts(exc: BaseException) -> Counts:
    """(characters joined before the stop, server output tokens, server input tokens) carried by
    a token-limit error; None for any count not readable. Reads plain attributes only, never
    text."""
    try:
        state = vars(exc)
        return (
            plain_count(state.get("output_chars")),
            plain_count(state.get("output_tokens")),
            plain_count(state.get("input_tokens")),
        )
    except Exception:
        return NO_COUNTS


def _log_call(
    task: str,
    outcome: CallOutcome,
    *,
    output_limit: int | None,
    output_chars: int | None,
    output_used: int | None,
    input_used: int | None,
    started: float,
    started_ns: int,
) -> None:
    """One INFO line per model call, with counts only. `output_limit` and `output_used` are
    counts of tokens, `output_chars` of characters. The same fields, plus `input_used` (the
    server's input tokens, span-only: never on the log line), go to the run's trace collector,
    if any (ADR 0027 amendment). Never raises: a logging or tracing failure is dropped."""
    fields: dict[str, object] | None = None
    with contextlib.suppress(Exception):  # logging must never change the call's result
        fields = {
            "task": task,
            "outcome": outcome,
            "output_limit": plain_count(output_limit),
            "output_chars": plain_count(output_chars),
            "output_used": plain_count(output_used),
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        }
        logger.info("model call finished", extra=fields)
    if fields is not None:
        with contextlib.suppress(Exception):  # tracing must never change the call's result
            record_model_call({**fields, "input_used": plain_count(input_used)}, started_ns)


async def call_structured(
    provider: LLMProvider,
    *,
    task: str,
    user_message: str,
    output: type[T],
    allowed_ids: frozenset[str],
    timeout: float | None = None,
) -> T:
    if len(user_message) > MAX_PROMPT_CHARS:
        raise AgentError(f"{task}: prompt too large", kind="rejected")
    request = LLMRequest(
        messages=(
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_message),
        ),
        temperature=0.0,
        max_output_tokens=output_tokens(provider),
        json_schema=output.model_json_schema(),
    )
    # Defaults hold if the call ends some other way (a BaseException such as KeyboardInterrupt):
    # it is still logged, and never swallowed.
    outcome: CallOutcome = "error"
    chars: int | None = None
    used: int | None = None
    used_in: int | None = None
    error: AgentError | None = None
    started = time.monotonic()
    started_ns = time.time_ns()  # the span's wall-clock start only; never on the record
    try:
        try:
            parsed, (chars, used, used_in) = await _call(
                provider, request, task, output, allowed_ids, timeout
            )
            outcome = "ok"
        except _Finished as finished:
            outcome = finished.outcome
            chars, used, used_in = finished.counts
            error = finished.error
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
    finally:
        _log_call(
            task,
            outcome,
            output_limit=request.max_output_tokens,
            output_chars=chars,
            output_used=used,
            input_used=used_in,
            started=started,
            started_ns=started_ns,
        )
    # Raised outside the `except`, so `__context__` never holds the reply (as before).
    if error is not None:
        raise error from None
    return parsed


class _Finished(Exception):
    """Internal: carries a call's outcome label and its counts (plain ints, never the response
    or the provider's exception) to the log line, with the `AgentError` the caller sees."""

    def __init__(
        self,
        outcome: CallOutcome,
        error: AgentError,
        counts: Counts = NO_COUNTS,
    ) -> None:
        super().__init__()
        self.outcome: CallOutcome = outcome
        self.counts = counts
        self.error = error


async def _call(
    provider: LLMProvider,
    request: LLMRequest,
    task: str,
    output: type[T],
    allowed_ids: frozenset[str],
    timeout: float | None,
) -> tuple[T, Counts]:
    """The validated output, with the reply's character count and the server's output and input
    token counts (for the log line and the trace; counts only, never the response)."""
    # The errors below are raised outside their `except` blocks, so `__context__` never holds a
    # provider's or a model's text.
    failure: _Finished | None = None
    try:
        response = await asyncio.wait_for(
            provider.complete(request),
            timeout=timeout if timeout is not None else call_timeout(provider),
        )
    except TimeoutError:
        failure = _Finished(
            "timed_out", AgentError(f"{task}: provider timed out", kind="timed_out")
        )
    except TokenLimitError as exc:
        failure = _Finished(
            "token_limit",
            AgentError(f"{task}: output stopped at the token limit"),
            _token_limit_counts(exc),
        )
    except Exception as exc:  # provider adapters may raise anything; never chain their text
        failure = _Finished(
            "provider_error", AgentError(f"{task}: provider error ({type(exc).__name__})")
        )
    if failure is not None:
        raise failure from None

    counts = _response_counts(response)
    if len(response.text) > MAX_OUTPUT_CHARS:
        raise _Finished(
            "too_long",
            AgentError(f"{task}: output too long", kind="rejected", label="too_long"),
            counts,
        )
    parsed: T | None = None
    try:
        parsed = output.model_validate(
            json.loads(_json_text(response.text), object_pairs_hook=_no_duplicate_keys)
        )
    except (json.JSONDecodeError, ValueError, ValidationError, RecursionError):
        parsed = None
    if parsed is None:
        raise _Finished(
            "schema",
            AgentError(
                f"{task}: output rejected (not valid for the schema)",
                kind="rejected",
                label="schema",
            ),
            counts,
        ) from None

    cited: tuple[str, ...] = getattr(parsed, "evidence_ids", ())
    unknown = set(cited) - allowed_ids
    if unknown:
        raise _Finished(
            "unknown_evidence_id",
            AgentError(
                f"{task}: output cites {len(unknown)} unknown evidence id(s)",
                kind="rejected",
                label="unknown_evidence_id",
            ),
            counts,
        )
    return parsed, counts
