"""Runs one bounded, validated LLM call.

Fail-closed contract: any problem (provider error, timeout, oversized or malformed output,
unknown or missing evidence ids) raises `AgentError`. Output is never repaired or partially
used. Provider error text is not propagated (it may contain prompt content).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from divesafe.agents.prompts import SYSTEM_PROMPT
from divesafe.domain import MAX_EVIDENCE_ITEMS as _MAX_EVIDENCE_ITEMS
from divesafe.models import ChatMessage, LLMProvider, LLMRequest
from divesafe.models.llm import TokenLimitError

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


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

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


def _json_text(raw: str) -> str:
    text = raw.strip()
    fenced = _FENCE.match(text)
    return fenced.group(1) if fenced else text


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
    try:
        response = await asyncio.wait_for(
            provider.complete(request),
            timeout=timeout if timeout is not None else call_timeout(provider),
        )
    except TimeoutError:
        raise AgentError(f"{task}: provider timed out", kind="timed_out") from None
    except TokenLimitError:
        raise AgentError(f"{task}: output stopped at the token limit") from None
    except Exception as exc:  # provider adapters may raise anything; never chain their text
        raise AgentError(f"{task}: provider error ({type(exc).__name__})") from None

    if len(response.text) > MAX_OUTPUT_CHARS:
        raise AgentError(f"{task}: output too long", kind="rejected", label="too_long")
    try:
        parsed = output.model_validate(json.loads(_json_text(response.text)))
    except (json.JSONDecodeError, ValidationError, RecursionError) as exc:
        raise AgentError(
            f"{task}: output rejected (not valid for the schema)",
            kind="rejected",
            label="schema",
        ) from exc

    cited: tuple[str, ...] = getattr(parsed, "evidence_ids", ())
    unknown = set(cited) - allowed_ids
    if unknown:
        raise AgentError(
            f"{task}: output cites {len(unknown)} unknown evidence id(s)",
            kind="rejected",
            label="unknown_evidence_id",
        )
    return parsed
