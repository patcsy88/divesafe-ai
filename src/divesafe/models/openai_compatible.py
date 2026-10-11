"""OpenAI-compatible chat adapter (ADR 0018): vLLM, llama.cpp server, LM Studio, or a hosted
service that speaks `/chat/completions`.

The reply is streamed (server-sent events). It counts only if a chunk ends with `finish_reason`
"stop" and the stream then ends with `[DONE]`; a cut-off, a token-limit stop, an error object or
any odd shape is refused, never repaired. Reasoning text is never used.

Never logs a request (it holds the whole prompt) and never puts the API key, a URL or a response
in an error. Redirects are refused and environment proxies are ignored. A loopback server may use
plain http. Any other host must use https and be listed in `DIVESAFE_LLM_ALLOWED_HOSTS`, and it
counts as external (the prompt leaves this machine).
"""

from __future__ import annotations

import asyncio
import functools
import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import SecretStr

from divesafe.config.settings import Settings
from divesafe.models.llm import LLMRequest, LLMResponse, register_provider
from divesafe.models.ollama import (
    MAX_RESPONSE_BYTES,
    MAX_STREAM_LINES,
    SOCKET_TIMEOUT_SECONDS,
    check_budgets,
    is_loopback,
)

_CHUNK = 64 * 1024
_KEY = re.compile(r"[\x21-\x7e]{1,512}")  # printable ASCII, no whitespace: no header injection
_SCHEMA_NAME = "divesafe_output"

Transport = Callable[[str, bytes, float], bytes]


class OpenAICompatibleError(RuntimeError):
    """The server was unreachable or returned something unusable. Messages are fixed strings."""


class _LoopbackConnection(http.client.HTTPConnection):
    """Plain http is allowed only to a loopback server. `localhost` is resolved by the system, so
    the address actually connected to is checked before anything (prompt, key) is sent."""

    def connect(self) -> None:
        super().connect()
        sock = self.sock
        peer = sock.getpeername()[0] if isinstance(sock, socket.socket) else ""
        if not is_loopback(str(peer).split("%", 1)[0]):
            self.close()
            raise OpenAICompatibleError("the model server is not on this machine")


