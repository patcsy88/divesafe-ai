"""Limits shared by the model adapters: response size, stream length and the time and token
budgets. They were first written for the Ollama adapter (ADR 0016, removed); the OpenAI-compatible
adapter (ADR 0018) uses them unchanged."""

from __future__ import annotations

MAX_RESPONSE_BYTES = 1024 * 1024
SOCKET_TIMEOUT_SECONDS = 15.0
MAX_STREAM_LINES = 20_000

MAX_CALL_TIMEOUT_SECONDS = 600.0
MAX_STAGE_TIMEOUT_SECONDS = 1800.0
MAX_FIRST_BYTE_TIMEOUT_SECONDS = 300.0
MAX_CONCURRENT_CEILING = 8


MAX_OUTPUT_TOKENS_CEILING = 8000


def check_output_tokens(tokens: int) -> None:
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        raise ValueError("the LLM output-token cap must be a whole number")
    if not 1 <= tokens <= MAX_OUTPUT_TOKENS_CEILING:
        raise ValueError("the LLM output-token cap is out of range")


def check_budgets(call: float, stage: float, first_byte: float, concurrent: int) -> None:
    numbers = (call, stage, first_byte)
    if any(isinstance(n, bool) or not isinstance(n, int | float) for n in numbers):
        raise ValueError("the LLM time budgets must be numbers")
    if isinstance(concurrent, bool) or not isinstance(concurrent, int):
        raise ValueError("the LLM concurrency must be a whole number")
    if not all(0 < n < float("inf") for n in numbers):
        raise ValueError("the LLM time budgets must be positive and finite")
    if (
        call > MAX_CALL_TIMEOUT_SECONDS
        or stage > MAX_STAGE_TIMEOUT_SECONDS
        or first_byte > MAX_FIRST_BYTE_TIMEOUT_SECONDS
        or not 1 <= concurrent <= MAX_CONCURRENT_CEILING
    ):
        raise ValueError("an LLM time budget or the concurrency is above its ceiling")
    if stage < call or call < 2 * first_byte + 1:
        raise ValueError(
            "the stage budget must cover one call, and a call must be at least twice the "
            "first-byte timeout plus 1 s"
        )
