"""The real HTTP path of the Ollama adapter, against a throwaway loopback server."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from divesafe.models import ChatMessage, LLMRequest
from divesafe.models import ollama as mod
from divesafe.models.ollama import OllamaError, OllamaProvider

REQUEST = LLMRequest(messages=(ChatMessage(role="user", content="SECRET-PROMPT"),))
GOOD = json.dumps({"message": {"content": "ok"}}).encode()


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    redirect_to = "http://127.0.0.1:1/elsewhere"
    seen: list[bytes] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        type(self).seen.append(self.rfile.read(length))
        mode = type(self).mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
        elif mode == "error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"SECRET-SERVER-TRACE")
        elif mode == "huge":
            self.send_response(200)
            self.send_header("Content-Length", str(mod.MAX_RESPONSE_BYTES + 10))
            self.end_headers()
            self.wfile.write(b"x" * (mod.MAX_RESPONSE_BYTES + 10))
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
    _Handler.mode, _Handler.seen = "ok", []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _complete(url: str) -> str:
    return asyncio.run(OllamaProvider("m", url).complete(REQUEST)).text


def _printed(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def test_a_real_round_trip_works_and_sends_the_prompt_to_the_server(server: str) -> None:
    assert _complete(server) == "ok"
    assert b"SECRET-PROMPT" in _Handler.seen[0]


@pytest.mark.parametrize("mode", ["redirect", "error"])
def test_a_redirect_or_server_error_gives_a_fixed_message(server: str, mode: str) -> None:
    _Handler.mode = mode
    with pytest.raises(OllamaError, match="could not be reached") as caught:
        _complete(server)
    text = _printed(caught.value)
    assert "SECRET" not in text and "elsewhere" not in text


def test_an_oversized_response_is_refused(server: str) -> None:
    _Handler.mode = "huge"
    with pytest.raises(OllamaError, match="too large"):
        _complete(server)


def test_a_slow_drip_hits_the_total_deadline(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _Handler.mode = "drip"
    monkeypatch.setattr(mod, "TOTAL_DEADLINE_SECONDS", 0.3)
    started = time.monotonic()
    with pytest.raises(OllamaError, match="total deadline"):
        _complete(server)
    assert time.monotonic() - started < 2


def test_environment_proxies_are_ignored(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    assert _complete(server) == "ok"


def test_too_many_calls_in_flight_are_refused(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_SLOTS", threading.BoundedSemaphore(1))
    mod._SLOTS.acquire()
    with pytest.raises(OllamaError, match="in flight"):
        mod._post(f"{server}/api/chat", b"{}", 0.1)


def test_the_concurrency_limit_is_small() -> None:
    assert 1 <= mod.MAX_CONCURRENT_CALLS <= 8
    sem = mod._SLOTS
    taken = 0
    while sem.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        sem.release()
    assert taken == mod.MAX_CONCURRENT_CALLS


def test_a_redirect_is_never_followed_to_a_live_second_server(server: str) -> None:
    hits: list[bytes] = []

    class _Target(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _answer(self) -> None:
            hits.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(200)
            self.send_header("Content-Length", str(len(GOOD)))
            self.end_headers()
            self.wfile.write(GOOD)

        do_POST = do_GET = _answer  # a followed 302 turns the POST into a GET

    target = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    try:
        _Handler.mode = "redirect"
        _Handler.redirect_to = f"http://127.0.0.1:{target.server_address[1]}/elsewhere"
        with pytest.raises(OllamaError):
            _complete(server)
        assert hits == []
    finally:
        target.shutdown()
        target.server_close()
        _Handler.redirect_to = "http://127.0.0.1:1/elsewhere"


@pytest.mark.parametrize("url", ["http://localhost:abc", "http://localhost:99999"])
def test_a_bad_port_is_refused_at_construction(url: str) -> None:
    with pytest.raises(ValueError, match="port"):
        OllamaProvider("m", url)


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:11434",
        "http://2130706433",
        "http://[::ffff:127.0.0.1]",
        "http://localhost.:1",
    ],
)
def test_odd_loopback_spellings_do_not_get_plain_http(url: str) -> None:
    try:
        provider = OllamaProvider("m", url)
    except ValueError:
        return
    assert provider.external is False or "localhost" in url


def test_a_deeply_nested_response_is_a_fixed_error() -> None:
    nested = b"[" * 200_000 + b"]" * 200_000
    provider = OllamaProvider("m", transport=lambda u, b, t: nested)
    with pytest.raises(OllamaError, match="expected shape"):
        asyncio.run(provider.complete(REQUEST))
