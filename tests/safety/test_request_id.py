"""ADR 0027 item 1: one request id per HTTP request.

An inbound `X-Request-ID` is kept only from a trusted proxy and only when it is one header that
fully matches `[A-Za-z0-9-]{1,64}`; anything else is replaced by a new UUID4 and never echoed or
logged. The id is on every log line of the request and of the run it starts, and in the response
header. It never reaches a prompt or the stored record (logs only, owner 2026-11-15). The run
span is pinned in `test_tracing_export.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.agent.scripted_llm import scripted
from tests.api_polling import create_and_poll, started
from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build

from divesafe.api import app as app_module
from divesafe.api import request_id as rid
from divesafe.config.logging import (
    JsonFormatter,
    bind_request_id,
    current_request_id,
    unbind_request_id,
)
from divesafe.data import http as data_http
from divesafe.models import FakeProvider
from divesafe.observability import current_collector, start_collecting

pytestmark = pytest.mark.safety

PROXY = "10.0.0.5"
GOOD = "proxy-Request-7f3a"
GENERATED = re.compile(r"[0-9a-f]{32}")
# Each is refused from a trusted proxy. None of them is a valid id, so none may appear anywhere.
REFUSED = [
    pytest.param("x" * 65, id="too-long"),
    pytest.param("MARKER_r8", id="underscore"),
    pytest.param("MARKER.r8", id="dot"),
    pytest.param("MARKER r8", id="space"),
    pytest.param("MARKER-r8;drop", id="semicolon"),
    pytest.param("MARKER-r8\t", id="tab"),
    pytest.param("MARKER-r8/../x", id="slash"),
]


class _Lines(logging.Handler):
    """Formats each record at once, in the thread and context that wrote it, as production does."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def parsed(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]


@pytest.fixture
def lines(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Lines]:
    # `create_app` replaces the root handlers; keep this one in place.
    monkeypatch.setattr(app_module, "configure_logging", lambda level="INFO": None)
    handler = _Lines()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _client(*, proxies: frozenset[str] = frozenset(), peer: str | None = None, **kw: Any) -> Any:
    client, _, repo = _build(**kw)
    client.app.state.divesafe.trusted_proxies = proxies  # type: ignore[attr-defined]
    if peer is not None:
        client = started(TestClient(client.app, client=(peer, 5000)))
    return client, repo


def _scope(peer: str | None, *headers: tuple[bytes, bytes]) -> dict[str, Any]:
    return {
        "type": "http",
        "client": None if peer is None else (peer, 5000),
        "headers": list(headers),
    }


# ---- choosing the id ---------------------------------------------------------------------------


def test_an_id_from_a_trusted_proxy_is_kept() -> None:
    scope = _scope(PROXY, (b"x-request-id", GOOD.encode()))
    assert rid.choose_request_id(scope, frozenset({PROXY})) == GOOD
    longest = _scope(f"::ffff:{PROXY}", (b"x-request-id", b"a" * 64))
    assert rid.choose_request_id(longest, frozenset({PROXY})) == "a" * 64


@pytest.mark.parametrize("peer", [None, "10.0.0.6", "testclient", "", "::1"])
def test_an_id_from_an_untrusted_peer_is_replaced(peer: str | None) -> None:
    scope = _scope(peer, (b"x-request-id", GOOD.encode()))
    chosen = rid.choose_request_id(scope, frozenset({PROXY}))
    assert chosen != GOOD and GENERATED.fullmatch(chosen)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 65,
        b"MARKER_r8",
        b"MARKER r8",
        b"MARKER-r8\n",
        b"MARKER-r8\r\nSet-Cookie: a=b",
        b"MARKER-r8\x00",
        b"MARKER-r8\x7f",
        "MARKER-r8-é".encode(),
        b"MARKER-r8\xff",
        "MARKER-r8-٣".encode(),  # a non-ASCII digit, which `\d` would accept
    ],
)
def test_an_invalid_id_from_a_trusted_proxy_is_replaced(raw: bytes) -> None:
    chosen = rid.choose_request_id(_scope(PROXY, (b"x-request-id", raw)), frozenset({PROXY}))
    assert GENERATED.fullmatch(chosen) and chosen.encode() != raw


