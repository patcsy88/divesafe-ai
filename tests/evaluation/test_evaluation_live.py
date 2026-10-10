"""Live evaluation against a local Ollama model. Skipped unless you opt in:

    DIVESAFE_EVAL_OLLAMA_MODEL=<model> .venv/bin/pytest tests/evaluation/test_evaluation_live.py -s

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
from tests.evaluation.harness import evaluate

from divesafe.models.ollama import OllamaProvider

MODEL = os.environ.get("DIVESAFE_EVAL_OLLAMA_MODEL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(MODEL is None, reason="DIVESAFE_EVAL_OLLAMA_MODEL not set"),
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
    base = os.environ.get("DIVESAFE_EVAL_OLLAMA_URL", "http://localhost:11434")
    report = evaluate(lambda: OllamaProvider(MODEL, base))
    out = Path(os.environ.get("DIVESAFE_EVAL_OUT", str(tmp_path))) / "evaluation_ollama.json"
    out.write_text(
        json.dumps(
            {
                "model": MODEL,
                "model_digest": "not recorded (name only)",
                "temperature": 0.0,
                "run_at": datetime.now(UTC).isoformat(),
                "code_sha": _sha(),
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
