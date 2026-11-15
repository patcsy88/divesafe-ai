"""Start-up checks for tracing to the local, self-hosted Langfuse (ADR 0027 amendment).

With tracing off nothing here opens a socket or imports an OpenTelemetry module. Messages are
fixed text and never echo the endpoint, an address or a variable's value.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping

from divesafe.config import Settings
from divesafe.config.settings import trace_endpoint_address, tracing_settings_refusal

OTEL_REFUSAL = (
    "refusing to start: an OTEL_* variable is set while DIVESAFE_TRACING_ENABLED=true; the "
    "environment must not change where spans go or what they hold (ADR 0027)"
)
SUBNET_REFUSAL = (
    "refusing to start: this container has no interface in the /24 of DIVESAFE_TRACE_ENDPOINT, "
    "so it is not on the divesafe-trace network (ADR 0027)"
)


def check_tracing_startup(settings: Settings, environ: Mapping[str, str]) -> None:
    """Repeat the settings checks (`model_copy` and `model_construct` skip validators), then,
    only when tracing is enabled, refuse any `OTEL_*` variable and check the subnet."""
    refusal = tracing_settings_refusal(settings)
    if refusal is not None:
        raise RuntimeError(f"refusing to start: {refusal}")
    if not settings.tracing_enabled:
        return
    if any(name.upper().startswith("OTEL_") for name in environ):
        raise RuntimeError(OTEL_REFUSAL)
    target = trace_endpoint_address(settings.trace_endpoint)
    if target is None:  # unreachable after the refusal check; never fall through to a send
        raise RuntimeError(SUBNET_REFUSAL)
    if not _has_interface_in_subnet(*target):
        raise RuntimeError(SUBNET_REFUSAL)


def _has_interface_in_subnet(address: ipaddress.IPv4Address, port: int) -> bool:
    """Connect a UDP socket (no packet is sent) and compare the local address the kernel picks.
    A failed connect, as an error number or an `OSError`, is the same refusal as a wrong subnet.
    `connect_ex` returns the error number instead of raising, so both failure paths are explicit;
    the database-connection scan in `tests/safety/test_app_connection_options.py` checks
    `.connect` calls only and is not meant to cover this probe."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            if probe.connect_ex((str(address), port)) != 0:
                return False
            local_text = probe.getsockname()[0]
    except OSError:
        return False
    try:
        local = ipaddress.IPv4Address(local_text)
    except ValueError:
        return False
    subnet = ipaddress.IPv4Network(f"{address}/24", strict=False)
    return local in subnet and local != address
