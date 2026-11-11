"""Truncated bodies and `_Job.detach` of `divesafe.data.http`, for BOTH getters (GET and POST).

Offline: no network, no database. Fakes cover the logic; a REAL `http.client.HTTPResponse` on a
`socket.socketpair()` covers the Content-Length bookkeeping (`response.length`) that the check
relies on. Every socketpair end is closed in `finally`. Each scenario takes a module (the real one
or a mutated COPY made with `mutated_module`) and returns what it got wrong. A crashing mutant is
not a refusal: the mutation tests need one non-crash miss.

EQUIVALENT MUTANTS (not tested):
  - `owed > 0` as `owed >= 1` (ints);
  - the order of the `isinstance(owed, int)` and `not isinstance(owed, bool)` tests;
  - `if job is not None:` around `job.detach()` removed (a getter call always has a job in the
    pool; with no job there is nothing to detach and the code path is the direct-call one).

GAPS:
  - A server that sends MORE than Content-Length is not a truncation; http.client stops at the
    length, so the extra bytes are never read. Not tested here.
  - Chunked bodies cut mid-stream raise `IncompleteRead` inside http.client (an HTTPException,
    reported as an ordinary failure); only the fixed-length case is the new check.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import socket
import threading
import time
import urllib.error
from collections.abc import Callable
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_http_deadline_pool import KINDS, LABEL, URL, Opener, is_text, make, timed_out
from tests.safety.test_http_socket_cancel import CLOSED, FakeSock, Wrapped, call_with_job

from divesafe.data import http as http_module
from divesafe.data.errors import ConnectorResponseError, ConnectorTransportError

pytestmark = pytest.mark.safety

TRUNCATED = "response was truncated"
SECRET = "https://secret.example/x?token=SEKRET"
ABSENT = object()


def attempt(bad: list[str], label: str, call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{label}: raised {type(exc).__name__}: {exc}")
        return None


def later() -> float:
    return time.monotonic() + 30


class Fake:
    """A response with read1 and read; `length` is set only when given."""

    def __init__(self, chunks: list[bytes], length: Any = ABSENT, sock: Any = None) -> None:
        self.chunks = list(chunks)
        self.url = SECRET
        if length is not ABSENT:
            self.length = length
        if sock is not None:
            self.fp = type("Fp", (), {"raw": type("Raw", (), {"_sock": sock})()})()
        self.hook: Callable[[Fake], None] | None = None

    def __enter__(self) -> Fake:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read1(self, n: int = -1) -> bytes:
        if self.hook is not None:
            self.hook(self)
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, n: int = -1) -> bytes:
        out = b"".join(self.chunks)
        self.chunks = []
        return out


# --- 1. truncation ------------------------------------------------------------------------------

LENGTHS: list[tuple[Any, bool]] = [
    (5, True),
    (1, True),
    (0, False),
    (None, False),
    (ABSENT, False),
    ("5", False),
    (5.0, False),
    (True, False),
    (-3, False),
]


def s_truncation(mod: Any) -> list[str]:
    bad: list[str] = []
    mod._local.job = None
    for length, truncated in LENGTHS:
        for deadline in (later(), None):
            where = f"length={length!r} deadline={deadline is not None}"
            try:
                got = mod.read_limited(Fake([b"12"], length), 100, deadline)
                if truncated:
                    bad.append(f"{where}: a short body was returned")
                elif got != b"12":
                    bad.append(f"{where}: body changed: {got!r}")
            except ConnectorResponseError as exc:
                if not truncated:
                    bad.append(f"{where}: refused without a positive int length: {exc}")
                elif str(exc) != TRUNCATED:
                    bad.append(f"{where}: error text {str(exc)!r}")
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{where}: raised {type(exc).__name__}")
    for deadline in (later(), None):
        try:
            mod.read_limited(Fake([b"x" * 11], 5), 10, deadline)
            bad.append("an over-cap body was returned")
        except ConnectorResponseError as exc:
            if "exceeds" not in str(exc):
                bad.append(f"the size cap does not come first: {exc}")
    try:
        mod.read_limited(Fake([b"12"], 5), 100, time.monotonic() - 1)
        bad.append("an expired deadline was not reported")
    except mod._DeadlineExceeded:
        pass
    except ConnectorResponseError:
        bad.append("truncation took priority over an expired deadline")
    job = mod._Job("l", 30)
    job.abort()
    mod._local.job = job
    try:
        mod.read_limited(Fake([b"12"], 5), 100, later())
        bad.append("a cancelled job was not reported")
    except mod._DeadlineExceeded:
        pass
    except ConnectorResponseError:
        bad.append("truncation took priority over a cancel")
    finally:
        mod._local.job = None
    return bad


def test_a_stream_ending_with_content_length_owed_is_truncated_on_both_paths() -> None:
    found = s_truncation(http_module)
    assert found == []


def s_truncation_open(mod: Any, kind: str) -> list[str]:
    bad: list[str] = []
    label = LABEL[kind]
    cases: list[tuple[str, list[bytes], Any, Any]] = [
        ("cut short", [b"{"], 5, TRUNCATED),
        ("a number cut short", [b"12"], 3, TRUNCATED),  # '12' of '12345' would parse as 12
        ("complete", [b'{"a": 1}'], 0, {"a": 1}),
        ("chunked or unknown", [b'{"a": 1}'], None, {"a": 1}),
    ]
    for name, chunks, length, wanted in cases:
        job = mod._Job(label, 30)
        got = call_with_job(mod, kind, job, Opener(lambda r, t, c=chunks, n=length: Fake(c, n)))
        if wanted == TRUNCATED:
            ok = isinstance(got, ConnectorResponseError) and str(got) == TRUNCATED
            if not ok:
                bad.append(f"{name}: {got!r}")
            elif "secret" in str(got) or "SEKRET" in str(got) or label in str(got):
                bad.append(f"{name}: the error text carries the URL")
        elif got != wanted:
            bad.append(f"{name}: {got!r}")
    job = mod._Job(label, 30)
    fake = Fake([b"{"], 5)
    fake.hook = lambda f: job.abort()
    got = call_with_job(mod, kind, job, Opener(lambda r, t: fake))
    if not is_text(got, timed_out(kind)):
        bad.append(f"a cancel while truncated: {got!r}")
    return bad


@pytest.mark.parametrize("kind", KINDS)
def test_open_json_refuses_a_truncated_body_and_keeps_the_other_errors(kind: str) -> None:
    found = s_truncation_open(http_module, kind)
    assert found == []


def s_real_truncation(mod: Any, kind: str) -> list[str]:
    """A real HTTPResponse: Content-Length 100, 10 bytes sent, then the peer closes."""
    bad: list[str] = []
    cases = [
        ("truncated", b"Content-Length: 100", b'{"a": 1,  ', TRUNCATED),
        ("number cut", b"Content-Length: 5", b"12", TRUNCATED),
        ("complete", b"Content-Length: 8", b'{"a": 1}', {"a": 1}),
    ]
    for name, header, body, wanted in cases:
        client, peer = socket.socketpair()
        try:
            peer.sendall(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n\r\n" + body)
            peer.close()
            response = http.client.HTTPResponse(client)
            response.begin()
            entered, exited = threading.Event(), threading.Event()
            opener = Opener(
                lambda r, t, resp=response, en=entered, ex=exited: Wrapped(resp, en, ex)
            )
            getter = make(mod, kind, 5.0, 10.0, max_bytes=1000)
            with pytest.MonkeyPatch.context() as m:
                import urllib.request

                m.setattr(urllib.request, "build_opener", lambda *a, op=opener, **k: op)
                try:
                    got: Any = asyncio.run(getter.get_json(URL[kind], {}))
                except Exception as exc:  # noqa: BLE001
                    got = exc
            if wanted == TRUNCATED:
                if not (isinstance(got, ConnectorResponseError) and str(got) == TRUNCATED):
                    bad.append(f"{name}: {got!r}")
            elif got != wanted:
                bad.append(f"{name}: {got!r}")
        finally:
            for end in (client, peer):
                with contextlib.suppress(OSError):
                    end.close()
    # the no-deadline path on a real response
    client, peer = socket.socketpair()
    try:
        peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 10)
        peer.close()
        response = http.client.HTTPResponse(client)
        response.begin()
        try:
            mod.read_limited(response, 1000)
            bad.append("no-deadline path: a truncated real body was returned")
        except ConnectorResponseError as exc:
            if str(exc) != TRUNCATED:
                bad.append(f"no-deadline path: {exc}")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"no-deadline path: {type(exc).__name__}")
    finally:
        for end in (client, peer):
            with contextlib.suppress(OSError):
                end.close()
    return bad


@pytest.mark.parametrize("kind", KINDS)
def test_a_real_response_cut_short_by_the_peer_is_truncated(kind: str) -> None:
    found = s_real_truncation(http_module, kind)
    assert found == []


# --- 2. detach ----------------------------------------------------------------------------------


class TimedSock(FakeSock):
    """A socket fake that also accepts timeout calls, like a real one."""

    def settimeout(self, value: float | None) -> None:
        return None

    def gettimeout(self) -> float | None:
        return None


class BlockingSock(FakeSock):
    def __init__(self) -> None:
        super().__init__()
        self.in_close = threading.Event()
        self.release = threading.Event()

    def close(self) -> None:
        self.calls.append(("close",))
        self.in_close.set()
        self.release.wait(5)


def s_job_detach(mod: Any) -> list[str]:
    bad: list[str] = []
    job, sock = mod._Job("l", 30), FakeSock()
    job.attach(sock)
    job.detach()
    if job._sock is not None or sock.calls:
        bad.append(f"detach did not just clear the reference: {sock.calls}")
    attempt(bad, "second detach", job.detach)
    attempt(bad, "detach on a fresh job", mod._Job("l", 30).detach)
    attempt(bad, "abort after detach", job.abort)
    if sock.calls or not job.cancelled.is_set():
        bad.append(f"abort after detach touched the socket or did not cancel: {sock.calls}")
    twice, once = mod._Job("l", 30), FakeSock()
    twice.attach(once)
    twice.abort()
    twice.abort()
    if once.calls != CLOSED:
        bad.append(f"a second abort closed the socket again: {once.calls}")
    ended, late = mod._Job("l", 30), FakeSock()
    ended.abort()
    ended.attach(late)
    if late.calls != CLOSED:
        bad.append(f"attach after abort did not close at once: {late.calls}")
    attempt(bad, "detach after abort", ended.detach)
    if late.calls != CLOSED:
        bad.append("detach closed again")
    # abort closes INSIDE the lock: detach cannot return while the close runs
    holder, slow = mod._Job("l", 30), BlockingSock()
    holder.attach(slow)
    closer = threading.Thread(target=holder.abort, daemon=True)
    detacher = threading.Thread(target=holder.detach, daemon=True)
    try:
        closer.start()
        if not slow.in_close.wait(2):
            bad.append("abort never reached close")
        else:
            detacher.start()
            detacher.join(0.3)
            if not detacher.is_alive():
                bad.append("detach returned while a close was running")
    finally:
        slow.release.set()
        closer.join(2)
        detacher.join(2)
    if detacher.is_alive() or closer.is_alive():
        bad.append("a thread did not finish after the close was released")
    return bad


def test_detach_forgets_the_socket_never_closes_and_waits_for_a_running_close() -> None:
    found = s_job_detach(http_module)
    assert found == []


class Spy:
    """A job that counts attach and detach calls."""

    @staticmethod
    def of(mod: Any, label: str, seconds: float = 30) -> Any:
        class CountJob(mod._Job):  # type: ignore[misc]
            attached = 0
            detaches = 0

            def attach(self, sock: Any) -> None:
                if sock is not None:
                    self.attached += 1
                super().attach(sock)

            def detach(self) -> None:
                self.detaches += 1
                super().detach()

        return CountJob(label, seconds)


def s_open_detach(mod: Any, kind: str) -> list[str]:
    bad: list[str] = []
    label = LABEL[kind]

    def run(
        name: str,
        build: Callable[[Any, TimedSock], Fake],
        wanted: Callable[[Any], bool],
        closes: bool = False,
        seconds: float = 30,
    ) -> None:
        job, sock = Spy.of(mod, label, seconds), TimedSock()
        got = call_with_job(mod, kind, job, Opener(lambda r, t: build(job, sock)))
        if not wanted(got):
            bad.append(f"{name}: unexpected result {got!r}")
        if job.attached != 1:
            bad.append(f"{name}: control, the socket was attached {job.attached} times")
        if job.detaches != 1 or job._sock is not None:
            bad.append(f"{name}: detached {job.detaches} times, held={job._sock is not None}")
        if sock.calls != (CLOSED if closes else []):
            bad.append(f"{name}: socket calls {sock.calls}")
        job.abort()  # a late abort after the read must not touch the socket
        if sock.calls != (CLOSED if closes else []):
            bad.append(f"{name}: a late abort touched the socket: {sock.calls}")

    run("success", lambda j, s: Fake([b"{}"], 0, s), lambda g: g == {})
    run(
        "size cap",
        lambda j, s: Fake([b"x" * 5000], 0, s),
        lambda g: isinstance(g, ConnectorResponseError) and "exceeds" in str(g),
    )
    run(
        "truncated",
        lambda j, s: Fake([b"1"], 5, s),
        lambda g: isinstance(g, ConnectorResponseError) and str(g) == TRUNCATED,
    )

    def slow(j: Any, s: TimedSock) -> Fake:
        f = Fake([b"{", b"}"], 0, s)
        f.hook = lambda _: time.sleep(0.3)
        return f

    run("deadline", slow, lambda g: is_text(g, timed_out(kind)), seconds=0.2)

    def broken(j: Any, s: TimedSock) -> Fake:
        f = Fake([b"{"], 0, s)

        def boom(_: Fake) -> None:
            raise OSError("reset")

        f.hook = boom
        return f

    run("read error", broken, lambda g: is_text(g, f"request to {label} failed"))

    def cancelled(j: Any, s: TimedSock) -> Fake:
        f = Fake([b"{", b"}"], 0, s)
        f.hook = lambda _: j.abort()
        return f

    run("cancel", cancelled, lambda g: is_text(g, timed_out(kind)), closes=True)

    job, sock = Spy.of(mod, label), FakeSock()

    def http_error(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            URL[kind],
            503,
            "m",
            http.client.HTTPMessage(),
            None,  # type: ignore[arg-type]
        )

    got = call_with_job(mod, kind, job, Opener(http_error))
    expected = f"HTTP 503 from {label}"
    if not (isinstance(got, ConnectorTransportError) and str(got) == expected):
        bad.append(f"HTTP error: {got!r}")
    if job._sock is not None or sock.calls:
        bad.append("HTTP error: a socket was held or touched")
    return bad


@pytest.mark.parametrize("kind", KINDS)
def test_open_json_detaches_on_every_exit_and_a_late_abort_touches_nothing(kind: str) -> None:
    found = s_open_detach(http_module, kind)
    assert found == []


# --- mutations on copies of divesafe.data.http --------------------------------------------------

RAISE = '        raise ConnectorResponseError("response was truncated")\n'
COND = "    if isinstance(owed, int) and not isinstance(owed, bool) and owed > 0:\n"
OWED = '    owed = getattr(response, "length", None)\n'
DETACH_BLOCK = (
    "                body = read_limited(response, max_bytes, deadline_at)\n"
    "            finally:\n"
    "                if job is not None:\n"
    "                    job.detach()\n"
)
DETACH_BODY = "        with self._lock:\n            self._sock = None\n"
ABORT_CLOSE = "            if sock is not None:\n                _close_socket(sock)\n"
TAKE = "            sock, self._sock = self._sock, None\n"

HTTP_MUTANTS = [
    pytest.param([(RAISE, "        pass\n")], id="truncation-check-removed"),
    pytest.param(
        [(COND, COND.replace(" and not isinstance(owed, bool)", ""))], id="check-applies-to-bool"
    ),
    pytest.param(
        [(COND, COND.replace("if isinstance", "if owed is None or isinstance"))],
        id="check-applies-to-none",
    ),
    pytest.param(
        [(COND, COND.replace("owed, int)", "owed, int | float)", 1))], id="check-applies-to-float"
    ),
    pytest.param(
        [(OWED, OWED.replace("None)", "None) if deadline_at is not None else None"))],
        id="check-only-on-the-deadline-path",
    ),
    pytest.param(
        [(OWED, OWED.replace("None)", "None) if deadline_at is None else None"))],
        id="check-only-on-the-no-deadline-path",
    ),
    pytest.param(
        [
            (
                RAISE,
                '        raise ConnectorResponseError(f"response was truncated: {response.url}")\n',
            )
        ],
        id="error-text-carries-the-url",
    ),
    pytest.param(
        [(RAISE, '        raise ConnectorResponseError("response truncated")\n')],
        id="error-text-changed",
    ),
    pytest.param(
        [
            (
                DETACH_BLOCK,
                "                body = read_limited(response, max_bytes, deadline_at)\n"
                "                if job is not None:\n"
                "                    job.detach()\n"
                "            finally:\n"
                "                pass\n",
            )
        ],
        id="detach-not-in-finally",
    ),
    pytest.param(
        [("                    job.detach()\n", "                    pass\n")],
        id="detach-never-called",
    ),
    pytest.param(
        [(DETACH_BODY, DETACH_BODY.replace("self._sock = None", "pass"))],
        id="detach-clears-nothing",
    ),
    pytest.param(
        [
            (
                DETACH_BODY,
                "        with self._lock:\n"
                "            sock, self._sock = self._sock, None\n"
                "            if sock is not None:\n"
                "                _close_socket(sock)\n",
            )
        ],
        id="detach-closes-the-socket",
    ),
    pytest.param([(DETACH_BODY, "        self._sock = None\n")], id="detach-not-under-the-lock"),
    pytest.param(
        [
            (
                TAKE + ABORT_CLOSE,
                TAKE + "        if sock is not None:\n            _close_socket(sock)\n",
            )
        ],
        id="abort-closes-outside-the-lock",
    ),
    pytest.param(
        [(TAKE, "            sock = self._sock\n")], id="abort-keeps-the-reference-and-closes-twice"
    ),
]


def http_plan() -> list[tuple[str, Callable[[Any], list[str]]]]:
    plan: list[tuple[str, Callable[[Any], list[str]]]] = [
        ("truncation", s_truncation),
        ("job", s_job_detach),
    ]
    for kind in KINDS:
        plan.append((f"open/{kind}", lambda mod, k=kind: s_truncation_open(mod, k)))
        plan.append((f"detach/{kind}", lambda mod, k=kind: s_open_detach(mod, k)))
        plan.append((f"real/{kind}", lambda mod, k=kind: s_real_truncation(mod, k)))
    return plan


@pytest.mark.parametrize("edits", HTTP_MUTANTS)
def test_every_mutation_of_truncation_and_detach_is_caught(edits: list[tuple[str, str]]) -> None:
    mutant = mutated_module(http_module, *edits)
    misses: list[str] = []
    crashes: list[str] = []
    try:
        for name, scenario in http_plan():
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
