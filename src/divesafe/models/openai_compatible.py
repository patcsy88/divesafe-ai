"""OpenAI-compatible chat adapter (ADR 0018): vLLM, llama.cpp server, LM Studio, or a hosted
service that speaks `/chat/completions`.

The reply is streamed (server-sent events). It counts only if a chunk ends with `finish_reason`
"stop" and the stream then ends with `[DONE]`; a cut-off, a token-limit stop, an error object or
any odd shape is refused, never repaired. Reasoning text is never used.

Never logs a request (it holds the whole prompt) and never puts the API key, a URL or a response
in an error. Redirects are refused and environment proxies are ignored. A loopback server may use
plain http. Any other host must use https and be listed in `DIVESAFE_LLM_ALLOWED_HOSTS`, and it
counts as external (the prompt leaves this machine). A loopback address does not show the
prompt stays here: a proxy or tunnel can forward it (ADR 0023).
"""

from __future__ import annotations

import asyncio
import functools
import http.client
import json
import re
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
from divesafe.models.llm import LLMRequest, LLMResponse, TokenLimitError, register_provider
from divesafe.models.loopback import (
    LoopbackHTTPHandler,
    LoopbackHTTPSHandler,
    NotOnThisMachineError,
    is_loopback,
)
from divesafe.models.ollama import (
    MAX_RESPONSE_BYTES,
    MAX_STREAM_LINES,
    SOCKET_TIMEOUT_SECONDS,
    check_budgets,
    check_output_tokens,
)

_CHUNK = 64 * 1024
_KEY = re.compile(r"[\x21-\x7e]{1,512}")  # printable ASCII, no whitespace: no header injection
_SCHEMA_NAME = "divesafe_output"

Transport = Callable[[str, bytes, float], bytes]


FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "unreachable",
        "read_timeout",
        "connection_error",
        "http_error",
        "deadline",
        "too_large",
        "slots_full",
        "closed",
        "bad_event_json",
        "error_event",
        "no_done_marker",
        "data_after_done",
        "event_line",
        "tool_or_refusal",
        "bad_shape",
        "empty_stream",
        "token_limit",
        "unclassified",
    }
)


class OpenAICompatibleError(RuntimeError):
    """The server was unreachable or returned something unusable. Messages are fixed strings.

    `reason` is a fixed label from FAILURE_REASONS, for the recorder. It carries no message text.
    """

    reason: str

    def __init__(self, *args: object, reason: str = "unclassified") -> None:
        super().__init__(*args)
        self.reason = reason


class OpenAICompatibleTokenLimitError(OpenAICompatibleError, TokenLimitError):
    """The reply stopped at the output-token limit."""

    def __init__(self) -> None:
        TokenLimitError.__init__(self)
        self.reason = "token_limit"


def failure_reason(exc: BaseException) -> str:
    """The fixed failure label for `exc`, or "unclassified" if it has none."""
    if isinstance(exc, OpenAICompatibleError):
        reason = vars(exc).get("reason", "unclassified")
        if type(reason) is str and reason in FAILURE_REASONS:
            return next(label for label in FAILURE_REASONS if label == reason)
    return "unclassified"


def _refuse(reason: str) -> OpenAICompatibleError:
    """A fresh refusal with the fixed shape message and the given label."""
    return OpenAICompatibleError("the model response was not in the expected shape", reason=reason)


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
        *(
            [LoopbackHTTPHandler(), LoopbackHTTPSHandler(context=tls)]
            if loopback
            else [urllib.request.HTTPSHandler(context=tls)]
        ),
    )
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
    request = urllib.request.Request(  # noqa: S310 (scheme and host checked at construction)
        url, data=body, headers=headers, method="POST"
    )
    if not slots.acquire(timeout=timeout):
        raise OpenAICompatibleError(
            "too many model calls are already in flight", reason="slots_full"
        )
    try:
        if time.monotonic() >= deadline:
            raise OpenAICompatibleError(
                "the model call exceeded its total deadline", reason="deadline"
            )
        chunks: list[bytes] = []
        size = 0
        with opener.open(request, timeout=socket_timeout) as response:
            while True:
                if time.monotonic() > deadline:
                    raise OpenAICompatibleError(
                        "the model call exceeded its total deadline", reason="deadline"
                    )
                chunk: bytes = response.read1(min(_CHUNK, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise OpenAICompatibleError(
                        "the model response was too large", reason="too_large"
                    )
    except OpenAICompatibleError:
        raise
    except NotOnThisMachineError:
        raise OpenAICompatibleError(
            "the model server is not on this machine", reason="unreachable"
        ) from None
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OpenAICompatibleError(
            "the model server could not be reached or refused the request", reason="http_error"
        ) from None
    except TimeoutError:
        raise OpenAICompatibleError(
            "the model server could not be reached or refused the request", reason="read_timeout"
        ) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        raise OpenAICompatibleError(
            "the model server could not be reached or refused the request",
            reason="connection_error",
        ) from None
    finally:
        slots.release()
    return b"".join(chunks)


def join_sse(raw: bytes) -> str:
    """Join the streamed `data:` events into the reply text, or refuse the whole reply."""
    try:
        lines = re.split(r"\r\n|\r|\n", raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise _refuse("bad_shape") from None
    if not lines or len(lines) > MAX_STREAM_LINES:
        raise _refuse("bad_shape")
    parts: list[str] = []
    finished = False
    done = False
    seen_data = False
    for line in lines:
        if not line.strip() or line.startswith(":"):  # blank or keep-alive comment
            continue
        if not line.startswith("data:"):
            raise _refuse("event_line")  # an unknown field
        if done:
            raise _refuse("data_after_done")  # a data line after [DONE]
        seen_data = True
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            event = json.loads(payload)
        except (ValueError, RecursionError):
            raise _refuse("bad_event_json") from None
        if not isinstance(event, dict):
            raise _refuse("bad_shape")
        if "error" in event:
            raise _refuse("error_event")
        choices = event.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise _refuse("bad_shape")
        if not choices:
            continue  # a usage-only event
        choice = choices[0]
        if not isinstance(choice, dict) or finished or choice.get("index", 0) != 0:
            raise _refuse("bad_shape")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise _refuse("bad_shape")
        if any(delta.get(k) for k in ("tool_calls", "function_call", "refusal")):
            # a reply that asks for a tool or refuses is not a plain answer
            raise _refuse("tool_or_refusal")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _refuse("bad_shape")
            parts.append(content)
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason == "length":
                raise OpenAICompatibleTokenLimitError
            if reason != "stop":
                raise _refuse("bad_shape")
            finished = True
    if not (finished and done):
        raise _refuse("no_done_marker" if seen_data else "empty_stream")
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
        max_output_tokens: int = 1500,
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
        check_output_tokens(max_output_tokens)
        self.max_output_tokens = max_output_tokens
        self.call_timeout_seconds = call_timeout_seconds
        self.stage_timeout_seconds = stage_timeout_seconds
        # The deadline is checked between reads; one read or the connect can take a further
        # `first_byte` seconds, so the worker thread ends before the agent's own timeout does.
        self._deadline = call_timeout_seconds - first_byte_timeout_seconds - 1.0
        self._closed = False
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

    def close(self) -> None:
        """Refuse new calls and let the worker threads end; a call in flight is not waited for."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if self._closed:
            raise OpenAICompatibleError("the model provider is closed", reason="closed")
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
        max_output_tokens=settings.llm_max_output_tokens,
    )


register_provider("openai_compatible", _factory)
