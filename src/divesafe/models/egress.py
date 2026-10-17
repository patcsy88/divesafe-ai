"""Where a model server may send the prompt (ADR 0023).

`LLMProvider.external` only says whether the server's address is off this machine. It does not say
where the prompt ends up: a proxy on loopback, an SSH tunnel or a port-forward can all carry it to
a hosted model. So the operator must say what the server does, and the answer is returned as an
"egress basis" that is logged and recorded with evaluation runs.
"""

from __future__ import annotations

from typing import Literal

from divesafe.config.settings import Settings
from divesafe.models.llm import FakeProvider, LLMProvider

EgressBasis = Literal[
    "none",
    "declared_local",
    "loopback_external_opt_in",
    "remote_external_opt_in",
]


def check_egress(provider: LLMProvider, settings: Settings) -> EgressBasis:
    """Return why this provider may be used, or raise `RuntimeError`.

    A remote server needs DIVESAFE_ALLOW_EXTERNAL_LLM. A loopback address does not show where a
    server sends the prompt, so it needs either that opt-in or the declaration
    DIVESAFE_LLM_SERVER_IS_LOCAL, an operator statement the software cannot check. The declaration
    is refused for a remote server. The exact `FakeProvider` class runs scripted replies in tests
    and is exempt; a subclass is not."""
    if type(provider) is FakeProvider:
        return "none"
    if provider.external:
        if settings.llm_server_is_local:
            raise RuntimeError(
                "DIVESAFE_LLM_SERVER_IS_LOCAL cannot be used with a model server that is not "
                "on this machine"
            )
        if not settings.allow_external_llm:
            raise RuntimeError(
                "this LLM provider sends evidence and plan data off-machine; "
                "set DIVESAFE_ALLOW_EXTERNAL_LLM=true to accept that"
            )
        return "remote_external_opt_in"
    if settings.llm_server_is_local:
        return "declared_local"
    if settings.allow_external_llm:
        return "loopback_external_opt_in"
    raise RuntimeError(
        "this LLM server is on a loopback address, which does not show where it sends "
        "prompts (a local proxy, SSH tunnel or port-forward can carry them to a hosted model); "
        "set DIVESAFE_LLM_SERVER_IS_LOCAL=true if it runs the model on this machine and "
        "forwards nowhere, or DIVESAFE_ALLOW_EXTERNAL_LLM=true to accept that it may not"
    )
