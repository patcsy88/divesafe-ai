"""Ollama adapter (ADR 0016): a local or self-hosted model over its native chat API.

The reply is streamed, so a proxy with an idle timeout does not cut off a slow answer.

Never logs a request (it holds the whole prompt). Errors carry no prompt, response or URL
content. Redirects are refused. A non-loopback server is treated as external, because the prompt
leaves this machine. Plain http to a remote host is refused unless that exact hostname is in
`DIVESAFE_OLLAMA_PLAIN_HTTP_HOSTS`; the prompt is then unencrypted on the network.
"""

from __future__ import annotations

import asyncio
import functools
import http.client
import ipaddress
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from divesafe.config.settings import Settings
from divesafe.models.llm import LLMRequest, LLMResponse, register_provider

DEFAULT_BASE_URL = "http://localhost:11434"
MAX_RESPONSE_BYTES = 1024 * 1024
SOCKET_TIMEOUT_SECONDS = 15.0
MAX_CONCURRENT_CALLS = 4
_CHUNK = 64 * 1024
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
_LOOPBACK_NAMES = frozenset({"localhost"})

Transport = Callable[[str, bytes, float], bytes]


class OllamaError(RuntimeError):
    """The server was unreachable or returned something unusable. Messages are fixed strings."""


def is_loopback(host: str) -> bool:
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


