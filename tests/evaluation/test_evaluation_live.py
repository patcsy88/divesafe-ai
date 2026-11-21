"""Live evaluation against a real model server. Skipped unless you opt in:

    DIVESAFE_EVAL_MODEL=<model> .venv/bin/pytest tests/evaluation/test_evaluation_live.py -s

It uses the OpenAI-compatible adapter (ADR 0018) with DIVESAFE_EVAL_MODEL,
DIVESAFE_EVAL_BASE_URL (for example http://localhost:8000/v1), and for a remote server
DIVESAFE_EVAL_ALLOWED_HOSTS and DIVESAFE_EVAL_API_KEY (https only). Set
DIVESAFE_EVAL_OUT to a directory outside the repository: it holds raw model replies, and
.gitignore covers the default file names only as a backstop.

It reports metrics and writes raw records (ADR 0016). Hard assertions: the model produced at least
one usable proposal (otherwise nothing was measured), no under-severe result, and exact rules.
Quality numbers are reported, not gated. All scenarios are synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest
from pydantic import SecretStr
from tests.evaluation.egress_gate import (
    egress_gate,
    eval_output_dir,
    output_path,
    write_private,
)
from tests.evaluation.harness import evaluate

from divesafe.agents import PROMPT_VERSION
from divesafe.config import Settings
from divesafe.config.settings import base_url_host
from divesafe.models.openai_compatible import OpenAICompatibleProvider

PROVIDER = os.environ.get("DIVESAFE_EVAL_PROVIDER", "openai_compatible")
# Read at import, before the per-test environment isolation in tests/conftest.py removes it.
ALLOW_EXTERNAL = os.environ.get("DIVESAFE_ALLOW_EXTERNAL_LLM") or "false"
SERVER_IS_LOCAL = os.environ.get("DIVESAFE_LLM_SERVER_IS_LOCAL") or "false"
MODEL = os.environ.get("DIVESAFE_EVAL_MODEL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(MODEL is None, reason="DIVESAFE_EVAL_MODEL not set"),
]


def _sha() -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _host() -> str:
    """The model server's host name only: no scheme, path, port or credentials."""
    url = os.environ.get("DIVESAFE_EVAL_BASE_URL", "http://localhost:8000/v1")
    return urlparse(url).hostname or "unknown"


def eval_base_url() -> str:
    return os.environ.get("DIVESAFE_EVAL_BASE_URL", "http://localhost:8000/v1")


def eval_model_host() -> str | None:
    """ADR 0030 decision 3: the host every live runner passes as `model_host`, by the function
    `check_egress` uses (never via `api.state`). None fails the run in the runner."""
    return base_url_host(eval_base_url())


def provider_builder() -> tuple[Settings, Callable[[str], OpenAICompatibleProvider]]:
    """The one builder for every live provider (ADR 0030 decision 3): the evaluation's base URL,
    allow-list, key and budgets, with the model id as the only varying argument."""
    env = os.environ.get
    budgets = Settings(
        _env_file=None,
        llm_call_timeout_seconds=env("DIVESAFE_EVAL_CALL_TIMEOUT", "60"),  # type: ignore[arg-type]
        llm_stage_timeout_seconds=env("DIVESAFE_EVAL_STAGE_TIMEOUT", "150"),  # type: ignore[arg-type]
        llm_first_byte_timeout_seconds=env("DIVESAFE_EVAL_FIRST_BYTE", "15"),  # type: ignore[arg-type]
        llm_max_concurrent_calls=env("DIVESAFE_EVAL_CONCURRENCY", "4"),  # type: ignore[arg-type]
        llm_max_output_tokens=env("DIVESAFE_EVAL_MAX_OUTPUT_TOKENS", "1500"),  # type: ignore[arg-type]
        llm_allowed_hosts=env("DIVESAFE_EVAL_ALLOWED_HOSTS"),
    )
    options: dict[str, object] = {
        "call_timeout_seconds": budgets.llm_call_timeout_seconds,
        "stage_timeout_seconds": budgets.llm_stage_timeout_seconds,
        "first_byte_timeout_seconds": budgets.llm_first_byte_timeout_seconds,
        "max_concurrent_calls": budgets.llm_max_concurrent_calls,
        "max_output_tokens": budgets.llm_max_output_tokens,
    }
    options["allowed_hosts"] = budgets.llm_allowed_host_set()
    base = eval_base_url()
    key = env("DIVESAFE_EVAL_API_KEY")
    options["api_key"] = SecretStr(key) if key else None

    def make(model_id: str) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(model_id, base, **options)  # type: ignore[arg-type]

    return budgets, make


def gate(provider: OpenAICompatibleProvider) -> str:
    """`egress_gate` for one provider instance, from the evaluation's settings."""
    env = os.environ.get
    return egress_gate(
        provider,
        allow_external=ALLOW_EXTERNAL,
        server_is_local=SERVER_IS_LOCAL,
        base_url=eval_base_url(),
        allowed_hosts=env("DIVESAFE_EVAL_ALLOWED_HOSTS"),
        # ADR 0028: the evaluation's own knowledge host; unset keeps knowledge withheld.
        knowledge_host=env("DIVESAFE_EVAL_KNOWLEDGE_HOST"),
    )


def _setup() -> tuple[Settings, Callable[[], OpenAICompatibleProvider], str]:
    assert MODEL is not None
    assert PROVIDER == "openai_compatible", "DIVESAFE_EVAL_PROVIDER is not known"
    model = MODEL
    budgets, make_for = provider_builder()

    def make() -> OpenAICompatibleProvider:
        return make_for(model)

    basis = gate(make())
    return budgets, make, basis


def test_a_local_model_cannot_lower_the_result_and_its_quality_is_reported(
    tmp_path: Path,
) -> None:
    budgets, make, basis = _setup()
    out_dir = eval_output_dir(os.environ.get("DIVESAFE_EVAL_OUT"), tmp_path)
    report = evaluate(make, egress_basis=basis, model_host=eval_model_host())
    out = output_path(out_dir, f"evaluation_{PROVIDER}.json")
    write_private(
        out,
        json.dumps(
            {
                "provider": PROVIDER,
                "model": MODEL,
                "model_digest": "not recorded (name only)",
                "host": _host(),
                "egress_basis": basis,
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
        ),
    )
    print("\nEVALUATION", MODEL, report.summary(), "raw records:", out)
    assert report.proposals_obtained >= 1, "model unusable: no case produced a valid proposal"
    assert report.under_severe_errors == 0
    assert report.rule_outcome_accuracy == 1.0
