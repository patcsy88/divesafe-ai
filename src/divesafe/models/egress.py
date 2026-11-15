"""Where a model server may send the prompt (ADR 0023).

`LLMProvider.external` only says whether the server's address is off this machine. It does not say
where the prompt ends up: a proxy on loopback, an SSH tunnel or a port-forward can all carry it to
a hosted model. So the operator must say what the server does, and the answer is returned as an
"egress basis" that is logged and recorded with evaluation runs.
"""

from __future__ import annotations

from typing import Literal

from divesafe.config.settings import Settings, base_url_host, is_single_knowledge_host
from divesafe.models.llm import FakeProvider, LLMProvider

EgressBasis = Literal[
    "none",
    "declared_local",
    "loopback_external_opt_in",
    "remote_external_opt_in",
    "remote_knowledge_opt_in",
]

# Fixed refusals for the knowledge host (ADR 0028 decision 3). They never name the host or URL.
_KH = "DIVESAFE_LLM_KNOWLEDGE_HOST"
KNOWLEDGE_HOST_NOT_REMOTE = f"{_KH} needs a model server that is not on this machine"
KNOWLEDGE_HOST_NO_OPT_IN = f"{_KH} needs DIVESAFE_ALLOW_EXTERNAL_LLM=true"
KNOWLEDGE_HOST_DECLARED_LOCAL = f"{_KH} cannot be used with DIVESAFE_LLM_SERVER_IS_LOCAL"
KNOWLEDGE_HOST_NO_SERVER = f"{_KH} needs a model server provider and DIVESAFE_LLM_BASE_URL"
KNOWLEDGE_HOST_MALFORMED = f"{_KH} must be exactly one hostname, not a loopback name"
KNOWLEDGE_HOST_NOT_ALLOWED = f"{_KH} must be one of DIVESAFE_LLM_ALLOWED_HOSTS"
KNOWLEDGE_HOST_MISMATCH = f"{_KH} must be the host of DIVESAFE_LLM_BASE_URL and of the provider"


def _knowledge_host_set(settings: Settings) -> bool:
    """Absent or empty means unset (`env_ignore_empty`, as `Settings` reads it); any other value,
    including a non-string one smuggled in by `model_construct`, means set."""
    host: object = settings.llm_knowledge_host
    return host is not None and host != ""


def _check_knowledge_host(provider: LLMProvider, settings: Settings) -> EgressBasis:
    """ADR 0028 decision 3: `remote_knowledge_opt_in` only when every condition holds; otherwise
    raise, never fall back to another basis. Repeats the `Settings` checks, because
    `model_copy` and `model_construct` skip its validators."""
    host: object = settings.llm_knowledge_host
    if type(provider.external) is not bool or not provider.external:
        raise RuntimeError(KNOWLEDGE_HOST_NOT_REMOTE)
    if settings.allow_external_llm is not True:
        raise RuntimeError(KNOWLEDGE_HOST_NO_OPT_IN)
    if settings.llm_server_is_local is not False:
        raise RuntimeError(KNOWLEDGE_HOST_DECLARED_LOCAL)
    if settings.llm_provider == "fake" or settings.llm_base_url is None:
        raise RuntimeError(KNOWLEDGE_HOST_NO_SERVER)
    if type(host) is not str or not is_single_knowledge_host(host):
        raise RuntimeError(KNOWLEDGE_HOST_MALFORMED)
    try:
        allowed = settings.llm_allowed_host_set()
    except ValueError:
        raise RuntimeError(KNOWLEDGE_HOST_NOT_ALLOWED) from None
    if host not in allowed:
        raise RuntimeError(KNOWLEDGE_HOST_NOT_ALLOWED)
    provider_host: object = getattr(provider, "host", None)
    url_host = base_url_host(str(settings.llm_base_url))
    if type(provider_host) is not str or url_host is None:
        raise RuntimeError(KNOWLEDGE_HOST_MISMATCH)
    if host != provider_host or host != url_host:
        raise RuntimeError(KNOWLEDGE_HOST_MISMATCH)
    return "remote_knowledge_opt_in"


def check_egress(provider: LLMProvider, settings: Settings) -> EgressBasis:
    """Return why this provider may be used, or raise `RuntimeError`.

    A remote server needs DIVESAFE_ALLOW_EXTERNAL_LLM. A loopback address does not show where a
    server sends the prompt, so it needs either that opt-in or the declaration
    DIVESAFE_LLM_SERVER_IS_LOCAL, an operator statement the software cannot check. The declaration
    is refused for a remote server, and the two flags together are refused. The exact
    `FakeProvider` class runs scripted replies in tests and is exempt; a subclass is not.

    With DIVESAFE_LLM_KNOWLEDGE_HOST set (ADR 0028) the answer is `remote_knowledge_opt_in` or a
    refusal, checked before the fake-provider exemption, so a fake provider with it set raises."""
    if _knowledge_host_set(settings):
        return _check_knowledge_host(provider, settings)
    if type(provider) is FakeProvider:
        return "none"
    # `Settings` refuses this pair too, but `model_copy` and `model_construct` skip its validators.
    if settings.llm_server_is_local and settings.allow_external_llm:
        raise RuntimeError(
            "DIVESAFE_LLM_SERVER_IS_LOCAL and DIVESAFE_ALLOW_EXTERNAL_LLM contradict each other"
        )
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
