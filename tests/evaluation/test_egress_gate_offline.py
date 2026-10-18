"""The live-evaluation egress gate refuses what start-up refuses (ADR 0023). Offline."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.evaluation.egress_gate import egress_gate, tide_check_allowed

from divesafe.models.egress import EgressBasis
from divesafe.models.ollama import OllamaProvider
from divesafe.models.openai_compatible import OpenAICompatibleProvider

pytestmark = pytest.mark.safety


def _loopback() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider("m", "http://127.0.0.1:8317/v1")


def _remote() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "m", "https://llm.example.org/v1", allowed_hosts=frozenset({"llm.example.org"})
    )


def test_an_undeclared_loopback_server_is_skipped() -> None:
    with pytest.raises(pytest.skip.Exception, match="loopback"):
        egress_gate(_loopback(), allow_external=False, server_is_local=False)
    with pytest.raises(pytest.skip.Exception, match="loopback"):
        egress_gate(
            OllamaProvider("m", "http://localhost:11434"),
            allow_external=False,
            server_is_local=False,
        )


def test_a_declared_or_opted_in_loopback_server_runs_and_reports_why() -> None:
    assert egress_gate(_loopback(), allow_external=False, server_is_local=True) == "declared_local"
    assert (
        egress_gate(_loopback(), allow_external=True, server_is_local=False)
        == "loopback_external_opt_in"
    )


def test_a_remote_server_needs_the_opt_in_and_refuses_the_declaration() -> None:
    with pytest.raises(pytest.skip.Exception, match="off-machine"):
        egress_gate(_remote(), allow_external=False, server_is_local=False)
    with pytest.raises(pytest.skip.Exception, match="not on this machine"):
        egress_gate(_remote(), allow_external=False, server_is_local=True)
    assert (
        egress_gate(_remote(), allow_external=True, server_is_local=False)
        == "remote_external_opt_in"
    )


def test_both_flags_together_fail_instead_of_skipping() -> None:
    with pytest.raises(ValidationError, match="contradict"):
        egress_gate(_loopback(), allow_external=True, server_is_local=True)


@pytest.mark.parametrize(
    ("basis", "acknowledged", "allowed"),
    [
        ("declared_local", False, True),
        ("declared_local", True, True),
        ("loopback_external_opt_in", False, False),
        ("remote_external_opt_in", False, False),
        ("none", False, False),
        ("loopback_external_opt_in", True, True),
        ("remote_external_opt_in", True, True),
    ],
)
def test_the_tide_check_sends_the_phn_item_only_to_a_local_server_or_when_acknowledged(
    basis: EgressBasis, acknowledged: bool, allowed: bool
) -> None:
    assert tide_check_allowed(basis, phn_terms_acknowledged=acknowledged) is allowed
