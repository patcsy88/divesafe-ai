"""The real HTTP path of the OpenAI-compatible adapter, against a throwaway loopback server."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pydantic import SecretStr

from divesafe.models import ChatMessage, LLMRequest
from divesafe.models import ollama as shared
from divesafe.models.openai_compatible import OpenAICompatibleError, OpenAICompatibleProvider

REQUEST = LLMRequest(messages=(ChatMessage(role="user", content="SECRET-PROMPT"),))
GOOD = (
    b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)
KEY = "sk-SECRET-KEY-123"


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list[tuple[str, dict[str, str], bytes]] = []
    redirect_to = ""

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # only reached if a redirect is followed (urllib turns POST into GET)
        type(self).seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, b""))
        self.send_response(200)
        self.send_header("Content-Length", str(len(GOOD)))
        self.end_headers()
        self.wfile.write(GOOD)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        mode = type(self).mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to or "http://127.0.0.1:1/elsewhere")
            self.end_headers()
        elif mode == "error":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(f"SECRET-SERVER-TRACE {KEY}".encode())
        elif mode == "huge":
            self.send_response(200)
            self.send_header("Content-Length", str(shared.MAX_RESPONSE_BYTES + 10))
            self.end_headers()
            self.wfile.write(b"x" * (shared.MAX_RESPONSE_BYTES + 10))
        elif mode == "silent":
            time.sleep(2)
        elif mode == "drip":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(GOOD)))
            self.end_headers()
            self.wfile.write(GOOD)


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.mode, _Handler.seen, _Handler.redirect_to = "ok", [], ""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()


def _complete(url: str, **kwargs: Any) -> str:
    return asyncio.run(OpenAICompatibleProvider("m", url, **kwargs).complete(REQUEST)).text


def _printed(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def test_a_real_round_trip_sends_the_prompt_and_the_key_in_the_header_only(server: str) -> None:
    assert _complete(server, api_key=SecretStr(KEY)) == "ok"
    path, headers, body = _Handler.seen[0]
    assert path == "/v1/chat/completions"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert b"SECRET-PROMPT" in body and KEY.encode() not in body


def test_no_authorization_header_is_sent_without_a_key(server: str) -> None:
    _complete(server)
    assert "authorization" not in _Handler.seen[0][1]


@pytest.mark.parametrize("mode", ["redirect", "error"])
def test_a_redirect_or_server_error_gives_a_fixed_message_without_secrets(
    server: str, mode: str
) -> None:
    _Handler.mode = mode
    with pytest.raises(OpenAICompatibleError, match="could not be reached") as caught:
        _complete(server, api_key=SecretStr(KEY))
    text = _printed(caught.value)
    assert "SECRET" not in text and "elsewhere" not in text and KEY not in text


def test_a_redirect_is_not_followed(server: str) -> None:
    _Handler.mode = "redirect"
    _Handler.redirect_to = f"{server}/elsewhere"  # a live target: following it would be visible
    with pytest.raises(OpenAICompatibleError):
        _complete(server, api_key=SecretStr(KEY))
    assert len(_Handler.seen) == 1  # the key never went to the redirect target


def test_an_oversized_response_is_refused(server: str) -> None:
    _Handler.mode = "huge"
    with pytest.raises(OpenAICompatibleError, match="too large"):
        _complete(server)


def test_a_slow_drip_hits_the_total_deadline(server: str) -> None:
    _Handler.mode = "drip"
    started = time.monotonic()
    with pytest.raises(OpenAICompatibleError, match="total deadline"):
        _complete(
            server,
            call_timeout_seconds=1.6,
            stage_timeout_seconds=2.0,
            first_byte_timeout_seconds=0.3,
        )
    assert time.monotonic() - started < 2


def test_the_first_byte_timeout_comes_from_the_provider(server: str) -> None:
    _Handler.mode = "silent"
    started = time.monotonic()
    with pytest.raises(OpenAICompatibleError, match="could not be reached"):
        _complete(
            server,
            first_byte_timeout_seconds=0.3,
            call_timeout_seconds=30,
            stage_timeout_seconds=60,
        )
    assert time.monotonic() - started < 3


@pytest.mark.safety
def test_the_tls_context_verifies_certificates_and_host_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ssl

    from divesafe.models import openai_compatible as oc

    seen: list[ssl.SSLContext] = []
    real_build = oc.urllib.request.build_opener

    def recording(*handlers: Any) -> Any:
        seen.extend(h._context for h in handlers if hasattr(h, "_context"))
        return real_build(*handlers)

    monkeypatch.setattr(oc.urllib.request, "build_opener", recording)
    for url in ("http://127.0.0.1:1/v1", "https://llm.invalid/v1"):
        seen.clear()
        provider = OpenAICompatibleProvider("m", url, allowed_hosts=frozenset({"llm.invalid"}))
        with pytest.raises(OpenAICompatibleError):
            asyncio.run(provider.complete(REQUEST))
        assert seen and all(c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname for c in seen)


@pytest.mark.safety
def test_plain_http_is_refused_if_the_connection_does_not_land_on_loopback(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.models import loopback as lb

    provider = OpenAICompatibleProvider("m", server, api_key=SecretStr(KEY))  # built as loopback
    monkeypatch.setattr(lb, "is_loopback", lambda host: False)  # but the peer check disagrees
    with pytest.raises(OpenAICompatibleError, match="not on this machine"):
        asyncio.run(provider.complete(REQUEST))
    assert _Handler.seen == []  # nothing, including the key, was sent


def test_environment_proxies_are_ignored(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    assert _complete(server) == "ok"


def test_each_provider_has_its_own_pool_and_concurrency_limit(server: str) -> None:
    names: list[str] = []

    def transport(url: str, body: bytes, timeout: float) -> bytes:
        names.append(threading.current_thread().name)
        return GOOD

    one = OpenAICompatibleProvider("m", server, transport=transport, max_concurrent_calls=1)
    asyncio.run(one.complete(REQUEST))
    assert names[0].startswith("openai-compat")
