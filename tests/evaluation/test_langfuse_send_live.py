"""ADR 0027 §2's live check of the dataset sender, against the local Langfuse's `divesafe-eval`
project. Skipped unless you opt in, from the owner's shell, with a clean worktree:

    DIVESAFE_EVAL_MODEL=<model> DIVESAFE_EVAL_SECRETS_DIR=<dir outside the repo> \\
        .venv/bin/pytest tests/evaluation/test_langfuse_send_live.py -s

It needs the `[eval]` extra (`langfuse` at `LANGFUSE_VERIFIED_VERSION`). It is given `SCENARIOS`
itself with a case limit of one, so it sends the first case only, makes no judge call and sends
no completion marker: the run is incomplete in every report and is never cited. Every start-up
refusal is in force; there is no probe path. It prints the server version from the live check's
health call (JSON-quoted), for the record's `langfuse_server_version`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.evaluation import harness
from tests.evaluation.langfuse_sender import run_send
from tests.evaluation.test_evaluation_live import MODEL, eval_model_host, gate, provider_builder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(MODEL is None, reason="DIVESAFE_EVAL_MODEL not set"),
    pytest.mark.skipif(
        os.environ.get("DIVESAFE_EVAL_SECRETS_DIR") is None,
        reason="DIVESAFE_EVAL_SECRETS_DIR not set",
    ),
]


def test_the_first_scenario_is_sent_to_divesafe_eval_without_a_completion_marker(
    tmp_path: Path,
) -> None:
    assert MODEL is not None
    model = MODEL
    _, make_for = provider_builder()
    # The agents' basis is the gate's return value for a provider instance from the same builder.
    basis = gate(make_for(model))

    result = run_send(
        harness.SCENARIOS,
        lambda: make_for(model),
        None,
        egress_basis=basis,
        model_host=eval_model_host(),
        output_dir=Path(os.environ.get("DIVESAFE_EVAL_OUT") or tmp_path),
        case_limit=1,
    )

    assert result.confirmed_cases == 1
    assert result.expected_cases == len(harness.SCENARIOS)
    assert not result.complete
    # The record's `langfuse_server_version`: the health reply's version, which the sender has
    # already refused (`server_version`) unless it passes `langfuse_checks.is_server_version_shape`
    # and `langfuse_sender._version_holds_key` returns False (see both for the checks), so only a
    # version string is printed.
    version = result.langfuse_server_version
    assert version is not None
    print(
        f"langfuse send: run {result.run_name}, file {result.output_file}, "
        f"langfuse_server_version {json.dumps(version)}"
    )