def _post(
    url: str,
    body: bytes,
    timeout: float,
    *,
    slots: threading.BoundedSemaphore | None = None,
    socket_timeout: float | None = None,
) -> bytes:
    """POST with no proxy (environment proxies would carry the prompt elsewhere), no redirects, a
    size cap and a total deadline. `timeout` is the whole-call budget in seconds."""
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(  # noqa: S310 (scheme and host checked at construction)
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    slots = slots if slots is not None else _SLOTS
    if not slots.acquire(timeout=timeout):
        raise OllamaError("too many Ollama calls are already in flight")
    try:
        if time.monotonic() >= deadline:
            raise OllamaError("the Ollama call exceeded its total deadline")
        chunks: list[bytes] = []
        size = 0
        with opener.open(
            request,
            timeout=socket_timeout if socket_timeout is not None else SOCKET_TIMEOUT_SECONDS,
        ) as response:
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
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OllamaError("the Ollama server could not be reached or refused the request") from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        raise OllamaError("the Ollama server could not be reached or refused the request") from None
    finally:
        slots.release()
    return b"".join(chunks)


MAX_STREAM_LINES = 20_000


def _join_stream(raw: bytes) -> str:
    """Join the streamed lines (one JSON object each). The reply counts only if the final line
    says `done` with `done_reason` "stop"; a cut-off stream, a reply stopped by the token limit,
    an error line or any odd shape is refused, never repaired. `thinking` text is never used."""
    bad = OllamaError("the Ollama response was not in the expected shape")
    raw_lines = raw.splitlines()
    if not raw_lines or len(raw_lines) > MAX_STREAM_LINES:
        raise bad
    parts: list[str] = []
    last: dict[str, Any] | None = None
    for line in raw_lines:
        if not line.strip():
            continue
        if last is not None and last.get("done") is not False:
            raise bad  # something follows a final line
        try:
            x = json.loads(line)
        except (ValueError, RecursionError):
            raise bad from None
        message = x.get("message") if isinstance(x, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise bad
        parts.append(content)
        last = x
    if last is None or last.get("done") is not True or last.get("done_reason", "stop") != "stop":
        raise bad
    return "".join(parts)


MAX_CALL_TIMEOUT_SECONDS = 600.0
MAX_STAGE_TIMEOUT_SECONDS = 1800.0
MAX_FIRST_BYTE_TIMEOUT_SECONDS = 300.0
MAX_CONCURRENT_CEILING = 8


def check_budgets(call: float, stage: float, first_byte: float, concurrent: int) -> None:
    numbers = (call, stage, first_byte)
    if any(isinstance(n, bool) or not isinstance(n, int | float) for n in numbers):
        raise ValueError("the LLM time budgets must be numbers")
    if isinstance(concurrent, bool) or not isinstance(concurrent, int):
        raise ValueError("the LLM concurrency must be a whole number")
    if not all(0 < n < float("inf") for n in numbers):
        raise ValueError("the LLM time budgets must be positive and finite")
    if (
        call > MAX_CALL_TIMEOUT_SECONDS
        or stage > MAX_STAGE_TIMEOUT_SECONDS
        or first_byte > MAX_FIRST_BYTE_TIMEOUT_SECONDS
        or not 1 <= concurrent <= MAX_CONCURRENT_CEILING
    ):
        raise ValueError("an LLM time budget or the concurrency is above its ceiling")
    if stage < call or call < 2 * first_byte + 1:
        raise ValueError(
            "the stage budget must cover one call, and a call must be at least twice the "
            "first-byte timeout plus 1 s"
        )


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        think: bool | None = False,
        plain_http_hosts: frozenset[str] = frozenset(),
        call_timeout_seconds: float = 60.0,
        stage_timeout_seconds: float = 150.0,
        first_byte_timeout_seconds: float = SOCKET_TIMEOUT_SECONDS,
        max_concurrent_calls: int = MAX_CONCURRENT_CALLS,
    ) -> None:
        if not model.strip():
            raise ValueError("an Ollama model name is required (DIVESAFE_LLM_MODEL)")
        try:
            parsed = urllib.parse.urlsplit(base_url)
        except ValueError:
            raise ValueError("the Ollama base URL is not a valid URL") from None
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not host:
            raise ValueError("the Ollama base URL must be an http(s) URL with a host")
        if "@" in parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(
                "the Ollama base URL must not carry credentials, a query or a fragment"
            )
        try:
            parsed.port  # noqa: B018 (validates the port)
        except ValueError:
            raise ValueError("the Ollama base URL has an invalid port") from None
        loopback = is_loopback(host)
        if parsed.scheme == "http" and not loopback and host not in plain_http_hosts:
            raise ValueError("a remote Ollama server must use https")
        self.model = model.strip()
        self._think = think
        self.external = not loopback
        self._url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/api/chat"
        check_budgets(
            call_timeout_seconds,
            stage_timeout_seconds,
            first_byte_timeout_seconds,
            max_concurrent_calls,
        )
        self.call_timeout_seconds = call_timeout_seconds
        self.stage_timeout_seconds = stage_timeout_seconds
        # The deadline is checked between reads, and one read or the connect can take a further
        # `first_byte` seconds, so the worker thread ends before the agent's own timeout does.
        self._deadline = call_timeout_seconds - first_byte_timeout_seconds - 1.0
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_calls, thread_name_prefix="ollama"
        )
        self._transport: Transport = transport or functools.partial(
            _post,
            slots=threading.BoundedSemaphore(max_concurrent_calls),
            socket_timeout=first_byte_timeout_seconds,
        )

    def _body(self, request: LLMRequest) -> bytes:
        options: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "options": options,
        }
        if self._think is not None:
            payload["think"] = self._think
        if request.json_schema is not None:
            payload["format"] = request.json_schema
        return json.dumps(payload).encode()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = self._body(request)
        # The calls run on this provider's own small pool and queue there, so a line of slow model
        # calls can never use up the default threads the data connectors need.
        raw = await asyncio.get_running_loop().run_in_executor(
            self._executor, self._transport, self._url, body, self._deadline
        )
        return LLMResponse(text=_join_stream(raw), provider=self.name, model=self.model)


def _factory(settings: Settings) -> OllamaProvider:
    base = str(settings.llm_base_url) if settings.llm_base_url else DEFAULT_BASE_URL
    return OllamaProvider(
        settings.llm_model or "",
        base,
        plain_http_hosts=settings.plain_http_host_set(),
        call_timeout_seconds=settings.llm_call_timeout_seconds,
        stage_timeout_seconds=settings.llm_stage_timeout_seconds,
        first_byte_timeout_seconds=settings.llm_first_byte_timeout_seconds,
        max_concurrent_calls=settings.llm_max_concurrent_calls,
    )


register_provider("ollama", _factory)
