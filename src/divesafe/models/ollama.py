"""Ollama adapter (ADR 0016): a local or self-hosted model over its native chat API.

Never logs a request (it holds the whole prompt). Errors carry no prompt, response or URL
content. Redirects are refused. A non-loopback server is treated as external, because the prompt
leaves this machine.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from divesafe.config.settings import Settings
from divesafe.models.llm import LLMRequest, LLMResponse, register_provider

DEFAULT_BASE_URL = "http://localhost:11434"
MAX_RESPONSE_BYTES = 1024 * 1024
SOCKET_TIMEOUT_SECONDS = 15.0
TOTAL_DEADLINE_SECONDS = 50.0  # below the agent runner's 60 s, so the worker thread ends first
MAX_CONCURRENT_CALLS = 4
_CHUNK = 64 * 1024
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
_LOOPBACK_NAMES = frozenset({"localhost"})

Transport = Callable[[str, bytes, float], bytes]


class OllamaError(RuntimeError):
    """The server was unreachable or returned something unusable. Messages are fixed strings."""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        return None


def _post(url: str, body: bytes, timeout: float) -> bytes:
    """POST with no proxy (environment proxies would carry the prompt elsewhere), no redirects, a
    size cap and a total deadline. `timeout` is the whole-call budget in seconds."""
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(  # noqa: S310 (scheme and host checked at construction)
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    if not _SLOTS.acquire(timeout=timeout):
        raise OllamaError("too many Ollama calls are already in flight")
    try:
        chunks: list[bytes] = []
        size = 0
        with opener.open(request, timeout=SOCKET_TIMEOUT_SECONDS) as response:
            while True:
                if time.monotonic() > deadline:
                    raise OllamaError("the Ollama call exceeded its total deadline")
                chunk: bytes = response.read1(min(_CHUNK, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise OllamaError("the Ollama response was too large")
    except OllamaError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        raise OllamaError("the Ollama server could not be reached or refused the request") from None
    finally:
        _SLOTS.release()
    return b"".join(chunks)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self, model: str, base_url: str = DEFAULT_BASE_URL, transport: Transport | None = None
    ) -> None:
        if not model.strip():
            raise ValueError("an Ollama model name is required (DIVESAFE_LLM_MODEL)")
        parsed = urllib.parse.urlsplit(base_url)
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not host:
            raise ValueError("the Ollama base URL must be an http(s) URL with a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "the Ollama base URL must not carry credentials, a query or a fragment"
            )
        try:
            parsed.port  # noqa: B018 (validates the port)
        except ValueError:
            raise ValueError("the Ollama base URL has an invalid port") from None
        loopback = _is_loopback(host)
        if parsed.scheme == "http" and not loopback:
            raise ValueError("a remote Ollama server must use https")
        self.model = model.strip()
        self.external = not loopback
        self._url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/api/chat"
        self._transport = transport or _post

    def _body(self, request: LLMRequest) -> bytes:
        options: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": False,
            "options": options,
        }
        if request.json_schema is not None:
            payload["format"] = request.json_schema
        return json.dumps(payload).encode()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = self._body(request)
        raw = await asyncio.to_thread(self._transport, self._url, body, TOTAL_DEADLINE_SECONDS)
        try:
            data = json.loads(raw)
            text = data["message"]["content"]
        except (ValueError, KeyError, TypeError, RecursionError):
            raise OllamaError("the Ollama response was not in the expected shape") from None
        if not isinstance(text, str):
            raise OllamaError("the Ollama response was not in the expected shape")
        return LLMResponse(text=text, provider=self.name, model=self.model)


def _factory(settings: Settings) -> OllamaProvider:
    base = str(settings.llm_base_url) if settings.llm_base_url else DEFAULT_BASE_URL
    return OllamaProvider(settings.llm_model or "", base)


register_provider("ollama", _factory)