def test_two_inbound_ids_from_a_trusted_proxy_are_both_refused() -> None:
    scope = _scope(PROXY, (b"x-request-id", GOOD.encode()), (b"x-request-id", b"second-id"))
    chosen = rid.choose_request_id(scope, frozenset({PROXY}))
    assert GENERATED.fullmatch(chosen) and chosen not in {GOOD, "second-id"}


def test_each_request_without_an_accepted_id_gets_a_new_one() -> None:
    ids = {rid.choose_request_id(_scope("10.0.0.6"), frozenset()) for _ in range(50)}
    assert len(ids) == 50


# ---- over HTTP: the header, the logs and no echo -----------------------------------------------


def test_the_response_carries_the_kept_id_from_a_trusted_proxy(lines: _Lines) -> None:
    client, _ = _client(proxies=frozenset({PROXY}), peer=PROXY)
    response = client.get("/v1/sites", headers={**AUTH_A, "X-Request-ID": GOOD})
    assert response.status_code == 200
    assert response.headers.get_list("x-request-id") == [GOOD]


@pytest.mark.parametrize("value", REFUSED)
@pytest.mark.parametrize("trusted", [True, False])
def test_a_refused_id_is_never_echoed_or_logged(value: str, trusted: bool, lines: _Lines) -> None:
    client, _ = _client(proxies=frozenset({PROXY}), peer=PROXY if trusted else "10.0.0.6")
    sent = value if trusted else GOOD
    responses = [
        client.get("/v1/sites", headers={**AUTH_A, "X-Request-ID": sent}),
        client.get("/v1/sites", headers={"Authorization": "Bearer wrong", "X-Request-ID": sent}),
        client.get("/v1/assessments/" + "f" * 32, headers={**AUTH_A, "X-Request-ID": sent}),
        client.post("/v1/assessments", json={"x": 1}, headers={**AUTH_A, "X-Request-ID": sent}),
    ]
    for response in responses:
        (chosen,) = response.headers.get_list("x-request-id")
        assert GENERATED.fullmatch(chosen)
        assert sent not in response.text
        assert all(sent not in v for _, v in response.headers.multi_items())
    assert lines.lines, "nothing was logged"
    assert all(sent not in line for line in lines.lines)


def test_an_invalid_header_value_from_a_proxy_never_reaches_the_response() -> None:
    client, _ = _client(proxies=frozenset({PROXY}), peer=PROXY)
    response = client.get("/health", headers={"X-Request-ID": "MARKER-r8 x"})
    assert "MARKER" not in str(response.headers.multi_items())


def test_every_response_carries_exactly_one_id() -> None:
    client, _ = _client()
    responses = [
        client.get("/health"),
        client.get("/v1/sites"),
        client.get("/v1/sites", headers=AUTH_A),
        client.get("/no-such-path", headers=AUTH_A),
        client.post("/v1/assessments", json={"x": 1}, headers=AUTH_A),
        client.post(
            "/v1/assessments",
            content=b"x" * (70 * 1024),
            headers={**AUTH_A, "content-type": "application/json"},
        ),
    ]
    assert [r.status_code for r in responses] == [200, 401, 200, 404, 422, 413]
    ids = [r.headers.get_list("x-request-id") for r in responses]
    assert all(len(found) == 1 and GENERATED.fullmatch(found[0]) for found in ids)
    assert len({found[0] for found in ids}) == len(ids)


def test_an_unhandled_error_keeps_the_id_on_its_response_and_log_line(lines: _Lines) -> None:
    client, _ = _client()
    state = client.app.state.divesafe

    class Boom:
        def authenticate(self, request: object) -> None:
            raise RuntimeError("store down")

    state.authenticator = Boom()
    quiet = TestClient(client.app, raise_server_exceptions=False)
    response = quiet.get("/v1/sites", headers=AUTH_A)
    assert response.status_code == 500
    (chosen,) = response.headers.get_list("x-request-id")
    assert chosen not in response.text and response.json() == {"detail": "internal error"}
    (error,) = [d for d in lines.parsed() if d["message"] == "unhandled error"]
    assert error["request_id"] == chosen


