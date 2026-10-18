"""The egress gate for live evaluation runs (ADR 0023): the same check as start-up."""

from __future__ import annotations

import pytest

from divesafe.config import Settings
from divesafe.models.egress import EgressBasis, check_egress
from divesafe.models.llm import LLMProvider


def tide_check_allowed(basis: EgressBasis, *, phn_terms_acknowledged: bool) -> bool:
    """The recorded PHN tide item goes to the model in the tide check. PHN's terms are not on file
    (ADR 0021), so it runs only against a server declared local, or with an explicit
    acknowledgement. The egress opt-in covers where the prompt goes, not the data licence."""
    return basis == "declared_local" or phn_terms_acknowledged


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
    except RuntimeError as exc:
        pytest.skip(str(exc))
