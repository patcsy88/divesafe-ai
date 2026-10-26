"""Total deadline, bounded worker pool and cancellation of `divesafe.data.http` (Code Review
finding 4), for BOTH getters (GET `UrllibJsonGetter`, POST `UrllibFormPostJsonGetter`).

Offline: `urllib.request.build_opener` is patched with a fake opener and responses are fake
objects; no socket is opened. Each scenario takes a module (the real one or a mutated COPY made
with `mutated_module`) and returns the list of things it got wrong, so the same checks serve the
behaviour tests and the mutation tests. A mutant that crashes is not a refusal: the mutation tests
need at least one non-crash miss. A mutant's own `_POOL` is shut down after each run.

EQUIVALENT MUTANTS (not tested):
  - `min(timeout, remaining)` as `min(remaining, timeout)`; `>=` as `>` in the deadline compares
    (a monotonic reading equal to the deadline is not a state a test can reach);
  - removing `_local.job = None` from `_run_job`'s `finally`: every pooled call sets its own job
    first, and `_current_deadline` is only read inside a job or a direct call;
  - changing `_READ_CHUNK`, or `max(remaining, 0.001)`'s floor (only the upper bound is observable);
  - changing `HTTP_POOL_WORKERS` itself: the tests tie the pool to the constant, not to 8.

NOTE: `_tighten_socket_timeout` reaches `response.fp.raw._sock.settimeout`, a private path of
http.client. Here it runs against a fake that mirrors the path; that a REAL HTTPResponse still has
the path is proven offline in `test_http_socket_cancel.py` (socketpair). Changed mutation targets
after the M1 restructure: `gave_up()` in `_open_json`, the `read_limited` loop's give-up line,
`_Job.abort`'s `self.cancelled.set()` and `job.abort()` in `_call_in_pool`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import threading
import time
import types
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.data import http as http_module
from divesafe.data.errors import ConnectorResponseError, ConnectorTransportError

pytestmark = pytest.mark.safety

KINDS = ("get", "post")
URL = {
    "get": "https://api.data.gov.my/weather/warning",
    "post": "https://etide.hydro.gov.my/api/tide",
}
LABEL = {"get": "api.data.gov.my/weather/warning", "post": "etide.hydro.gov.my/api/tide"}
SECRET = "TOPSECRETQUERY"
PARAMS = {"q": SECRET}


def timed_out(kind: str) -> str:
    return f"request to {LABEL[kind]} timed out"


def failed(kind: str) -> str:
    return f"request to {LABEL[kind]} failed"


def leaks(exc: BaseException) -> bool:
    text = str(exc)
    return any(token in text for token in (SECRET, "?", "q="))


class Resp:
    """A fake response; `exited` is set when the `with` block around it is left."""

    def __init__(self, reader: Callable[[int], bytes]) -> None:
        self._reader = reader
        self.exited = threading.Event()
        self.reads: list[int] = []

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited.set()

    def read(self, n: int = -1) -> bytes:
        self.reads.append(n)
        return self._reader(n)


class Opener:
    def __init__(self, behave: Callable[[Any, float], Any]) -> None:
        self.behave = behave
        self.timeouts: list[float] = []
        self.threads: list[str] = []

    def open(self, request: Any, timeout: float) -> Any:
        self.timeouts.append(timeout)
        self.threads.append(threading.current_thread().name)
        return self.behave(request, timeout)


def install(m: pytest.MonkeyPatch, opener: Opener) -> list[int]:
    built: list[int] = []

    def build(*args: object, **kwargs: object) -> Opener:
        built.append(1)
        return opener

    m.setattr(urllib.request, "build_opener", build)
    return built


def json_once() -> Callable[[int], bytes]:
    chunks = [b"{}", b""]
    return lambda n: chunks.pop(0) if chunks else b""


def make(mod: Any, kind: str, timeout: float, deadline: float, max_bytes: int = 1000) -> Any:
    cls = mod.UrllibJsonGetter if kind == "get" else mod.UrllibFormPostJsonGetter
    return cls(timeout_seconds=timeout, deadline_seconds=deadline, max_bytes=max_bytes)


def run(getter: Any, kind: str) -> tuple[Exception | None, Any]:
    try:
        return None, asyncio.run(getter.get_json(URL[kind], PARAMS))
    except Exception as exc:  # noqa: BLE001
        return exc, None


def is_text(exc: Exception | None, text: str) -> bool:
    return isinstance(exc, ConnectorTransportError) and str(exc) == text


def block_pool(
    mod: Any, count: int
) -> tuple[threading.Event, list[threading.Event], list[str], list[Any]]:
    release = threading.Event()
    started = [threading.Event() for _ in range(count)]
    names: list[str] = []

    def hold(i: int) -> None:
        names.append(threading.current_thread().name)
        started[i].set()
        release.wait(10)

    return release, started, names, [mod._POOL.submit(hold, i) for i in range(count)]


# --- scenarios: (mod, kind) -> list of misses ----------------------------------------------------


def s_drip(mod: Any, kind: str) -> list[str]:
    """(a)(b)(g): a response that drips bytes hits the total deadline; the worker ends."""
    bad: list[str] = []
    stop = threading.Event()

    def reader(n: int) -> bytes:
        return b"" if stop.wait(0.02) else b"x" * 10

    resp = Resp(reader)
    opener = Opener(lambda r, t: resp)
    with pytest.MonkeyPatch.context() as m:
        install(m, opener)
        getter = make(mod, kind, 5.0, 0.3, max_bytes=10_000_000)
        try:
            t0 = time.perf_counter()
            exc, _ = run(getter, kind)
            elapsed = time.perf_counter() - t0
            ended = resp.exited.wait(0.6)
        finally:
            stop.set()
    if not is_text(exc, timed_out(kind)):
        bad.append(f"not the fixed timeout error: {exc!r}")
    if exc is not None and leaks(exc):
        bad.append("the error text holds the query or payload")
    if elapsed > 1.5:
        bad.append(f"the call took {elapsed:.2f}s")
    if sum(resp.reads) * 10 >= 10_000_000:
        bad.append("the whole body was read")
    if not ended:
        bad.append("the worker was still reading after the caller gave up")
    if not opener.threads or not all(t.startswith("divesafe-http") for t in opener.threads):
        bad.append(f"the work did not run on the dedicated pool: {opener.threads}")
    return bad


def s_text(mod: Any, kind: str) -> list[str]:
    """(b)(e, early half): ordinary failures keep their own text and never echo query/payload."""
    bad: list[str] = []
    cases: list[tuple[str, Callable[[Any, float], Any], str]] = [
        ("URLError", _raise(urllib.error.URLError(f"x {SECRET}")), failed(kind)),
        ("OSError", _raise(OSError(f"x {SECRET}")), failed(kind)),
        (
            "HTTPError",
            _raise(urllib.error.HTTPError(f"{URL[kind]}?q={SECRET}", 503, "m", {}, None)),  # type: ignore[arg-type]
            f"HTTP 503 from {LABEL[kind]}",
        ),
    ]
    for name, behave, wanted in cases:
        with pytest.MonkeyPatch.context() as m:
            install(m, Opener(behave))
            exc, _ = run(make(mod, kind, 5.0, 5.0), kind)
        if not is_text(exc, wanted):
            bad.append(f"{name}: {exc!r}")
        if exc is not None and leaks(exc):
            bad.append(f"{name}: query or payload in the text")
    body = Resp(lambda n: b"not json " + SECRET.encode())
    with pytest.MonkeyPatch.context() as m:
        install(m, Opener(lambda r, t: body))
        exc, _ = run(make(mod, kind, 5.0, 5.0), kind)
    if not isinstance(exc, ConnectorResponseError) or leaks(exc):
        bad.append(f"bad body: {exc!r}")
    return bad


def _raise(error: BaseException) -> Callable[[Any, float], Any]:
    def behave(request: Any, timeout: float) -> Any:
        raise error

    return behave


def s_timeout_arg(mod: Any, kind: str) -> list[str]:
    """(d): the socket timeout is <= what is left and <= the configured timeout."""
    bad: list[str] = []
    for timeout, deadline in ((5.0, 0.5), (0.2, 5.0)):
        opener = Opener(lambda r, t: Resp(json_once()))
        with pytest.MonkeyPatch.context() as m:
            install(m, opener)
            exc, value = run(make(mod, kind, timeout, deadline), kind)
        if exc is not None or value != {}:
            bad.append(f"timeout={timeout}: the call failed: {exc!r}")
            continue
        (given,) = opener.timeouts
        if not 0 < given <= min(timeout, deadline):
            bad.append(f"timeout={timeout} deadline={deadline}: open got {given}")
    return bad


def s_late(mod: Any, kind: str) -> list[str]:
    """(c)(e): an expired deadline opens nothing; a late error is a timeout, an early one not."""
    bad: list[str] = []
    url = URL[kind]
    request = urllib.request.Request(url, data=b"a=1" if kind == "post" else None)

    def attempt(seconds: float, delay: float, error: BaseException) -> tuple[str, Opener]:
        def behave(r: Any, t: float) -> Any:
            time.sleep(delay)
            raise error

        opener = Opener(behave)
        mod._local.job = mod._Job(LABEL[kind], seconds)
        try:
            mod._open_json(opener, request, url, 5.0, 1000)
        except ConnectorTransportError as exc:
            return str(exc), opener
        finally:
            mod._local.job = None
        return "no error", opener

    for error in (urllib.error.URLError("x"), OSError("x"), TimeoutError()):
        late, _ = attempt(0.05, 0.1, error)
        if late != timed_out(kind):
            bad.append(f"{type(error).__name__}: a late error was reported as {late!r}")
        early, _ = attempt(5.0, 0.0, error)
        if early != failed(kind):
            bad.append(f"{type(error).__name__}: an early error was reported as {early!r}")
    text, opener = attempt(-1.0, 0.0, OSError("x"))
    if text != timed_out(kind) or opener.timeouts:
        bad.append(f"an expired deadline still opened a connection: {text!r}")
    return bad


def saturated(mod: Any, kind: str, deadline: float, cancel: bool) -> list[str]:
    bad: list[str] = []
    jobs: list[Any] = []
    opener = Opener(lambda r, t: Resp(lambda n: b""))

    async def drive(getter: Any) -> tuple[Exception | None, float, int, bool]:
        t0, exc, was_cancelled = time.perf_counter(), None, False
        if cancel:
            task = asyncio.ensure_future(getter.get_json(URL[kind], PARAMS))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                was_cancelled = True
            except Exception as e:  # noqa: BLE001
                exc = e
        else:
            try:
                await getter.get_json(URL[kind], PARAMS)
            except Exception as e:  # noqa: BLE001
                exc = e
        elapsed = time.perf_counter() - t0
        # other work on asyncio's DEFAULT executor must not be starved meanwhile
        return (
            exc,
            elapsed,
            await asyncio.wait_for(asyncio.to_thread(lambda: 7), 1.0),
            was_cancelled,
        )

    with pytest.MonkeyPatch.context() as m:
        built = install(m, opener)
        base = mod._Job

        class Rec(base):  # type: ignore[valid-type, misc]
            def __init__(self, *args: Any) -> None:
                super().__init__(*args)
                jobs.append(self)

        m.setattr(mod, "_Job", Rec)
        release, started, _, futures = block_pool(mod, mod.HTTP_POOL_WORKERS)
        try:
            if not all(e.wait(2) for e in started):
                return ["the pool could not be filled"]
            exc, elapsed, side, was_cancelled = asyncio.run(
                asyncio.wait_for(drive(make(mod, kind, 5.0, deadline)), 4)
            )
        except TimeoutError:
            return ["the call or the default executor did not answer"]
        finally:
            release.set()
        try:
            for future in futures:
                future.result(2)
            mod._POOL.submit(lambda: None).result(2)  # FIFO: the queued job has been dealt with
        except Exception:  # noqa: BLE001
            bad.append("the pool did not drain")
    time.sleep(0.05)
    if built or opener.timeouts:
        bad.append("a connection was opened although the caller had given up")
    if side != 7:
        bad.append("the default executor was starved")
    if len(jobs) != 1 or not jobs[0].cancelled.is_set():
        bad.append("the cancelled flag was not set when the caller left")
    if cancel:
        if not was_cancelled:
            bad.append("the caller was not cancelled")
    else:
        if not is_text(exc, timed_out(kind)):
            bad.append(f"not the fixed timeout error: {exc!r}")
        elif leaks(exc):
            bad.append("query or payload in the text")
        if not 0.25 <= elapsed < 1.5:
            bad.append(f"timed out after {elapsed:.2f}s, not at the 0.3s deadline")
    return bad


def s_saturated(mod: Any, kind: str) -> list[str]:
    """(f)"""
    return saturated(mod, kind, 0.3, cancel=False)


def s_cancel(mod: Any, kind: str) -> list[str]:
    """(h)"""
    return saturated(mod, kind, 30.0, cancel=True)


def s_run_job(mod: Any, kind: str) -> list[str]:
    """Queued work that was cancelled or is past its deadline never starts (direct calls)."""
    bad: list[str] = []
    calls: list[Any] = []

    def fn(url: str, params: Any) -> str:
        calls.append(getattr(mod._local, "job", None))
        return "ran"

    for name, seconds, cancel in (("cancelled", 30.0, True), ("expired", -1.0, False)):
        job = mod._Job("h/p", seconds)
        if cancel:
            job.cancelled.set()
        try:
            mod._run_job(job, fn, "u", {})
            bad.append(f"{name}: the work ran")
        except ConnectorTransportError as exc:
            if str(exc) != "request to h/p timed out":
                bad.append(f"{name}: {exc}")
    if calls:
        bad.append("work started although it should not have")
    live = mod._Job("h/p", 30.0)
    result = mod._run_job(live, fn, "u", {})
    if result != "ran" or calls != [live] or getattr(mod._local, "job", None) is not None:
        bad.append("a live job did not run with its job set and cleared")
    return bad


class Sock:
    def __init__(self, fail: bool = False) -> None:
        self.values: list[float] = []
        self.fail = fail

    def settimeout(self, value: float) -> None:
        self.values.append(value)
        if self.fail:
            raise OSError("closed")


class SockResp(io.BytesIO):
    def __init__(self, data: bytes, sock: Sock) -> None:
        super().__init__(data)
        self.fp = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=sock))


def s_read(mod: Any, kind: str) -> list[str]:
    """(j) the size cap with and without a deadline, the expired deadline, the socket tightening."""
    bad: list[str] = []
    read_limited = mod.read_limited
    for data, wanted in ((b"x" * 100, b"x" * 100), (b"x" * 101, None)):
        for deadline in (None, time.monotonic() + 30):
            try:
                body = read_limited(io.BytesIO(data), 100, deadline)
            except ConnectorResponseError:
                body = None
            if body != wanted:
                bad.append(f"cap with deadline={deadline is not None}: {len(data)} bytes")
    once = Resp(lambda n: b"ab")
    read_limited(once, 100)
    if once.reads != [101]:
        bad.append(f"deadline_at=None did not read once: {once.reads}")
    big = io.BytesIO(b"y" * 40_000)
    counted = Resp(big.read)
    if read_limited(counted, 100_000, time.monotonic() + 30) != b"y" * 40_000:
        bad.append("a body under the cap was changed")
    if max(counted.reads) > mod._READ_CHUNK:
        bad.append("a read was larger than one chunk")
    never = Resp(lambda n: b"z")
    try:
        read_limited(never, 100, time.monotonic() - 1)
        bad.append("an expired deadline did not stop the read")
    except mod._DeadlineExceeded:
        pass
    except ConnectorResponseError:
        bad.append("an expired deadline was ignored: the read ran on to the size cap")
    if never.reads:
        bad.append("a read was made after the deadline")
    sock = Sock()
    read_limited(SockResp(b"{}", sock), 100, time.monotonic() + 10)
    if not sock.values or not all(0 < v <= 10 for v in sock.values):
        bad.append(f"the socket timeout was not tightened to the remaining time: {sock.values}")
    if read_limited(SockResp(b"{}", Sock(fail=True)), 100, time.monotonic() + 10) != b"{}":
        bad.append("a failing settimeout broke the read")
    if read_limited(io.BytesIO(b"{}"), 100, time.monotonic() + 10) != b"{}":
        bad.append("a response without a socket path broke the read")
    return bad


def s_pool(mod: Any, kind: str) -> list[str]:
    """(g) the pool threads are named divesafe-http and there are HTTP_POOL_WORKERS of them."""
    bad: list[str] = []
    workers = mod.HTTP_POOL_WORKERS
    release, started, names, _ = block_pool(mod, workers + 1)
    try:
        if not all(e.wait(2) for e in started[:workers]):
            return ["the workers did not start"]
        extra_started = started[workers].wait(0.15)
        seen = sorted(set(names))
    finally:
        release.set()
    if workers < 1 or mod._POOL._max_workers != workers:
        bad.append("the pool size is not the constant")
    if extra_started:
        bad.append("more threads than HTTP_POOL_WORKERS ran at once")
    if len(seen) != workers or not all(n.startswith("divesafe-http") for n in seen):
        bad.append(f"worker threads: {seen}")
    return bad


PER_KIND: list[tuple[str, Callable[[Any, str], list[str]]]] = [
    ("drip", s_drip),
    ("text", s_text),
    ("timeout_arg", s_timeout_arg),
    ("late", s_late),
    ("saturated", s_saturated),
    ("cancel", s_cancel),
]
SHARED: list[tuple[str, Callable[[Any, str], list[str]]]] = [
    ("run_job", s_run_job),
    ("read", s_read),
    ("pool", s_pool),
]


# --- the real module ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name,scenario", PER_KIND, ids=[n for n, _ in PER_KIND])
def test_the_real_getter_meets_every_scenario(
    kind: str, name: str, scenario: Callable[[Any, str], list[str]]
) -> None:
    found = scenario(http_module, kind)
    assert found == []


@pytest.mark.parametrize("name,scenario", SHARED, ids=[n for n, _ in SHARED])
def test_the_real_module_meets_every_shared_scenario(
    name: str, scenario: Callable[[Any, str], list[str]]
) -> None:
    found = scenario(http_module, "get")
    assert found == []


def test_the_pool_is_the_dedicated_module_pool() -> None:
    assert http_module._POOL._thread_name_prefix == "divesafe-http"
    assert http_module._POOL._max_workers == http_module.HTTP_POOL_WORKERS


# --- mutations on copies -----------------------------------------------------------------------

READ_GIVE_UP = (
    "            if remaining <= 0 or (job is not None and job.cancelled.is_set()):\n"
    "                raise _DeadlineExceeded\n"
)
OPEN_TIMEOUT = "timeout=min(timeout, deadline_at - time.monotonic())"
EDITS = [
    pytest.param(
        [
            (
                "    if gave_up():\n        raise _timed_out(_label(url))\n",
                "    if False:\n        raise _timed_out(_label(url))\n",
            )
        ],
        id="deadline-check-removed",
    ),
    pytest.param(
        [
            (
                READ_GIVE_UP,
                READ_GIVE_UP.replace("remaining <= 0 or (", "("),
            )
        ],
        id="deadline-ignored-in-read-loop",
    ),
    pytest.param(
        [("read_limited(response, max_bytes, deadline_at)", "read_limited(response, max_bytes)")],
        id="deadline-not-passed-to-read",
    ),
    pytest.param([(OPEN_TIMEOUT, "timeout=timeout")], id="remaining-not-applied"),
    pytest.param(
        [("            _tighten_socket_timeout(response, remaining)\n", "            pass\n")],
        id="socket-tightening-removed",
    ),
    pytest.param(
        [
            (
                "job.cancelled.is_set() or time.monotonic() >= job.deadline_at",
                "time.monotonic() >= job.deadline_at",
            )
        ],
        id="cancelled-flag-not-checked",
    ),
    pytest.param(
        [
            (
                "job.cancelled.is_set() or time.monotonic() >= job.deadline_at",
                "job.cancelled.is_set()",
            )
        ],
        id="deadline-not-checked-before-start",
    ),
    pytest.param(
        [
            (
                "    if job.cancelled.is_set() or time.monotonic() >= job.deadline_at:\n",
                "    if False:\n",
            )
        ],
        id="not-started-check-removed",
    ),
    pytest.param(
        [("        self.cancelled.set()\n", "        pass\n")], id="cancelled-flag-never-set"
    ),
    pytest.param([("        job.abort()\n", "        pass\n")], id="abort-never-called"),
    pytest.param([("run_in_executor(_POOL,", "run_in_executor(None,")], id="default-pool-used"),
    pytest.param(
        [("_timed_out(_label(url))", "_timed_out(_label(url) + '?' + request.full_url)")],
        id="timeout-text-includes-the-query",
    ),
    pytest.param(
        [("{_label(url)} failed", "{request.full_url} {request.data} failed")],
        id="failure-text-includes-query-and-payload",
    ),
    pytest.param(
        [
            (
                "        if gave_up():\n",
                "        if False:\n",
            )
        ],
        id="late-errors-not-mapped",
    ),
    pytest.param(
        [("        if gave_up():\n", "        if True:\n")],
        id="early-errors-mapped-as-timeouts",
    ),
    pytest.param(
        [('thread_name_prefix="divesafe-http"', 'thread_name_prefix="x"')], id="pool-thread-name"
    ),
    pytest.param(
        [("max_workers=HTTP_POOL_WORKERS", "max_workers=HTTP_POOL_WORKERS + 1")],
        id="pool-bigger-than-the-constant",
    ),
]


@pytest.mark.parametrize("edits", EDITS)
def test_every_mutation_of_the_deadline_and_pool_is_caught(edits: list[tuple[str, str]]) -> None:
    mutant = mutated_module(http_module, *edits, many=True)
    misses: list[str] = []
    crashes: list[str] = []
    plan = [(n, s, k) for k in KINDS for n, s in PER_KIND] + [(n, s, "get") for n, s in SHARED]
    try:
        for name, scenario, kind in plan:
            try:
                misses += scenario(mutant, kind)
            except Exception as exc:  # noqa: BLE001
                crashes.append(f"{name}/{kind}: {type(exc).__name__}")
            if misses:
                break  # caught; no need to run the rest
    finally:
        with contextlib.suppress(Exception):
            mutant._POOL.shutdown(wait=False, cancel_futures=True)
    assert misses != [], f"survived (crashes: {crashes})"
