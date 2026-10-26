"""Per-read receive, socket close on cancel and cancelled-job handling of `divesafe.data.http`
(security M1 fix), for BOTH getters (GET `UrllibJsonGetter`, POST `UrllibFormPostJsonGetter`).

Offline: no network and no database. Fake responses cover the logic; a REAL
`http.client.HTTPResponse` on a `socket.socketpair()` covers the private path
(`response.fp.raw._sock`), the real blocked `read1` and the close from another thread. Every
socketpair end is closed in a `finally`. Each scenario takes a module (the real one or a mutated
COPY made with `mutated_module`) and returns what it got wrong, so the same checks serve the
behaviour tests and the mutation tests. A crashing mutant is not a refusal: the mutation tests
need one non-crash miss.

EQUIVALENT MUTANTS (not tested):
  - `getattr(response, "read1", None) or response.read` as an explicit `hasattr` test;
  - `min(_READ_CHUNK, x)` as `min(x, _READ_CHUNK)`; `<` as `<=` against a monotonic reading;
  - `_Job.attach` storing the socket before or after the `cancelled` test while the lock is held
    (both are under the lock; the order inside it is not observable);
  - `shutdown(SHUT_RD)` versus `SHUT_RDWR`: the fake records the argument, but no behaviour here
    depends on the direction once `close` follows.

GAPS:
  - A read blocked in connect, DNS or the TLS handshake (before `opener.open` returns a response)
    has no socket to close: it is NOT tested, and `docs/data-sources.md` lists it as a known gap.
  - The worker of a closed socket may see EOF (an empty `read1`) and report "not valid JSON"; its
    result is discarded because the caller already gave up, so that text is not asserted.
  - macOS and Linux behaviour of `shutdown` waking a blocked `recv` is what the real-socket test
    relies on; it has been run on macOS only.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import io
import logging
import socket
import threading
import time
import types
import urllib.error
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_http_deadline_pool import (
    KINDS,
    LABEL,
    PARAMS,
    URL,
    Opener,
    install,
    is_text,
    make,
    timed_out,
)

from divesafe.data import http as http_module
from divesafe.data.errors import ConnectorResponseError, ConnectorTransportError

pytestmark = pytest.mark.safety

WARN_TEXT = (
    "connector HTTP socket not reachable: the per-read deadline and the "
    "cancel-time socket close are inactive"
)
SECRET_URL = "https://secret.example/x?token=SEKRET"
CLOSED = [("shutdown", socket.SHUT_RDWR), ("close",)]


@pytest.fixture(autouse=True)
def restore_module_state() -> Iterator[None]:
    flag = http_module._socket_warned
    yield
    http_module._socket_warned = flag
    http_module._local.job = None


def attempt(bad: list[str], label: str, call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{label}: raised {type(exc).__name__}")
        return None


# --- (a) read_limited and read1 -----------------------------------------------------------------


class Reader:
    """Has read1 and read; records the size asked of each."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.read1_calls: list[int] = []
        self.read_calls: list[int] = []

    def read1(self, n: int = -1) -> bytes:
        self.read1_calls.append(n)
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, n: int = -1) -> bytes:
        self.read_calls.append(n)
        return b""


