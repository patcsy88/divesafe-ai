"""One request id per HTTP request (ADR 0027 item 1).

An inbound `X-Request-ID` is kept only when the TCP peer is a configured trusted proxy and the
header appears exactly once and fully matches `REQUEST_ID_PATTERN`; otherwise a new UUID4 is used.
A refused value is never stored, echoed or logged. The id is bound for the request's log lines
and returned in the `X-Request-ID` response header.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from divesafe.config.logging import bind_request_id, unbind_request_id, valid_request_id
from divesafe.config.netaddr import canonical_ip

HEADER = "x-request-id"
_HEADER_BYTES = HEADER.encode("ascii")


def choose_request_id(scope: Scope, trusted_proxies: frozenset[str]) -> str:
    client = scope.get("client")
    peer = canonical_ip(client[0]) if client and type(client[0]) is str else None
    if peer is not None and str(peer) in trusted_proxies:
        inbound = [value for name, value in scope.get("headers", ()) if name == _HEADER_BYTES]
        if len(inbound) == 1:
            candidate: str
            try:
                candidate = bytes(inbound[0]).decode("ascii")
            except UnicodeDecodeError:
                candidate = ""
            if valid_request_id(candidate):
                return candidate
    return uuid.uuid4().hex


def response_header(request_id: object) -> dict[str, str]:
    """The header for a response built outside the middleware; nothing unless the id is valid."""
    return {HEADER: request_id} if type(request_id) is str and valid_request_id(request_id) else {}


class RequestIdMiddleware:
    """`trusted_proxies` is read on every request, as authentication reads it."""

    def __init__(
        self, app: ASGIApp, trusted_proxies: Callable[[], frozenset[str]] = frozenset
    ) -> None:
        self.app = app
        self.trusted_proxies = trusted_proxies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = choose_request_id(scope, self.trusted_proxies())
        # A new dict, so a state mapping the server shares is never written to.
        scope["state"] = {**scope.get("state", {}), "request_id": request_id}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                del headers[HEADER]
                headers.append(HEADER, request_id)
            await send(message)

        token = bind_request_id(request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            unbind_request_id(token)