def test_the_id_is_unbound_after_each_request(lines: _Lines) -> None:
    client, _ = _client()
    client.get("/v1/sites", headers=AUTH_A)
    assert current_request_id() is None
    logging.getLogger("divesafe.test").info("after the request")
    assert "request_id" not in lines.parsed()[-1]


# ---- the run: logs carry the id; prompts and the record never do -------------------------------


def test_the_run_logs_carry_the_id_and_no_prompt_or_record_holds_it(lines: _Lines) -> None:
    provider = FakeProvider(scripted(recommendation="GO"))
    client, repo = _client(proxies=frozenset({PROXY}), peer=PROXY, provider=provider)
    final = create_and_poll(client, PLAN, AUTH_A, extra_headers={"X-Request-ID": GOOD})
    assert final.status_code == 200, final.text
    assessment_id = final.json()["record"]["id"]
    run_lines = [d for d in lines.parsed() if d.get("assessment_id") == assessment_id]
    messages = {d["message"] for d in run_lines}
    assert {"stage finished", "assessment stored"} <= messages
    assert all(d.get("request_id") == GOOD for d in run_lines)
    calls = [d for d in lines.parsed() if d["message"] == "model call finished"]
    assert calls and all(d.get("request_id") == GOOD for d in calls)
    assert provider.requests
    assert all(GOOD not in request.model_dump_json() for request in provider.requests)
    record = repo.get(assessment_id)
    assert record is not None and GOOD not in record.model_dump_json()
    assert GOOD not in final.text


def test_a_run_does_not_take_the_id_from_the_context_it_was_started_in(lines: _Lines) -> None:
    """The run gets its id only as an argument: one bound in the starting context is ignored."""
    from divesafe.api.runner import RunManager
    from divesafe.services import InMemoryAssessmentRepository

    async def scenario() -> None:
        manager = RunManager(InMemoryAssessmentRepository(), deadline=_td(5), grace=_td(1))

        async def work() -> Any:
            logging.getLogger("divesafe.test").info("inside the run")
            raise RuntimeError("stop")

        token = bind_request_id("outer-id")
        try:
            manager.start("a" * 32, "token", "alice", work)
        finally:
            unbind_request_id(token)
        await manager.shutdown()

    asyncio.run(scenario())
    (inside,) = [d for d in lines.parsed() if d["message"] == "inside the run"]
    assert "request_id" not in inside


def _td(seconds: int) -> Any:
    from datetime import timedelta

    return timedelta(seconds=seconds)


# ---- the formatter -----------------------------------------------------------------------------


def test_an_extra_field_cannot_forge_or_remove_the_id(lines: _Lines) -> None:
    log = logging.getLogger("divesafe.test")
    log.info("unbound", extra={"request_id": "forged-1"})
    token = bind_request_id(GOOD)
    try:
        log.info("bound", extra={"request_id": "forged-2"})
    finally:
        unbind_request_id(token)
    unbound, bound = lines.parsed()[-2:]
    assert "request_id" not in unbound and bound["request_id"] == GOOD
    assert "forged" not in "".join(lines.lines)


@pytest.mark.parametrize(
    "value", ["MARKER r8", "x" * 65, "", 7, b"abc", type("S", (str,), {})("abc")]
)
def test_an_invalid_value_is_never_bound(value: Any, lines: _Lines) -> None:
    token = bind_request_id(value)
    try:
        assert current_request_id() is None
        logging.getLogger("divesafe.test").info("line")
    finally:
        unbind_request_id(token)
    assert "request_id" not in lines.parsed()[-1]


def test_a_connector_worker_thread_logs_with_the_callers_id_and_nothing_else(
    lines: _Lines,
) -> None:
    """Only the id crosses to the worker: never the run's trace collector (not thread-safe)."""
    seen: list[object] = []

    def fetch(url: str, params: Any) -> str:
        seen.append(current_collector())
        logging.getLogger("divesafe.data.test").info("in the worker")
        return "ok"

    async def call() -> Any:
        start_collecting()
        assert current_collector() is not None
        token = bind_request_id(GOOD)
        try:
            return await data_http._call_in_pool(fetch, "https://example.test/x", {}, 5.0)
        finally:
            unbind_request_id(token)

    assert asyncio.run(call()) == "ok"
    (worker,) = [d for d in lines.parsed() if d["message"] == "in the worker"]
    assert worker["request_id"] == GOOD
    assert seen == [None]


