"""Loopback helpers shared by the self-hosted model adapters (ADR 0023).

A loopback host name is resolved by the system, so the address actually connected to is checked
before the prompt or an API key is sent (over http right after the TCP connection, over https
after the TLS handshake, so the ClientHello and server name have already gone).
That the peer is on this machine still does not show where it forwards the prompt; that is the
operator's declaration (`DIVESAFE_LLM_SERVER_IS_LOCAL`).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.request
from typing import Any

_LOOPBACK_NAMES = frozenset({"localhost"})


class NotOnThisMachineError(RuntimeError):
    """The connected peer is not a loopback address. The message is fixed."""

    def __init__(self) -> None:
        super().__init__("the model server is not on this machine")

    def __reduce__(self) -> tuple[type[NotOnThisMachineError], tuple[()]]:
        return (type(self), ())


def is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _require_loopback_peer(connection: http.client.HTTPConnection) -> None:
    sock = connection.sock
    peer = sock.getpeername()[0] if isinstance(sock, socket.socket) else ""
    if not is_loopback(str(peer).split("%", 1)[0]):
        connection.close()
        raise NotOnThisMachineError


class LoopbackConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        super().connect()
        _require_loopback_peer(self)


class LoopbackSecureConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        _require_loopback_peer(self)


class LoopbackHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(LoopbackConnection, req)


class LoopbackHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            LoopbackSecureConnection,
            req,
            context=self._context,  # type: ignore[attr-defined]
        )