class _LoopbackHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_LoopbackConnection, req)


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
    api_key: SecretStr | None,
    loopback: bool,
    slots: threading.BoundedSemaphore,
    socket_timeout: float,
) -> bytes:
    """POST with no proxy, no redirects, a size cap and a total deadline (`timeout` seconds)."""
    deadline = time.monotonic() + timeout
    tls = ssl.create_default_context()  # verifies the certificate and the host name
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect,
        urllib.request.HTTPSHandler(context=tls),
        *([_LoopbackHTTPHandler()] if loopback else []),
    )
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
    request = urllib.request.Request(  # noqa: S310 (scheme and host checked at construction)
        url, data=body, headers=headers, method="POST"
    )
    if not slots.acquire(timeout=timeout):
        raise OpenAICompatibleError("too many model calls are already in flight")
    try:
        if time.monotonic() >= deadline:
            raise OpenAICompatibleError("the model call exceeded its total deadline")
        chunks: list[bytes] = []
        size = 0
        with opener.open(request, timeout=socket_timeout) as response:
            while True:
                if time.monotonic() > deadline:
                    raise OpenAICompatibleError("the model call exceeded its total deadline")
                chunk: bytes = response.read1(min(_CHUNK, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise OpenAICompatibleError("the model response was too large")
    except OpenAICompatibleError:
        raise
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OpenAICompatibleError(
            "the model server could not be reached or refused the request"
        ) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        raise OpenAICompatibleError(
            "the model server could not be reached or refused the request"
        ) from None
    finally:
        slots.release()
    return b"".join(chunks)


def join_sse(raw: bytes) -> str:
    """Join the streamed `data:` events into the reply text, or refuse the whole reply."""
    bad = OpenAICompatibleError("the model response was not in the expected shape")
    try:
        lines = re.split(r"\r\n|\r|\n", raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise bad from None
    if not lines or len(lines) > MAX_STREAM_LINES:
        raise bad
    parts: list[str] = []
    finished = False
    done = False
    for line in lines:
        if not line.strip() or line.startswith(":"):  # blank or keep-alive comment
            continue
        if done or not line.startswith("data:"):
            raise bad  # something follows [DONE], or an unknown field
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            event = json.loads(payload)
        except (ValueError, RecursionError):
            raise bad from None
        if not isinstance(event, dict) or "error" in event:
            raise bad
        choices = event.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise bad
        if not choices:
            continue  # a usage-only event
        choice = choices[0]
        if not isinstance(choice, dict) or finished or choice.get("index", 0) != 0:
            raise bad
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise bad
        if any(delta.get(k) for k in ("tool_calls", "function_call", "refusal")):
            raise bad  # a reply that asks for a tool or refuses is not a plain answer
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise bad
            parts.append(content)
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason != "stop":
                raise bad
            finished = True
    if not (finished and done):
        raise bad
    return "".join(parts)


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        model: str,
        base_url: str,
        *,
        allowed_hosts: frozenset[str] = frozenset(),
        api_key: SecretStr | None = None,
        transport: Transport | None = None,
        call_timeout_seconds: float = 60.0,
        stage_timeout_seconds: float = 150.0,
        first_byte_timeout_seconds: float = SOCKET_TIMEOUT_SECONDS,
        max_concurrent_calls: int = 4,
    ) -> None:
        if not model.strip():
            raise ValueError("a model name is required (DIVESAFE_LLM_MODEL)")
        try:
            parsed = urllib.parse.urlsplit(base_url)
        except ValueError:
            raise ValueError("the model base URL is not a valid URL") from None
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not host:
            raise ValueError("the model base URL must be an http(s) URL with a host")
        if "@" in parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("the model base URL must not carry credentials, a query or a fragment")
        try:
            parsed.port  # noqa: B018 (validates the port)
        except ValueError:
            raise ValueError("the model base URL has an invalid port") from None
        loopback = is_loopback(host)
        if not loopback:
            if parsed.scheme != "https":
                raise ValueError("a remote model server must use https")
            if host not in allowed_hosts:
                raise ValueError("the model server host is not in DIVESAFE_LLM_ALLOWED_HOSTS")
        secret = api_key.get_secret_value() if api_key is not None else ""
        if api_key is not None and not _KEY.fullmatch(secret):
            raise ValueError("the model API key is not a plausible bearer token")
        self.model = model.strip()
        self.external = not loopback
        self._url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/chat/completions"
        check_budgets(
            call_timeout_seconds,
            stage_timeout_seconds,
            first_byte_timeout_seconds,
            max_concurrent_calls,
        )
        self.call_timeout_seconds = call_timeout_seconds
        self.stage_timeout_seconds = stage_timeout_seconds
        # The deadline is checked between reads; one read or the connect can take a further
        # `first_byte` seconds, so the worker thread ends before the agent's own timeout does.
        self._deadline = call_timeout_seconds - first_byte_timeout_seconds - 1.0
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_calls, thread_name_prefix="openai-compat"
        )
        self._transport: Transport = transport or functools.partial(
            _post,
            api_key=api_key,
            loopback=loopback,
            slots=threading.BoundedSemaphore(max_concurrent_calls),
            socket_timeout=first_byte_timeout_seconds,
        )

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider(model={self.model!r}, external={self.external})"

    def _body(self, request: LLMRequest) -> bytes:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "temperature": request.temperature,
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": _SCHEMA_NAME, "schema": request.json_schema},
            }
        return json.dumps(payload).encode()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = self._body(request)
        raw = await asyncio.get_running_loop().run_in_executor(
            self._executor, self._transport, self._url, body, self._deadline
        )
        return LLMResponse(text=join_sse(raw), provider=self.name, model=self.model)


def _factory(settings: Settings) -> OpenAICompatibleProvider:
    if settings.llm_base_url is None:
        raise ValueError("DIVESAFE_LLM_BASE_URL is required for the openai_compatible provider")
    return OpenAICompatibleProvider(
        settings.llm_model or "",
        str(settings.llm_base_url),
        allowed_hosts=settings.llm_allowed_host_set(),
        api_key=settings.llm_api_key,
        call_timeout_seconds=settings.llm_call_timeout_seconds,
        stage_timeout_seconds=settings.llm_stage_timeout_seconds,
        first_byte_timeout_seconds=settings.llm_first_byte_timeout_seconds,
        max_concurrent_calls=settings.llm_max_concurrent_calls,
    )


register_provider("openai_compatible", _factory)
