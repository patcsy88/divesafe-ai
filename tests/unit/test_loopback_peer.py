"""The peer-address check shared by the self-hosted adapters (ADR 0023), over http and https."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from divesafe.models import loopback as lb
from divesafe.models.ollama import OllamaError, OllamaProvider

pytestmark = pytest.mark.safety


@pytest.fixture
def listener() -> Iterator[tuple[str, int]]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    yield srv.getsockname()
    srv.close()


@pytest.mark.parametrize("cls", [lb.LoopbackConnection, lb.LoopbackSecureConnection])
def test_a_peer_that_is_not_loopback_closes_the_connection(
    cls: type, listener: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    host, port = listener
    client = socket.create_connection((host, port))
    base = cls.__mro__[1]
    monkeypatch.setattr(base, "connect", lambda self: setattr(self, "sock", client))
    monkeypatch.setattr(lb, "is_loopback", lambda h: False)
    connection: Any = cls(host, port)
    with pytest.raises(lb.NotOnThisMachineError, match="not on this machine"):
        connection.connect()
    assert connection.sock is None


@pytest.mark.parametrize("cls", [lb.LoopbackConnection, lb.LoopbackSecureConnection])
def test_a_loopback_peer_is_accepted(
    cls: type, listener: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    host, port = listener
    client = socket.create_connection((host, port))
    monkeypatch.setattr(cls.__mro__[1], "connect", lambda self: setattr(self, "sock", client))
    connection: Any = cls(host, port)
    connection.connect()
    assert connection.sock is client
    client.close()


class _Handler(BaseHTTPRequestHandler):
    seen: list[bytes] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        type(self).seen.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.send_response(200)
        self.end_headers()


def test_ollama_sends_nothing_if_the_peer_is_not_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from divesafe.models import ChatMessage, LLMRequest

    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        provider = OllamaProvider("m", f"http://127.0.0.1:{httpd.server_address[1]}")
        monkeypatch.setattr(lb, "is_loopback", lambda h: False)
        request = LLMRequest(messages=(ChatMessage(role="user", content="SECRET-PROMPT"),))
        with pytest.raises(OllamaError, match="not on this machine"):
            asyncio.run(provider.complete(request))
        assert _Handler.seen == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def _fail_the_peer_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Any) -> None:
        raise lb.NotOnThisMachineError

    monkeypatch.setattr(lb.LoopbackSecureConnection, "connect", refuse)


def test_a_loopback_https_url_goes_through_the_peer_check_in_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from divesafe.models import ChatMessage, LLMRequest

    _fail_the_peer_check(monkeypatch)
    request = LLMRequest(messages=(ChatMessage(role="user", content="x"),))
    with pytest.raises(OllamaError, match="not on this machine"):
        asyncio.run(OllamaProvider("m", "https://127.0.0.1:1").complete(request))


def test_a_loopback_https_url_goes_through_the_peer_check_in_the_compatible_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from divesafe.models import ChatMessage, LLMRequest
    from divesafe.models.openai_compatible import OpenAICompatibleError, OpenAICompatibleProvider

    _fail_the_peer_check(monkeypatch)
    request = LLMRequest(messages=(ChatMessage(role="user", content="x"),))
    with pytest.raises(OpenAICompatibleError, match="not on this machine"):
        asyncio.run(OpenAICompatibleProvider("m", "https://127.0.0.1:1/v1").complete(request))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("::ffff:127.0.0.1", True),
        ("::ffff:10.0.0.5", False),
        ("10.0.0.5", False),
        ("0.0.0.0", False),
        ("fe80::1", False),
        ("localhost.evil.example", False),
        ("", False),
    ],
)
def test_which_peers_count_as_loopback(host: str, expected: bool) -> None:
    assert lb.is_loopback(host) is expected


def test_a_scoped_ipv6_peer_is_judged_without_its_scope() -> None:
    class _Sock(socket.socket):
        def getpeername(self) -> Any:  # type: ignore[override]
            return ("::1%lo0", 1, 0, 0)

    class _Conn(lb.LoopbackConnection):
        pass

    conn = _Conn("localhost")
    sock = _Sock()
    conn.sock = sock
    lb._require_loopback_peer(conn)
    sock.close()


def test_ollama_verifies_certificates_for_loopback_and_remote_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import ssl

    from divesafe.models import ChatMessage, LLMRequest
    from divesafe.models import ollama as mod

    seen: list[ssl.SSLContext] = []
    real_build = mod.urllib.request.build_opener

    def recording(*handlers: Any) -> Any:
        seen.extend(h._context for h in handlers if hasattr(h, "_context"))
        return real_build(*handlers)

    monkeypatch.setattr(mod.urllib.request, "build_opener", recording)
    request = LLMRequest(messages=(ChatMessage(role="user", content="x"),))
    for url in ("https://127.0.0.1:1", "https://ollama.example.org"):
        seen.clear()
        with pytest.raises(OllamaError):
            asyncio.run(OllamaProvider("m", url).complete(request))
        assert seen
        assert all(c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname for c in seen)
