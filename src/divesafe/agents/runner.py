"""Runs one bounded, validated LLM call.

Fail-closed contract: any problem (provider error, timeout, oversized or malformed output,
unknown or missing evidence ids) raises `AgentError`. Output is never repaired or partially
used. Provider error text is not propagated (it may contain prompt content).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from divesafe.agents.prompts import SYSTEM_PROMPT
from divesafe.models import ChatMessage, LLMProvider, LLMRequest

CALL_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_TOKENS = 1500
MAX_OUTPUT_CHARS = 20_000
MAX_EVIDENCE_ITEMS = 120
MAX_PROMPT_CHARS = 200_000
STAGE_TIMEOUT_SECONDS = 150.0

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

T = TypeVar("T", bound=BaseModel)


class AgentError(Exception):
    """An agent call failed or its output was rejected."""


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
    timeout: float = CALL_TIMEOUT_SECONDS,
) -> T:
    if len(user_message) > MAX_PROMPT_CHARS:
        raise AgentError(f"{task}: prompt too large")
    request = LLMRequest(
        messages=(
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_message),
        ),
        temperature=0.0,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        json_schema=output.model_json_schema(),
    )
    try:
        response = await asyncio.wait_for(provider.complete(request), timeout=timeout)
    except TimeoutError:
        raise AgentError(f"{task}: provider timed out") from None
    except Exception as exc:  # provider adapters may raise anything; never chain their text
        raise AgentError(f"{task}: provider error ({type(exc).__name__})") from None

    if len(response.text) > MAX_OUTPUT_CHARS:
        raise AgentError(f"{task}: output too long")
    try:
        parsed = output.model_validate(json.loads(_json_text(response.text)))
    except (json.JSONDecodeError, ValidationError, RecursionError) as exc:
        raise AgentError(f"{task}: output rejected (not valid for the schema)") from exc

    cited: tuple[str, ...] = getattr(parsed, "evidence_ids", ())
    unknown = set(cited) - allowed_ids
    if unknown:
        raise AgentError(f"{task}: output cites {len(unknown)} unknown evidence id(s)")
    return parsed