class OnlyRead:
    """No read1: the fallback must use read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.calls: list[int] = []

    def read(self, n: int = -1) -> bytes:
        self.calls.append(n)
        return self.chunks.pop(0) if self.chunks else b""


def later() -> float:
    return time.monotonic() + 30


def s_read1(mod: Any) -> list[str]:
    bad: list[str] = []
    mod._local.job = None
    r = Reader([b"ab", b"cd", b""])
    body = attempt(bad, "short chunks", lambda: mod.read_limited(r, 100, later()))
    if body != b"abcd" or r.read1_calls != [101, 99, 97] or r.read_calls:
        bad.append(f"short chunks: {body!r} read1={r.read1_calls} read={r.read_calls}")
    big = Reader([b"x" * 16384, b"x" * 16384, b"x" * 10, b""])
    body = attempt(bad, "big", lambda: mod.read_limited(big, 100_000, later()))
    if big.read1_calls != [16384] * 4 or body != b"x" * 32778 or big.read_calls:
        bad.append(f"chunk cap: {big.read1_calls}")
    over = Reader([b"x" * 4, b"x" * 4, b"x" * 4])
    try:
        mod.read_limited(over, 10, later())
        bad.append("a body over the cap was returned (drip)")
    except ConnectorResponseError:
        pass
    if over.read1_calls != [11, 7, 3]:
        bad.append(f"cap not applied per call: {over.read1_calls}")
    exact = Reader([b"x" * 5, b"x" * 5, b""])
    body = attempt(bad, "exact cap", lambda: mod.read_limited(exact, 10, later()))
    if body != b"x" * 10 or exact.read1_calls != [11, 6, 1]:
        bad.append(f"a body exactly at the cap: {body!r} {exact.read1_calls}")
    one = Reader([b"x" * 11])
    try:
        mod.read_limited(one, 10, later())
        bad.append("max_bytes+1 bytes in one chunk were accepted")
    except ConnectorResponseError:
        pass
    if one.read1_calls != [11]:
        bad.append(f"the read went on after the cap: {one.read1_calls}")
    empty = Reader([b""])
    body = attempt(bad, "empty", lambda: mod.read_limited(empty, 100, later()))
    if body != b"" or empty.read1_calls != [101]:
        bad.append(f"an empty chunk is not end of stream: {empty.read1_calls}")
    plain = Reader([b"zz"])
    attempt(bad, "no deadline", lambda: mod.read_limited(plain, 100))
    if plain.read_calls != [101] or plain.read1_calls:
        bad.append(f"deadline None: read={plain.read_calls} read1={plain.read1_calls}")
    fallback = OnlyRead([b"ab", b"cd", b""])
    body = attempt(bad, "fallback", lambda: mod.read_limited(fallback, 100, later()))
    if body != b"abcd" or fallback.calls != [101, 99, 97]:
        bad.append(f"fallback to read: {body!r} {fallback.calls}")
    return bad


def test_read_limited_uses_one_read1_per_pass_capped_and_falls_back() -> None:
    found = s_read1(http_module)
    assert found == []


# --- (b) _Job.attach / abort --------------------------------------------------------------------


class FakeSock:
    def __init__(
        self, shutdown_exc: Exception | None = None, close_exc: Exception | None = None
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.shutdown_exc = shutdown_exc
        self.close_exc = close_exc

    def shutdown(self, how: int) -> None:
        self.calls.append(("shutdown", how))
        if self.shutdown_exc is not None:
            raise self.shutdown_exc

    def close(self) -> None:
        self.calls.append(("close",))
        if self.close_exc is not None:
            raise self.close_exc


def s_job(mod: Any) -> list[str]:
    bad: list[str] = []
    job = mod._Job("l", 30)
    sock = FakeSock()
    job.attach(sock)
    if job.cancelled.is_set() or sock.calls:
        bad.append("attach before cancel closed or cancelled")
    job.abort()
    if sock.calls != CLOSED or not job.cancelled.is_set():
        bad.append(f"abort did not shutdown(SHUT_RDWR) then close: {sock.calls}")
    job.abort()
    if sock.calls != CLOSED:
        bad.append(f"a second abort closed again: {sock.calls}")
    after = mod._Job("l", 30)
    after.abort()
    late = FakeSock()
    after.attach(late)
    if late.calls != CLOSED or after._sock is not None:
        bad.append(f"attach after abort did not close at once: {late.calls}")
    attempt(bad, "attach(None) after abort", lambda: after.attach(None))
    fresh = mod._Job("l", 30)
    fresh.attach(None)
    attempt(bad, "abort without a socket", fresh.abort)
    for exc in (OSError("x"), ValueError("x")):
        for kwargs in ({"shutdown_exc": exc}, {"close_exc": exc}):
            odd = FakeSock(**kwargs)
            holder = mod._Job("l", 30)
            holder.attach(odd)
            attempt(bad, f"abort with {exc!r}", holder.abort)
            if odd.calls != CLOSED:
                bad.append(f"a suppressed error stopped the close: {odd.calls}")
            odd_late = FakeSock(**kwargs)
            ended = mod._Job("l", 30)
            ended.abort()
            attempt(bad, f"late attach with {exc!r}", lambda s=odd_late, j=ended: j.attach(s))
            if odd_late.calls != CLOSED:
                bad.append(f"late attach, suppressed error: {odd_late.calls}")
    for kwargs in ({"shutdown_exc": RuntimeError("x")}, {"close_exc": RuntimeError("x")}):
        loud = FakeSock(**kwargs)
        holder = mod._Job("l", 30)
        holder.attach(loud)
        try:
            holder.abort()
            bad.append("an unexpected error was swallowed by abort")
        except RuntimeError:
            pass
    locked = mod._Job("l", 30)
    locked._lock.acquire()
    worker = threading.Thread(target=locked.abort, daemon=True)
    worker.start()
    try:
        if not locked.cancelled.wait(1.0):
            bad.append("abort set the cancel flag only after taking the lock")
    finally:
        locked._lock.release()
        worker.join(2)
    for _ in range(200):
        racing, target = mod._Job("l", 30), FakeSock()
        pair = [
            threading.Thread(target=racing.attach, args=(target,)),
            threading.Thread(target=racing.abort),
        ]
        for t in pair:
            t.start()
        for t in pair:
            t.join(2)
        if ("close",) not in target.calls:
            bad.append("an attach racing an abort left the socket open")
            break
    return bad


def test_job_attach_and_abort() -> None:
    found = s_job(http_module)
    assert found == []


# --- (c) a REAL HTTPResponse on a socketpair ----------------------------------------------------

HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"


@contextlib.contextmanager
def drip_pair(interval: float, total: int = 60) -> Iterator[tuple[http.client.HTTPResponse, Any]]:
    """A real response whose peer sent the headers and then drips one byte per `interval`."""
    client, peer = socket.socketpair()
    stop = threading.Event()
    response: http.client.HTTPResponse | None = None
    dripper: threading.Thread | None = None
    try:
        peer.sendall(HEAD)
        response = http.client.HTTPResponse(client)
        response.begin()

        def drip() -> None:
            for _ in range(total):
                if stop.wait(interval):
                    return
                try:
                    peer.sendall(b"x")
                except OSError:
                    return

        dripper = threading.Thread(target=drip, daemon=True)
        dripper.start()
        yield response, client
    finally:
        stop.set()
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()
        for end in (client, peer):
            with contextlib.suppress(OSError):
                end.close()
        if dripper is not None:
            dripper.join(2)


def s_real_path(mod: Any) -> list[str]:
    bad: list[str] = []
    with drip_pair(60.0) as (response, sock):
        if mod._socket_of(response) is not sock:
            bad.append("_socket_of did not find response.fp.raw._sock on a real HTTPResponse")
        if not hasattr(response, "read1"):
            bad.append("a real HTTPResponse has no read1")
        sock.settimeout(5.0)
        mod._tighten_socket_timeout(response, 10.0)
        if sock.gettimeout() != 5.0:
            bad.append(f"tighten raised the timeout: {sock.gettimeout()}")
        mod._tighten_socket_timeout(response, 2.0)
        if sock.gettimeout() != 2.0:
            bad.append(f"tighten did not lower the timeout: {sock.gettimeout()}")
        mod._tighten_socket_timeout(response, 10.0)
        if sock.gettimeout() != 2.0:
            bad.append(f"tighten raised a lowered timeout: {sock.gettimeout()}")
        sock.settimeout(None)
        mod._tighten_socket_timeout(response, 3.0)
        if sock.gettimeout() != 3.0:
            bad.append(f"a blocking socket was not given the remaining time: {sock.gettimeout()}")
        mod._tighten_socket_timeout(response, -1.0)
        if sock.gettimeout() != 0.001:
            bad.append(f"the floor is not 1 ms: {sock.gettimeout()}")
        sock.close()
        attempt(bad, "tighten on a closed socket", lambda: mod._tighten_socket_timeout(response, 1))
    return bad


def s_real_body(mod: Any) -> list[str]:
    bad: list[str] = []
    mod._local.job = None
    for body, cap, ok in ((b'{"a": 1}', 1000, True), (b"y" * 5000, 1000, False)):
        client, peer = socket.socketpair()
        try:
            head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body)
            peer.sendall(head + body)
            response = http.client.HTTPResponse(client)
            response.begin()
            try:
                got = mod.read_limited(response, cap, time.monotonic() + 10)
                if not ok or got != body:
                    bad.append(f"real response, {len(body)} bytes: {len(got)} returned")
            except ConnectorResponseError:
                if ok:
                    bad.append("a body under the cap was refused")
        finally:
            for end in (client, peer):
                with contextlib.suppress(OSError):
                    end.close()
    return bad


class Wrapped:
    def __init__(self, response: Any, entered: threading.Event, exited: threading.Event) -> None:
        self.response, self.entered, self.exited = response, entered, exited

    def __enter__(self) -> Any:
        self.entered.set()
        return self.response

    def __exit__(self, *exc: object) -> None:
        self.exited.set()


def s_real_cancel(mod: Any, kind: str, how: str = "cancel") -> list[str]:
    """A worker blocked in a real read1 ends soon after the caller gives up: the peer drips every
    3 s, so only closing the socket (not the next byte) can end it within a second."""
    bad: list[str] = []
    with drip_pair(3.0) as (response, sock):
        entered, exited = threading.Event(), threading.Event()
        opener = Opener(lambda r, t: Wrapped(response, entered, exited))
        with pytest.MonkeyPatch.context() as m:
            install(m, opener)
            deadline = 30.0 if how == "cancel" else 0.5
            getter = make(mod, kind, 30.0, deadline, max_bytes=1_000_000)

            async def main() -> tuple[Exception | None, float, bool]:
                task = asyncio.ensure_future(getter.get_json(URL[kind], PARAMS))
                error: Exception | None = None
                began = time.perf_counter()
                if how == "cancel":
                    await asyncio.sleep(0.3)
                    began = time.perf_counter()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                else:
                    try:
                        await task
                    except Exception as exc:  # noqa: BLE001
                        error = exc
                loop = asyncio.get_running_loop()
                ended = await loop.run_in_executor(None, exited.wait, 1.0)
                return error, time.perf_counter() - began, ended

            error, elapsed, ended = asyncio.run(main())
        if not entered.is_set():
            bad.append("the worker never reached the response")
        if not ended:
            bad.append("the worker was still blocked in read1 a second after the caller gave up")
        if elapsed > 1.4:
            bad.append(f"the worker took {elapsed:.2f}s to end")
        if how == "cancel" and sock.fileno() != -1:
            bad.append("the socket was not closed when the caller cancelled")
        if how == "timeout" and not is_text(error, timed_out(kind)):
            bad.append(f"not the fixed timeout error: {error!r}")
    return bad


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("how", ["cancel", "timeout"])
def test_a_worker_blocked_in_a_real_read1_ends_when_the_caller_gives_up(
    kind: str, how: str
) -> None:
    found = s_real_cancel(http_module, kind, how)
    assert found == []


def test_the_private_socket_path_exists_on_a_real_response_and_tightening_only_lowers() -> None:
    found = s_real_path(http_module)
    assert found == []


def test_a_real_response_is_read_through_read1_and_capped() -> None:
    found = s_real_body(http_module)
    assert found == []


# --- (d) the private path missing ---------------------------------------------------------------


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def s_warn(mod: Any) -> list[str]:
    bad: list[str] = []
    package = logging.getLogger("divesafe")
    capture, level = Capture(), package.level
    package.addHandler(capture)
    package.setLevel(logging.DEBUG)
    try:
        mod._socket_warned = False
        odd = types.SimpleNamespace(url=SECRET_URL)
        results = [mod._socket_of(odd) for _ in range(3)]
        if any(r is not None for r in results):
            bad.append("a missing private path did not return None")
        mod._tighten_socket_timeout(odd, 1.0)
        ours = [r for r in capture.records if r.name == mod.logger.name]
        if len(ours) != 1:
            bad.append(f"the warning was logged {len(ours)} times, not once")
        else:
            record = ours[0]
            if record.levelno != logging.WARNING or record.getMessage() != WARN_TEXT:
                bad.append(f"warning text or level: {record.levelname} {record.getMessage()!r}")
            if getattr(record, "http_socket_access", None) != "unavailable":
                bad.append("the extra http_socket_access='unavailable' is missing")
            dumped = " ".join(str(v) for v in record.__dict__.values())
            if "SEKRET" in dumped or "secret.example" in dumped:
                bad.append("the warning carries the URL")
        body = attempt(bad, "read", lambda: mod.read_limited(io.BytesIO(b"{}"), 100, later()))
        if body != b"{}":
            bad.append("reads stopped working without the private path")
        mod._socket_warned = False
        mod._socket_of(odd)
        again = [r for r in capture.records if r.name == mod.logger.name]
        if len(again) != 2:
            bad.append("resetting the once-flag did not allow a new warning")
    finally:
        package.removeHandler(capture)
        package.setLevel(level)
    return bad


def test_a_missing_private_path_warns_once_with_fixed_text_and_reads_still_work() -> None:
    found = s_warn(http_module)
    assert found == []


# --- (e)(f) cancelled jobs and error mapping, both getters --------------------------------------


class Body:
    """A response whose first read1 cancels the job; the next would finish a valid body."""

    def __init__(self, job: Any) -> None:
        self.job, self.calls = job, 0

    def __enter__(self) -> Body:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read1(self, n: int = -1) -> bytes:
        self.calls += 1
        if self.calls == 1:
            self.job.abort()
            return b"{"
        return b"}" if self.calls == 2 else b""


def call_with_job(mod: Any, kind: str, job: Any, opener: Opener) -> Exception | Any:
    getter = make(mod, kind, 5.0, 30.0)
    fn = getter._get if kind == "get" else getter._post
    with pytest.MonkeyPatch.context() as m:
        install(m, opener)
        mod._local.job = job
        try:
            return fn(URL[kind], PARAMS)
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            mod._local.job = None


def raiser(job: Any, exc: Exception, cancel: bool) -> Callable[[Any, float], Any]:
    def behave(request: Any, timeout: float) -> Any:
        if cancel:
            job.abort()
        raise exc

    return behave


def errors(kind: str) -> list[tuple[str, Callable[[], Exception], str]]:
    def http_error() -> Exception:
        return urllib.error.HTTPError(
            URL[kind], 500, "m", http.client.HTTPMessage(), io.BytesIO(b"")
        )

    failed_text = f"request to {LABEL[kind]} failed"
    return [
        ("OSError", lambda: OSError("boom"), failed_text),
        ("URLError", lambda: urllib.error.URLError("x"), failed_text),
        ("HTTPError", http_error, f"HTTP 500 from {LABEL[kind]}"),
        ("HTTPException", lambda: http.client.HTTPException("secret header"), failed_text),
        ("ValueError", lambda: ValueError("secret"), failed_text),
        ("TimeoutError", lambda: TimeoutError("t"), failed_text),
    ]


def s_cancel(mod: Any, kind: str) -> list[str]:
    bad: list[str] = []
    label = LABEL[kind]
    for name, job in (("cancelled", None), ("expired", None)):
        job = mod._Job(label, 30 if name == "cancelled" else 0.0)
        if name == "cancelled":
            job.abort()
        opener = Opener(lambda r, t: (_ for _ in ()).throw(AssertionError("connected")))
        got = call_with_job(mod, kind, job, opener)
        if not is_text(got, timed_out(kind)):
            bad.append(f"{name} job before connecting: {got!r}")
        if opener.timeouts:
            bad.append(f"{name} job still connected")
    job = mod._Job(label, 30)
    body = Body(job)
    got = call_with_job(mod, kind, job, Opener(lambda r, t: body))
    if not is_text(got, timed_out(kind)):
        bad.append(f"cancel during the read loop: {got!r}")
    if body.calls != 1:
        bad.append(f"the read loop went on after cancel: {body.calls} receives")
    for name, make_exc, normal in errors(kind):
        job = mod._Job(label, 30)
        got = call_with_job(mod, kind, job, Opener(raiser(job, make_exc(), cancel=True)))
        if not is_text(got, timed_out(kind)):
            bad.append(f"{name} after cancel: {got!r}")
        job = mod._Job(label, 30)
        got = call_with_job(mod, kind, job, Opener(raiser(job, make_exc(), cancel=False)))
        if not (isinstance(got, ConnectorTransportError) and str(got) == normal):
            bad.append(f"{name} before cancel: {got!r}")
        if "secret" in str(got):
            bad.append(f"{name}: the error text echoes the exception")
    job = mod._Job(label, 0.1)

    def slow(request: Any, timeout: float) -> Any:
        time.sleep(0.15)
        raise OSError("late")

    got = call_with_job(mod, kind, job, Opener(slow))
    if not is_text(got, timed_out(kind)):
        bad.append(f"an error after the deadline: {got!r}")
    return bad


@pytest.mark.parametrize("kind", KINDS)
def test_a_cancelled_job_neither_connects_nor_reads_and_maps_late_errors(kind: str) -> None:
    found = s_cancel(http_module, kind)
    assert found == []


# --- mutations on copies ------------------------------------------------------------------------

LOOP_GIVE_UP = "            if remaining <= 0 or (job is not None and job.cancelled.is_set()):\n"
READ_SOME = 'read_some = getattr(response, "read1", None) or response.read'
ABORT_CLOSE = "            if sock is not None:\n                _close_socket(sock)\n"
LATE_CLOSE = "        _close_socket(sock)\n\n    def detach"
TAKE = "        with self._lock:\n            sock, self._sock = self._sock, None\n" + ABORT_CLOSE
SET_FIRST = "        self.cancelled.set()\n" + TAKE
SET_LAST = TAKE + "        self.cancelled.set()\n"
TIGHTEN = "remaining if current is None else min(current, remaining)"
FLOOR = "sock.settimeout(max(" + TIGHTEN + ", 0.001))"
SHUT = "        sock.shutdown(socket.SHUT_RDWR)\n"
WARN_ONCE = "        if not _socket_warned:\n"
STORE = "                self._sock = sock\n                return\n"
SUPPRESS = "contextlib.suppress(OSError, ValueError)"
PRE_CONNECT = "    if gave_up():\n        raise _timed_out(_label(url))\n"

MUTANTS = [
    pytest.param([(READ_SOME, "read_some = response.read")], False, id="read1-replaced-by-read"),
    pytest.param([(READ_SOME, "read_some = response.read1")], False, id="no-fallback-to-read"),
    pytest.param([("min(_READ_CHUNK, max_bytes + 1 - size)", "_READ_CHUNK")], False, id="cap-lost"),
    pytest.param(
        [("min(_READ_CHUNK, max_bytes + 1 - size)", "max_bytes + 1")], False, id="chunk-cap-lost"
    ),
    pytest.param([(ABORT_CLOSE, "        pass\n")], False, id="abort-does-not-close"),
    pytest.param([(LATE_CLOSE, "        return\n\n    def detach")], False, id="late-attach-keeps"),
    pytest.param([(STORE, "                return\n")], False, id="attach-never-stores"),
    pytest.param([(SET_FIRST, SET_LAST)], False, id="cancel-flag-set-after-the-lock"),
    pytest.param([(SHUT, "        pass\n")], False, id="shutdown-removed"),
    pytest.param(
        [(SUPPRESS, "contextlib.suppress(OSError)")], True, id="valueerror-not-suppressed"
    ),
    pytest.param([(SUPPRESS, "contextlib.suppress(Exception)")], True, id="every-error-suppressed"),
    pytest.param(
        [("        if gave_up():\n", "        if False:\n")], True, id="handlers-ignore-cancel"
    ),
    pytest.param(
        [("        if gave_up():\n", "        if True:\n")], True, id="handlers-always-timeout"
    ),
    pytest.param(
        [(PRE_CONNECT, "    if False:\n        raise _timed_out(_label(url))\n")],
        False,
        id="connects-after-cancel",
    ),
    pytest.param(
        [(LOOP_GIVE_UP, "            if remaining <= 0:\n")],
        False,
        id="read-loop-ignores-cancel",
    ),
    pytest.param(
        [(TIGHTEN, "remaining if current is None else max(current, remaining)")],
        False,
        id="tighten-raises-the-timeout",
    ),
    pytest.param([(FLOOR, "sock.settimeout(" + TIGHTEN + ")")], False, id="tighten-floor-removed"),
    pytest.param([(WARN_ONCE, "        if True:\n")], False, id="warning-logged-every-time"),
    pytest.param(
        [
            (
                "connector HTTP socket not reachable",
                "connector " + "HTTP socket not reachable at " + "{url}",
            )
        ],
        False,
        id="warning-text-changed",
    ),
    pytest.param(
        [('extra={"http_socket_access": "unavailable"}', "extra={}")],
        False,
        id="warning-extra-lost",
    ),
]


def mutant_plan() -> list[tuple[str, Callable[[Any], list[str]]]]:
    plan: list[tuple[str, Callable[[Any], list[str]]]] = [
        ("read1", s_read1),
        ("job", s_job),
        ("warn", s_warn),
        ("path", s_real_path),
        ("body", s_real_body),
    ]
    for kind in KINDS:
        plan.append((f"cancel/{kind}", lambda mod, k=kind: s_cancel(mod, k)))
    plan.append(("real/get", lambda mod: s_real_cancel(mod, "get")))
    plan.append(("real/post", lambda mod: s_real_cancel(mod, "post", "timeout")))
    return plan


@pytest.mark.parametrize("edits,many", MUTANTS)
def test_every_mutation_of_the_socket_and_cancel_code_is_caught(
    edits: list[tuple[str, str]], many: bool
) -> None:
    mutant = mutated_module(http_module, *edits, many=many)
    misses: list[str] = []
    crashes: list[str] = []
    try:
        for name, scenario in mutant_plan():
            try:
                misses += scenario(mutant)
            except Exception as exc:  # noqa: BLE001
                crashes.append(f"{name}: {type(exc).__name__}")
            if misses:
                break
    finally:
        with contextlib.suppress(Exception):
            mutant._POOL.shutdown(wait=False, cancel_futures=True)
    assert misses != [], f"survived (crashes: {crashes})"
