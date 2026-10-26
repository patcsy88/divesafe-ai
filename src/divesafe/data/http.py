"""Minimal HTTP seam so connectors can be tested with recorded fixtures (no live calls).

Hardening: https only, host allow-list (applied to the first request and every redirect), at
most two redirects, a response size cap and a total deadline. Error text never includes query
strings.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Any, Protocol

from divesafe.data.errors import ConnectorResponseError, ConnectorTransportError

USER_AGENT = "divesafe-ai/0.1 (non-commercial research; decision support only)"
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"marine-api.open-meteo.com", "api.open-meteo.com", "api.data.gov.my"}
)
# Kept out of ALLOWED_HOSTS on purpose: only a getter built with a credential may call it.
MET_API_HOSTS: frozenset[str] = frozenset({"api.met.gov.my"})
# Also kept out of ALLOWED_HOSTS: only the POST getter below may call it.
PHN_HOSTS: frozenset[str] = frozenset({"etide.hydro.gov.my"})
PHN_MAX_BYTES = 512 * 1024
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 2
MAX_JSON_DEPTH = 32  # a parsing bound for hostile bodies, not a safety limit
_READ_CHUNK = 16 * 1024
logger = logging.getLogger(__name__)


class JsonGetter(Protocol):
    async def get_json(self, url: str, params: Mapping[str, str]) -> Any: ...


def _label(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.hostname}{parsed.path}"


def check_url(url: str, allowed_hosts: Collection[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ConnectorTransportError(f"refusing non-https URL for {_label(url)}")
    if parsed.hostname not in allowed_hosts:
        raise ConnectorTransportError(f"host not allowed: {parsed.hostname}")
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if port not in (None, 443) or parsed.username is not None or parsed.password is not None:
        raise ConnectorTransportError(f"refusing a non-standard port or userinfo for {_label(url)}")


class _DeadlineExceeded(Exception):
    """The total deadline passed inside the worker thread (converted to a fixed-text error)."""


_socket_warned = False


def _socket_of(response: Any) -> Any:
    """The response's socket, or None. It is reached through private http.client/urllib
    attributes, so a Python change can break it; that is reported once, with fixed text."""
    global _socket_warned
    try:
        return response.fp.raw._sock
    except AttributeError:
        if not _socket_warned:
            _socket_warned = True
            logger.warning(
                "connector HTTP socket not reachable: the per-read deadline and the "
                "cancel-time socket close are inactive",
                extra={"http_socket_access": "unavailable"},
            )
        return None


def _tighten_socket_timeout(response: Any, remaining: float) -> None:
    """Only ever lowers the socket timeout, to what is left of the total deadline."""
    sock = _socket_of(response)
    if sock is None:
        return
    with contextlib.suppress(OSError, ValueError):
        get = getattr(sock, "gettimeout", None)
        current = get() if callable(get) else None
        sock.settimeout(max(remaining if current is None else min(current, remaining), 0.001))


def read_limited(response: IO[bytes], max_bytes: int, deadline_at: float | None = None) -> bytes:
    """Read at most max_bytes (refusing more), stopping at the deadline or when cancelled.

    With a deadline, each pass makes one receive (`read1`, which does not loop to fill the
    request), so a slow drip cannot stretch one call into many socket timeouts.
    """
    if deadline_at is None:
        body = response.read(max_bytes + 1)
    else:
        job = getattr(_local, "job", None)
        read_some = getattr(response, "read1", None) or response.read
        chunks: list[bytes] = []
        size = 0
        while size <= max_bytes:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0 or (job is not None and job.cancelled.is_set()):
                raise _DeadlineExceeded
            _tighten_socket_timeout(response, remaining)
            chunk = read_some(min(_READ_CHUNK, max_bytes + 1 - size))
            if not chunk:  # b"" is end of stream; a short chunk just loops
                break
            chunks.append(chunk)
            size += len(chunk)
        body = b"".join(chunks)
    if len(body) > max_bytes:
        raise ConnectorResponseError(f"response exceeds the {max_bytes}-byte limit")
    # The stream ended (the cap was not hit). http.client leaves `length` as the Content-Length
    # still owed on a fixed-length body, so a positive value means the connection dropped early.
    # A missing, None or non-int length (chunked, or a fake) is unknown: no check.
    owed = getattr(response, "length", None)
    if isinstance(owed, int) and not isinstance(owed, bool) and owed > 0:
        raise ConnectorResponseError("response was truncated")
    return body


class _Job:
    """One call's deadline and cancel flag, shared by the awaiting task and its worker thread.

    The worker registers its response socket once; the event-loop side closes it on cancel so a
    blocked read ends. Registering after cancel closes at once; closing twice is harmless."""

    def __init__(self, label: str, deadline_seconds: float) -> None:
        self.label = label
        self.deadline_at = time.monotonic() + deadline_seconds
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._sock: Any = None

    def attach(self, sock: Any) -> None:
        if sock is None:
            return
        with self._lock:
            if not self.cancelled.is_set():
                self._sock = sock
                return
        _close_socket(sock)

    def detach(self) -> None:
        """Forget the socket once reading has ended (http.client closes it itself), so a later
        abort cannot touch a descriptor the OS may have reused. Idempotent; never closes."""
        with self._lock:
            self._sock = None

    def abort(self) -> None:
        """Set the cancel flag (before taking the lock, so attach cannot miss it) and close.

        The close happens under the lock, so detach cannot return while a close is in progress."""
        self.cancelled.set()
        with self._lock:
            sock, self._sock = self._sock, None
            if sock is not None:
                _close_socket(sock)


def _close_socket(sock: Any) -> None:
    with contextlib.suppress(OSError, ValueError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError, ValueError):
        sock.close()


_local = threading.local()
# A dedicated pool shared by all connectors: a stuck upstream can use at most these threads and
# never the default asyncio pool that asyncio.to_thread users rely on.
# HTTP_POOL_WORKERS is the small default, used by direct callers and tests that build getters
# without an app (threads start lazily, so an unused default pool costs nothing). The app replaces
# it at startup with a pool sized from its settings (configure_http_pool).
HTTP_POOL_WORKERS = 8
# Upper bound for a sized pool. Each worker is an OS thread with its own stack and (at most) one
# open upstream connection; the providers rate-limit us anyway (CachingRateLimitedGetter), so
# beyond this a larger pool buys little and only raises memory use and upstream concurrency.
HTTP_POOL_CEILING = 64
_THREAD_PREFIX = "divesafe-http"
_DEFAULT_POOL = ThreadPoolExecutor(
    max_workers=HTTP_POOL_WORKERS, thread_name_prefix="divesafe-http"
)
_POOL = _DEFAULT_POOL


def http_pool_size(connectors: int, max_runs_in_flight: int) -> int:
    """Calls that can overlap (connectors x runs in flight), within [1, HTTP_POOL_CEILING]."""
    return max(1, min(HTTP_POOL_CEILING, connectors * max_runs_in_flight))


def configure_http_pool(workers: int) -> ThreadPoolExecutor:
    """Install (and return) a pool of `workers` threads (floored at 1, capped at the ceiling).

    Replaces any earlier sized pool without waiting for it; the shared default pool is never shut
    down here. Calls already running on a replaced pool finish there (each is deadline-bound).
    """
    global _POOL
    previous = _POOL
    _POOL = ThreadPoolExecutor(
        max_workers=max(1, min(HTTP_POOL_CEILING, workers)), thread_name_prefix=_THREAD_PREFIX
    )
    if previous is not _DEFAULT_POOL:
        previous.shutdown(wait=False)
    return _POOL


def shutdown_http_pool() -> None:
    """Shut the sized pool down without waiting for blocked workers and fall back to the default.

    Queued, not-yet-started calls are cancelled; running ones end at their own deadline.
    """
    global _POOL
    pool, _POOL = _POOL, _DEFAULT_POOL
    if pool is not _DEFAULT_POOL:
        pool.shutdown(wait=False, cancel_futures=True)


def _timed_out(label: str) -> ConnectorTransportError:
    return ConnectorTransportError(f"request to {label} timed out")


def _run_job(job: _Job, fn: Callable[[str, Mapping[str, str]], Any], url: str, params: Any) -> Any:
    # Queued work whose caller already gave up (or whose deadline passed) never starts.
    if job.cancelled.is_set() or time.monotonic() >= job.deadline_at:
        raise _timed_out(job.label)
    _local.job = job
    try:
        return fn(url, params)
    finally:
        _local.job = None


async def _call_in_pool(
    fn: Callable[[str, Mapping[str, str]], Any],
    url: str,
    params: Mapping[str, str],
    deadline_seconds: float,
) -> Any:
    job = _Job(_label(url), deadline_seconds)
    future = asyncio.get_running_loop().run_in_executor(_POOL, _run_job, job, fn, url, params)
    try:
        return await asyncio.wait_for(future, timeout=deadline_seconds)
    except TimeoutError as exc:
        raise _timed_out(job.label) from exc
    finally:
        # Also covers caller cancellation: a worker not yet started will not start, and one
        # blocked on its socket is unblocked by closing that socket from here.
        job.abort()


def _current_deadline(fallback_seconds: float) -> float:
    job = getattr(_local, "job", None)
    if job is not None:
        return float(job.deadline_at)
    return time.monotonic() + fallback_seconds


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def __init__(self, allowed_hosts: Collection[str]) -> None:
        self._allowed = allowed_hosts

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        check_url(newurl, self._allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """For requests that carry a credential: a redirect is never followed, so the header cannot
    follow it. urllib then raises HTTPError for the 3xx, which is reported as an HTTP status."""

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        return None


class UrllibJsonGetter:
    def __init__(
        self,
        timeout_seconds: float = 15.0,
        deadline_seconds: float = 30.0,
        allowed_hosts: Collection[str] = ALLOWED_HOSTS,
        max_bytes: int = MAX_BYTES,
        *,
        auth_header: tuple[str, str] | None = None,
    ) -> None:
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be a collection of host names")
        hosts = frozenset(allowed_hosts)
        if auth_header is not None and (not hosts or not hosts <= MET_API_HOSTS):
            raise ValueError("a getter with a credential needs its own host list")
        if hosts & PHN_HOSTS:
            raise ValueError("the PHN host may only be called by the PHN POST getter")
        self._timeout = timeout_seconds
        self._deadline = deadline_seconds
        self._allowed = hosts
        self._max_bytes = max_bytes
        self._auth_header = auth_header

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        check_url(url, self._allowed)
        try:
            return await _call_in_pool(self._get, url, params, self._deadline)
        except TimeoutError as exc:
            raise _timed_out(_label(url)) from exc

    def _get(self, url: str, params: Mapping[str, str]) -> Any:
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._auth_header is not None:
            opener = urllib.request.build_opener(_NoRedirect())
        else:
            opener = urllib.request.build_opener(_SafeRedirect(self._allowed))
        request = urllib.request.Request(  # noqa: S310 (scheme and host checked above)
            full, headers=headers
        )
        if self._auth_header is not None:
            request.add_unredirected_header(*self._auth_header)
        return _open_json(opener, request, url, self._timeout, self._max_bytes)


def _open_json(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    url: str,
    timeout: float,
    max_bytes: int,
) -> Any:
    deadline_at = _current_deadline(timeout)
    job: _Job | None = getattr(_local, "job", None)

    def gave_up() -> bool:
        return time.monotonic() >= deadline_at or (job is not None and job.cancelled.is_set())

    if gave_up():
        raise _timed_out(_label(url))
    try:
        # The socket timeout never exceeds what is left of the total deadline.
        with opener.open(request, timeout=min(timeout, deadline_at - time.monotonic())) as response:
            try:
                if job is not None:
                    job.attach(_socket_of(response))  # closes it at once if already cancelled
                body = read_limited(response, max_bytes, deadline_at)
            finally:
                if job is not None:
                    job.detach()
    except _DeadlineExceeded as exc:
        raise _timed_out(_label(url)) from exc
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(OSError):
            exc.close()
        if gave_up():
            raise _timed_out(_label(url)) from exc
        raise ConnectorTransportError(f"HTTP {exc.code} from {_label(url)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A socket closed by the caller's cancel surfaces here (or below) as an ordinary error.
        if gave_up():
            raise _timed_out(_label(url)) from exc
        raise ConnectorTransportError(f"request to {_label(url)} failed") from exc
    except (http.client.HTTPException, ValueError):
        # The text of these can echo a header value, so it is dropped.
        if gave_up():
            raise _timed_out(_label(url)) from None
        echoed = True
    else:
        echoed = False
    if echoed:
        raise ConnectorTransportError(f"request to {_label(url)} failed")
    # The body is parsed whatever the Content-Type says: PHN sends JSON labelled text/html.
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError) as exc:
        raise ConnectorResponseError(f"{_label(url)} did not return valid JSON") from exc
    if _depth_exceeds(parsed, MAX_JSON_DEPTH):
        raise ConnectorResponseError(f"{_label(url)} returned JSON nested too deeply")
    return parsed


def _depth_exceeds(value: Any, limit: int) -> bool:
    """Iterative, so a hostile body cannot overflow the stack here or in a later deep copy."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth > limit:
                return True
            children = item.values() if isinstance(item, dict) else item
            stack.extend((c, depth + 1) for c in children if isinstance(c, dict | list))
    return False