# ---- the middleware itself ---------------------------------------------------------------------


def _middleware_run(inner_headers: list[tuple[bytes, bytes]]) -> tuple[list[Any], str | None]:
    sent: list[Any] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": inner_headers})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Any:
        return {"type": "http.request", "body": b""}

    async def send(message: Any) -> None:
        sent.append(message)

    async def scenario() -> str | None:
        middleware = rid.RequestIdMiddleware(inner, trusted_proxies=frozenset)
        await middleware(_scope("10.0.0.6", (b"x-request-id", GOOD.encode())), receive, send)
        return current_request_id()

    return sent, asyncio.run(scenario())


def test_an_id_set_inside_the_app_is_replaced_and_the_id_is_unbound_after() -> None:
    sent, after = _middleware_run([(b"x-request-id", b"inner-id"), (b"x-other", b"kept")])
    start = sent[0]
    found = [v for k, v in start["headers"] if k == b"x-request-id"]
    assert len(found) == 1 and GENERATED.fullmatch(found[0].decode())
    assert (b"x-other", b"kept") in start["headers"]
    assert after is None


@pytest.mark.parametrize("ends", ["stored", "failed"])
def test_a_failing_bind_never_changes_how_a_run_ends(
    ends: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose work succeeds is still stored; one whose work fails still stores its code."""
    from divesafe.api import runner as api_runner
    from divesafe.api.runner import RunManager
    from divesafe.services import InMemoryAssessmentRepository

    def broken(value: object) -> Any:
        raise RuntimeError("bind failed")

    monkeypatch.setattr(api_runner, "bind_request_id", broken)
    failed: list[tuple[str, Any]] = []
    completed: list[object] = []
    record = object()

    class _Store(InMemoryAssessmentRepository):
        def fail(self, run_id: str, run_token: str, code: Any) -> bool:
            failed.append((run_id, code))
            return True

        def complete(self, record: Any, run_token: str) -> bool:
            completed.append(record)
            return True

    async def scenario() -> None:
        manager = RunManager(_Store(), deadline=_td(5), grace=_td(1))

        async def work() -> Any:
            if ends == "failed":
                raise _WorkFailed
            return record

        manager.start("a" * 32, "token", "alice", work, request_id=GOOD)
        await manager.shutdown()  # waits for the run

    asyncio.run(scenario())
    if ends == "stored":
        assert completed == [record] and failed == []
    else:
        assert completed == []
        assert [(run_id, code.value) for run_id, code in failed] == [("a" * 32, "internal")]


class _WorkFailed(Exception):
    """A plain error: the run maps it to `internal` (never a subclass of a mapped error)."""


def test_a_refused_id_from_a_trusted_proxy_never_reaches_the_run(lines: _Lines) -> None:
    provider = FakeProvider(scripted(recommendation="GO"))
    client, repo = _client(proxies=frozenset({PROXY}), peer=PROXY, provider=provider)
    refused = "MARKER r8"
    first = client.post("/v1/assessments", json=PLAN, headers={**AUTH_A, "X-Request-ID": refused})
    assert first.status_code == 202, first.text
    (chosen,) = first.headers.get_list("x-request-id")
    assert GENERATED.fullmatch(chosen)
    from tests.api_polling import poll

    final = poll(client, first.headers["Location"], AUTH_A)
    assessment_id = final.json()["record"]["id"]
    run_lines = [d for d in lines.parsed() if d.get("assessment_id") == assessment_id]
    assert run_lines and all(d.get("request_id") == chosen for d in run_lines)
    assert all(refused not in line and "MARKER" not in line for line in lines.lines)
    assert refused not in "".join(r.model_dump_json() for r in provider.requests)
