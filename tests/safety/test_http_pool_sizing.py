"""Sizing and lifecycle of the dedicated connector HTTP pool (`http_pool_size`,
`configure_http_pool`, `shutdown_http_pool`) and its wiring in `build_default_state` and the app
lifespan.

Offline: no socket, no database. Every test restores the module pool (`restore_pool`), so other
tests see the default pool. Blocked workers wait on an Event that is always released in a
`finally`; a call that might hang (a `wait=True` mutant) runs in a daemon thread with a join
timeout. Mutations run on COPIES (`mutated_module`) of `divesafe.data.http`,
`divesafe.api.state` and `divesafe.api.app`; a copy has its own pools, which `cleanup` shuts down.
A mutant that raises in a call the real code answers is counted as a miss of that check.

EQUIVALENT MUTANTS (not tested):
  - `min(CEILING, x)` as `min(x, CEILING)`; `max(1, y)` as `max(y, 1)`;
  - the order of `previous = _POOL` and building the new pool in `configure_http_pool`;
  - `pool, _POOL = _POOL, _DEFAULT_POOL` written as two statements;
  - the wording of the sizing log message beyond the message text pinned here, and the order of
    its extra keys;
  - `shutdown_http_pool()` placed before the `close` lookup versus after it, as long as it runs
    before the provider is closed (the order is tested through the provider's `close`).

GAP: the lifespan order "runner.shutdown, then the pool, then the provider" is tested through
observed pool state at each step, not by a mutation that moves the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import _build
from tests.safety.test_http_deadline_pool import (
    KINDS,
    PARAMS,
    URL,
    Opener,
    Resp,
    install,
    is_text,
    make,
    timed_out,
)

from divesafe.api import app as app_module
from divesafe.api import state as state_module
from divesafe.api.runner import RunManager
from divesafe.config import Settings
from divesafe.data import http as http_module
from divesafe.models import FakeProvider

pytestmark = pytest.mark.safety

TOKEN = "tok-SECRET-123"
SIZED = "connector HTTP pool sized"


@pytest.fixture(autouse=True)
def restore_pool() -> Iterator[None]:
    yield
    http_module.shutdown_http_pool()
    assert http_module._POOL is http_module._DEFAULT_POOL


def cleanup(mod: Any) -> None:
    with contextlib.suppress(Exception):
        mod.shutdown_http_pool()
    if mod is not http_module:
        with contextlib.suppress(Exception):
            mod._DEFAULT_POOL.shutdown(wait=False, cancel_futures=True)


def attempt(bad: list[str], label: str, call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{label}: raised {type(exc).__name__}")
        return None


def refuses(pool: Any) -> bool:
    try:
        pool.submit(lambda: None)
    except RuntimeError:
        return True
    return False


def works(pool: Any) -> bool:
    try:
        return bool(pool.submit(lambda: 5).result(2) == 5)
    except Exception:  # noqa: BLE001
        return False


def returns_within(call: Callable[[], Any], seconds: float) -> bool:
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive()


def block(pool: Any, count: int) -> tuple[threading.Event, list[threading.Event], list[Any]]:
    release = threading.Event()
    started = [threading.Event() for _ in range(count)]

    def hold(index: int) -> None:
        started[index].set()
        release.wait(10)

    return release, started, [pool.submit(hold, i) for i in range(count)]


# --- scenarios: mod -> misses -------------------------------------------------------------------

SIZES = [
    (4, 4, 16),
    (5, 4, 20),
    (6, 4, 24),
    (6, 64, 64),
    (6, 11, 64),
    (6, 10, 60),
    (8, 8, 64),
    (8, 9, 64),
    (7, 9, 63),
    (1000, 1000, 64),
    (0, 4, 1),
    (4, 0, 1),
    (0, 0, 1),
    (1, 1, 1),
]


def s_size(mod: Any) -> list[str]:
    bad: list[str] = []
    if mod.HTTP_POOL_CEILING != 64:
        bad.append("the ceiling is not 64")
    for connectors, runs, wanted in SIZES:
        got = attempt(
            bad, f"{connectors}x{runs}", lambda c=connectors, r=runs: mod.http_pool_size(c, r)
        )
        if got != wanted:
            bad.append(f"{connectors} x {runs}: {got!r}, wanted {wanted}")
    grid = [[mod.http_pool_size(c, r) for r in range(0, 70)] for c in range(0, 12)]
    for row in grid:
        if row != sorted(row):
            bad.append("not monotonic in runs")
            break
    for column in zip(*grid, strict=True):
        if list(column) != sorted(column):
            bad.append("not monotonic in connectors")
            break
    if any(not 1 <= n <= 64 for row in grid for n in row):
        bad.append("a size outside 1..64")
    return bad


def s_configure(mod: Any) -> list[str]:
    bad: list[str] = []
    first = attempt(bad, "configure(5)", lambda: mod.configure_http_pool(5))
    if first is None:
        return bad
    if first._max_workers != 5 or mod._POOL is not first:
        bad.append("configure(5) did not install a pool of 5")
    name = first.submit(lambda: threading.current_thread().name).result(2)
    if not name.startswith("divesafe-http"):
        bad.append(f"thread name {name!r}")
    second = attempt(bad, "configure(3)", lambda: mod.configure_http_pool(3))
    if second is None:
        return bad
    if second is first or mod._POOL is not second or second._max_workers != 3:
        bad.append("the second configure did not install its own pool")
    if not refuses(first):
        bad.append("the first sized pool was not shut by the second configure")
    if refuses(mod._DEFAULT_POOL) or not works(mod._DEFAULT_POOL):
        bad.append("the default pool was shut by configure")
    if mod._DEFAULT_POOL._max_workers != mod.HTTP_POOL_WORKERS:
        bad.append("the default pool is not HTTP_POOL_WORKERS")
    for asked, wanted in ((0, 1), (-4, 1), (1, 1), (64, 64), (65, 64), (1000, 64)):
        pool = attempt(bad, f"configure({asked})", lambda a=asked: mod.configure_http_pool(a))
        if pool is not None and (pool._max_workers != wanted or mod._POOL is not pool):
            bad.append(f"configure({asked}) gave {pool._max_workers}, wanted {wanted}")
    mod.shutdown_http_pool()
    mod.configure_http_pool(2)
    mod.shutdown_http_pool()
    if not works(mod._DEFAULT_POOL):
        bad.append("the default pool is not usable after the sized pools")
    return bad


def s_shutdown(mod: Any) -> list[str]:
    bad: list[str] = []
    pool = mod.configure_http_pool(2)
    release, started, futures = block(pool, 2)
    try:
        if not all(e.wait(2) for e in started):
            return ["the sized pool could not be filled"]
        queued = pool.submit(lambda: 1)
        if not returns_within(mod.shutdown_http_pool, 1.0):
            bad.append("shutdown waited for a blocked worker")
        if not queued.cancelled():
            bad.append("a queued job was not cancelled")
        if mod._POOL is not mod._DEFAULT_POOL:
            bad.append("the default pool was not restored")
        if not refuses(pool):
            bad.append("the sized pool still accepts work")
    finally:
        release.set()
    for future in futures:
        if attempt(bad, "blocked job", lambda f=future: f.result(2)) is not None:
            bad.append("a blocked job returned a value")
    attempt(bad, "second shutdown", mod.shutdown_http_pool)
    attempt(bad, "third shutdown", mod.shutdown_http_pool)
    if refuses(mod._DEFAULT_POOL) or not works(mod._DEFAULT_POOL):
        bad.append("the default pool was shut down")
    return bad


def s_replace_does_not_wait(mod: Any) -> list[str]:
    bad: list[str] = []
    pool = mod.configure_http_pool(2)
    release, started, _ = block(pool, 2)
    try:
        if not all(e.wait(2) for e in started):
            return ["the sized pool could not be filled"]
        if not returns_within(lambda: mod.configure_http_pool(3), 1.0):
            bad.append("configure waited for the blocked workers of the old pool")
    finally:
        release.set()
    return bad


HTTP_SCENARIOS: list[tuple[str, Callable[[Any], list[str]]]] = [
    ("size", s_size),
    ("configure", s_configure),
    ("shutdown", s_shutdown),
    ("replace", s_replace_does_not_wait),
]


@pytest.mark.parametrize("name,scenario", HTTP_SCENARIOS, ids=[n for n, _ in HTTP_SCENARIOS])
def test_the_real_pool_functions_meet_every_scenario(
    name: str, scenario: Callable[[Any], list[str]]
) -> None:
    found = scenario(http_module)
    assert found == []


def test_the_default_pool_is_the_initial_module_pool() -> None:
    assert http_module._POOL is http_module._DEFAULT_POOL
    assert http_module._DEFAULT_POOL._max_workers == http_module.HTTP_POOL_WORKERS == 8


# --- (e) a saturated sized pool -----------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_saturated_sized_pool_still_fails_at_the_deadline_and_spares_the_default_executor(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = http_module.configure_http_pool(3)
    opener = Opener(lambda r, t: Resp(lambda n: b""))
    built = install(monkeypatch, opener)
    release, started, _ = block(pool, 3)

    async def drive() -> tuple[Exception | None, float, int]:
        getter = make(http_module, kind, 5.0, 0.3)
        began = time.perf_counter()
        error: Exception | None = None
        try:
            await getter.get_json(URL[kind], PARAMS)
        except Exception as exc:  # noqa: BLE001
            error = exc
        elapsed = time.perf_counter() - began
        return error, elapsed, await asyncio.wait_for(asyncio.to_thread(lambda: 7), 1.0)

    try:
        assert all(e.wait(2) for e in started)
        error, elapsed, side = asyncio.run(asyncio.wait_for(drive(), 4))
    finally:
        release.set()
    http_module.shutdown_http_pool()
    assert is_text(error, timed_out(kind))
    assert 0.25 <= elapsed < 1.5
    assert side == 7
    assert built == [] and opener.timeouts == []  # the queued job never opened a connection


# --- (c) wiring in build_default_state ----------------------------------------------------------

BOTH = {"phn_etide_enabled": True, "met_api_token": TOKEN}
CASES: list[tuple[dict[str, Any], int, int]] = [
    ({}, 4, 16),
    ({"phn_etide_enabled": True}, 5, 20),
    ({"met_api_token": TOKEN}, 5, 20),
    (BOTH, 6, 24),
    ({**BOTH, "max_runs_in_flight": 64}, 6, 64),
    ({"max_runs_in_flight": 2}, 4, 8),
    ({"max_runs_in_flight": 1, "max_runs_per_actor": 1}, 4, 4),
]


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def state_misses(mod: Any) -> list[str]:
    bad: list[str] = []
    package = logging.getLogger("divesafe")
    for kwargs, connectors, wanted in CASES:
        label = f"{sorted(kwargs)}"
        http_module.shutdown_http_pool()
        capture, previous = Capture(), package.level
        package.addHandler(capture)
        package.setLevel(logging.DEBUG)
        try:
            with pytest.MonkeyPatch.context() as m:
                for name in ("DIVESAFE_MET_API_TOKEN", "DIVESAFE_PHN_ETIDE_ENABLED"):
                    m.delenv(name, raising=False)
                settings = Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]
                state = mod.build_default_state(settings)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{label}: build_default_state raised {type(exc).__name__}")
            continue
        finally:
            package.removeHandler(capture)
            package.setLevel(previous)
        pool = http_module._POOL
        if len(state.connectors) != connectors:
            bad.append(f"{label}: {len(state.connectors)} connectors, wanted {connectors}")
        if pool is http_module._DEFAULT_POOL or pool._max_workers != wanted:
            bad.append(f"{label}: pool of {pool._max_workers}, wanted {wanted}")
        elif not pool._thread_name_prefix.startswith("divesafe-http"):
            bad.append(f"{label}: thread prefix {pool._thread_name_prefix!r}")
        sized = [r for r in capture.records if r.getMessage() == SIZED]
        if len(sized) != 1:
            bad.append(f"{label}: the sizing log was written {len(sized)} times")
        else:
            extra = {
                k: sized[0].__dict__.get(k)
                for k in (
                    "http_pool_workers",
                    "connectors",
                    "max_runs_in_flight",
                    "http_pool_ceiling",
                )
            }
            runs = kwargs.get("max_runs_in_flight", 4)
            if extra != {
                "http_pool_workers": wanted,
                "connectors": connectors,
                "max_runs_in_flight": runs,
                "http_pool_ceiling": 64,
            }:
                bad.append(f"{label}: log extras {extra}")
        for record in capture.records:
            text = record.getMessage() + " ".join(str(v) for v in record.__dict__.values())
            if TOKEN in text:
                bad.append(f"{label}: the token reached a log record")
                break
    return bad


def test_build_default_state_sizes_the_pool_and_logs_once_without_secrets() -> None:
    found = state_misses(state_module)
    assert found == []


# --- (d) wiring in the lifespan -----------------------------------------------------------------


def lifespan_misses(mod: Any, runner_raises: bool) -> list[str]:
    bad: list[str] = []
    events: list[str] = []

    def pool_name() -> str:
        return "default" if http_module._POOL is http_module._DEFAULT_POOL else "sized"

    class Closing(FakeProvider):
        def close(self) -> None:
            events.append(f"provider:{pool_name()}")

    original = RunManager.shutdown

    async def recording(self: RunManager) -> None:
        events.append(f"runner:{pool_name()}")
        if runner_raises:
            raise RuntimeError("runner shutdown failed")
        await original(self)

    http_module.shutdown_http_pool()
    client, _, _ = _build(provider=Closing(), live=False)
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    with pytest.MonkeyPatch.context() as m:
        m.setattr(RunManager, "shutdown", recording)
        wired = TestClient(mod.create_app(state))
        sized = None
        try:
            with wired:
                sized = http_module.configure_http_pool(3)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"the lifespan raised {type(exc).__name__}")
    if sized is not None and not refuses(sized):
        bad.append("the sized pool was not shut when the app stopped")
    if http_module._POOL is not http_module._DEFAULT_POOL:
        bad.append("the module pool is not the default after the app stopped")
    if refuses(http_module._DEFAULT_POOL) or not works(http_module._DEFAULT_POOL):
        bad.append("the default pool was shut down by the app")
    if events != ["runner:sized", "provider:default"]:
        bad.append(f"order of shutdown steps: {events}")
    return bad


@pytest.mark.parametrize("runner_raises", [False, True], ids=["clean", "runner-raises"])
def test_the_lifespan_shuts_the_pool_after_the_runner_and_before_the_provider(
    runner_raises: bool,
) -> None:
    found = lifespan_misses(app_module, runner_raises)
    assert found == []


# --- mutations on copies ------------------------------------------------------------------------

SIZE = "max(1, min(HTTP_POOL_CEILING, connectors * max_runs_in_flight))"
CLAMP = "max_workers=max(1, min(HTTP_POOL_CEILING, workers))"
SHUT = "pool.shutdown(wait=False, cancel_futures=True)"
HTTP_EDITS = [
    pytest.param(
        [(SIZE, "max(1, connectors * max_runs_in_flight)")], id="size-ceiling-not-applied"
    ),
    pytest.param(
        [(SIZE, "min(HTTP_POOL_CEILING, connectors * max_runs_in_flight)")], id="size-floor-removed"
    ),
    pytest.param(
        [(SIZE, "max(1, min(HTTP_POOL_CEILING, connectors + max_runs_in_flight))")],
        id="size-adds-instead-of-multiplying",
    ),
    pytest.param([(CLAMP, "max_workers=max(1, workers)")], id="configure-ceiling-not-applied"),
    pytest.param(
        [(CLAMP, "max_workers=min(HTTP_POOL_CEILING, workers)")], id="configure-floor-removed"
    ),
    pytest.param(
        [('_THREAD_PREFIX = "divesafe-http"', '_THREAD_PREFIX = "pool"')],
        id="thread-prefix-changed",
    ),
    pytest.param(
        [("    if previous is not _DEFAULT_POOL:\n", "    if True:\n")],
        id="configure-shuts-the-default-pool",
    ),
    pytest.param(
        [("        previous.shutdown(wait=False)\n", "        pass\n")], id="previous-pool-not-shut"
    ),
    pytest.param(
        [("previous.shutdown(wait=False)", "previous.shutdown(wait=True)")],
        id="configure-waits-for-the-old-pool",
    ),
    pytest.param(
        [("    if pool is not _DEFAULT_POOL:\n", "    if True:\n")], id="shutdown-shuts-the-default"
    ),
    pytest.param([(SHUT, "pool.shutdown(wait=False)")], id="cancel-futures-removed"),
    pytest.param([(SHUT, "pool.shutdown(wait=True, cancel_futures=True)")], id="shutdown-waits"),
    pytest.param(
        [("pool, _POOL = _POOL, _DEFAULT_POOL", "pool = _POOL")], id="default-not-restored"
    ),
]


@pytest.mark.parametrize("edits", HTTP_EDITS)
def test_every_mutation_of_the_pool_functions_is_caught(edits: list[tuple[str, str]]) -> None:
    mutant = mutated_module(http_module, *edits)
    misses: list[str] = []
    crashes: list[str] = []
    try:
        for name, scenario in HTTP_SCENARIOS:
            try:
                misses += scenario(mutant)
            except Exception as exc:  # noqa: BLE001
                crashes.append(f"{name}: {type(exc).__name__}")
            if misses:
                break
            cleanup(mutant)
    finally:
        cleanup(mutant)
    assert misses != [], f"survived (crashes: {crashes})"


LOG_EXTRA = '            "connectors": len(connectors),\n'
STATE_EDITS = [
    pytest.param(
        [("    configure_http_pool(pool_size)\n", "    pass\n")], id="configure-not-called"
    ),
    pytest.param(
        [
            (
                "http_pool_size(len(connectors), settings.max_runs_in_flight)",
                "http_pool_size(len(connectors) + 1, settings.max_runs_in_flight)",
            )
        ],
        id="connector-count-off-by-one",
    ),
    pytest.param(
        [
            (
                "http_pool_size(len(connectors), settings.max_runs_in_flight)",
                "http_pool_size(len(connectors), settings.max_runs_per_actor)",
            )
        ],
        id="sized-by-runs-per-actor",
    ),
    pytest.param(
        [
            (
                LOG_EXTRA,
                LOG_EXTRA + '            "token": settings.met_api_token.get_secret_value()'
                " if settings.met_api_token else None,\n",
            )
        ],
        id="log-carries-the-token",
    ),
    pytest.param(
        [('"connector HTTP pool sized",', '"connector pool sized",')], id="log-message-changed"
    ),
    pytest.param(
        [
            (
                '            "http_pool_workers": pool_size,\n',
                '            "http_pool_workers": 0,\n',
            )
        ],
        id="log-reports-the-wrong-size",
    ),
]


@pytest.mark.parametrize("edits", STATE_EDITS)
def test_every_mutation_of_the_state_wiring_is_caught(edits: list[tuple[str, str]]) -> None:
    mutant = mutated_module(state_module, *edits)
    found = state_misses(mutant)
    assert found != []


APP_EDITS = [
    pytest.param(
        [("            shutdown_http_pool()\n", "            pass\n")], id="shutdown-not-called"
    ),
]


@pytest.mark.parametrize("edits", APP_EDITS)
@pytest.mark.parametrize("runner_raises", [False, True], ids=["clean", "runner-raises"])
def test_every_mutation_of_the_lifespan_is_caught(
    edits: list[tuple[str, str]], runner_raises: bool
) -> None:
    mutant = mutated_module(app_module, *edits)
    found = lifespan_misses(mutant, runner_raises)
    assert found != []
