"""The egress gate for live evaluation runs (ADR 0023): the same check as start-up."""

from __future__ import annotations

import pytest

from divesafe.config import Settings
from divesafe.models.egress import EgressBasis, check_egress
from divesafe.models.llm import LLMProvider


def egress_gate(
    provider: LLMProvider, *, allow_external: bool, server_is_local: bool
) -> EgressBasis:
    """Skip the run unless the operator has said where the prompt goes. Both flags together are
    a configuration error and fail the run rather than skip it."""
    settings = Settings(
        _env_file=None,
        allow_external_llm=allow_external,
        llm_server_is_local=server_is_local,
    )
    try:
        return check_egress(provider, settings)
    except RuntimeError as exc:
        pytest.skip(str(exc))
