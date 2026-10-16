"""Live evaluation against a real model server. Skipped unless you opt in:

    DIVESAFE_EVAL_OLLAMA_MODEL=<model> .venv/bin/pytest tests/evaluation/test_evaluation_live.py -s

Set DIVESAFE_EVAL_PROVIDER=openai_compatible to use the OpenAI-compatible adapter (ADR 0018) with
DIVESAFE_EVAL_MODEL, DIVESAFE_EVAL_BASE_URL (for example http://localhost:8000/v1), and for a
remote server DIVESAFE_EVAL_ALLOWED_HOSTS and DIVESAFE_EVAL_API_KEY (https only).

It reports metrics and writes raw records (ADR 0016). Hard assertions: the model produced at least
one usable proposal (otherwise nothing was measured), no under-severe result, and exact rules.
Quality numbers are reported, not gated. All scenarios are synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from tests.evaluation.harness import evaluate

from divesafe.agents import PROMPT_VERSION
from divesafe.config import Settings
from divesafe.models.ollama import OllamaProvider
from divesafe.models.openai_compatible import OpenAICompatibleProvider

PROVIDER = os.environ.get("DIVESAFE_EVAL_PROVIDER", "ollama")
# Read at import, before the per-test environment isolation in tests/conftest.py removes it.
ALLOW_EXTERNAL = os.environ.get("DIVESAFE_ALLOW_EXTERNAL_LLM") == "true"
MODEL = os.environ.get("DIVESAFE_EVAL_MODEL") or os.environ.get("DIVESAFE_EVAL_OLLAMA_MODEL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        MODEL is None, reason="DIVESAFE_EVAL_MODEL (or DIVESAFE_EVAL_OLLAMA_MODEL) not set"
    ),
]


def _sha() -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def test_a_local_model_cannot_lower_the_result_and_its_quality_is_reported(
    tmp_path: Path,
) -> None:
    assert MODEL is not None
    assert PROVIDER in ("ollama", "openai_compatible"), "DIVESAFE_EVAL_PROVIDER is not known"
    env = os.environ.get
    budgets = Settings(
        _env_file=None,
        llm_call_timeout_seconds=env("DIVESAFE_EVAL_CALL_TIMEOUT", "60"),  # type: ignore[arg-type]
        llm_stage_timeout_seconds=env("DIVESAFE_EVAL_STAGE_TIMEOUT", "150"),  # type: ignore[arg-type]
        llm_first_byte_timeout_seconds=env("DIVESAFE_EVAL_FIRST_BYTE", "15"),  # type: ignore[arg-type]
        llm_max_concurrent_calls=env("DIVESAFE_EVAL_CONCURRENCY", "4"),  # type: ignore[arg-type]
        llm_max_output_tokens=env("DIVESAFE_EVAL_MAX_OUTPUT_TOKENS", "1500"),  # type: ignore[arg-type]
        ollama_plain_http_hosts=env("DIVESAFE_EVAL_OLLAMA_PLAIN_HTTP_HOSTS"),
        llm_allowed_hosts=env("DIVESAFE_EVAL_ALLOWED_HOSTS"),
    )
    options: dict[str, object] = {
        "call_timeout_seconds": budgets.llm_call_timeout_seconds,
        "stage_timeout_seconds": budgets.llm_stage_timeout_seconds,
        "first_byte_timeout_seconds": budgets.llm_first_byte_timeout_seconds,
        "max_concurrent_calls": budgets.llm_max_concurrent_calls,
        "max_output_tokens": budgets.llm_max_output_tokens,
    }
    if PROVIDER == "ollama":
        base = env("DIVESAFE_EVAL_OLLAMA_URL", "http://localhost:11434")
        options["plain_http_hosts"] = budgets.plain_http_host_set()

        def make() -> OllamaProvider | OpenAICompatibleProvider:
            return OllamaProvider(MODEL, base, **options)  # type: ignore[arg-type]
    else:
        base = env("DIVESAFE_EVAL_BASE_URL", "http://localhost:8000/v1")
        key = env("DIVESAFE_EVAL_API_KEY")
        options["allowed_hosts"] = budgets.llm_allowed_host_set()
        options["api_key"] = SecretStr(key) if key else None

        def make() -> OllamaProvider | OpenAICompatibleProvider:
            return OpenAICompatibleProvider(MODEL, base, **options)  # type: ignore[arg-type]

    if make().external and not ALLOW_EXTERNAL:
        pytest.skip("a remote server needs DIVESAFE_ALLOW_EXTERNAL_LLM=true")
    report = evaluate(make)
    out = Path(os.environ.get("DIVESAFE_EVAL_OUT", str(tmp_path))) / f"evaluation_{PROVIDER}.json"
    out.write_text(
        json.dumps(
            {
                "provider": PROVIDER,
                "model": MODEL,
                "model_digest": "not recorded (name only)",
                "temperature": 0.0,
                "run_at": datetime.now(UTC).isoformat(),
                "code_sha": _sha(),
                "prompt_version": PROMPT_VERSION,
                "budgets": {
                    "max_output_tokens": budgets.llm_max_output_tokens,
                    "call_timeout_seconds": budgets.llm_call_timeout_seconds,
                    "stage_timeout_seconds": budgets.llm_stage_timeout_seconds,
                    "first_byte_timeout_seconds": budgets.llm_first_byte_timeout_seconds,
                    "max_concurrent_calls": budgets.llm_max_concurrent_calls,
                },
                "summary": report.summary(),
                "cases": [c.raw_record for c in report.cases],
            },
            indent=1,
        )
    )
    print("\nEVALUATION", MODEL, report.summary(), "raw records:", out)
    assert report.proposals_obtained >= 1, "model unusable: no case produced a valid proposal"
    assert report.under_severe_errors == 0
    assert report.rule_outcome_accuracy == 1.0