class UrllibFormPostJsonGetter:
    """POSTs `params` as a form and reads JSON, for the PHN e-Tide endpoint only.

    It sends no credential, keeps no cookie (the session cookie PHN sets is ignored), follows no
    redirect, and may only be built for the PHN host. `params` is the form body, not a query.
    """

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        deadline_seconds: float = 30.0,
        allowed_hosts: Collection[str] = PHN_HOSTS,
        max_bytes: int = PHN_MAX_BYTES,
    ) -> None:
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be a collection of host names")
        hosts = frozenset(allowed_hosts)
        if not hosts or not hosts <= PHN_HOSTS:
            raise ValueError("this getter may only call the PHN e-Tide host")
        self._timeout = timeout_seconds
        self._deadline = deadline_seconds
        self._allowed = hosts
        self._max_bytes = max_bytes

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        check_url(url, self._allowed)
        try:
            return await _call_in_pool(self._post, url, params, self._deadline)
        except TimeoutError as exc:
            raise _timed_out(_label(url)) from exc

    def _post(self, url: str, params: Mapping[str, str]) -> Any:
        request = urllib.request.Request(  # noqa: S310 (scheme and host checked above)
            url,
            data=urllib.parse.urlencode(params).encode("ascii"),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                # The e-Tide page's own script sends this; it is what the one recorded call used.
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect())
        return _open_json(opener, request, url, self._timeout, self._max_bytes)
