"""The egress gate for live evaluation runs (ADR 0023): the same check as start-up."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from divesafe.config import Settings
from divesafe.models.egress import EgressBasis, check_egress
from divesafe.models.llm import LLMProvider


def tide_check_allowed(basis: EgressBasis, *, phn_terms_acknowledged: bool) -> bool:
    """The recorded PHN tide item goes to the model in the tide check. PHN's terms are not on file
    (ADR 0021), so it runs only against a server declared local, or with an explicit
    acknowledgement. The egress opt-in covers where the prompt goes, not the data licence."""
    # "none" (the exact fake class) cannot reach the live module, which builds only real adapters;
    # it is refused here too, so a basis added later is refused until someone decides otherwise.
    return basis == "declared_local" or phn_terms_acknowledged


def output_path(directory: Path, name: str) -> Path:
    """A path that does not exist yet. A repeat run, or a skipped check, must never replace an
    earlier raw record, because the record of a run may cite that file's hash."""
    path = directory / name
    if not path.exists():
        return path
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return directory / f"{path.stem}_{stamp}{path.suffix}"


def egress_gate(
    provider: LLMProvider, *, allow_external: bool | str, server_is_local: bool | str
) -> EgressBasis:
    """Skip the run unless the operator has said where the prompt goes. Both flags together are
    a configuration error and fail the run rather than skip it. Text is read by `Settings`, so
    `1`, `TRUE` and `true` mean the same here as at start-up and an unreadable value fails."""
    settings = Settings(
        _env_file=None,
        allow_external_llm=allow_external,  # type: ignore[arg-type]
        llm_server_is_local=server_is_local,  # type: ignore[arg-type]
    )
    try:
        return check_egress(provider, settings)
    except RuntimeError:
        # F5: a fixed refusal. The exception text can echo configuration, so it is not used.
        pytest.skip("egress refused: the LLM server is not declared (see ADR 0023)")
