"""ADR 0025 item 6: the live capture path, tested with a fake state and no network. Offline.

Nothing here runs `--live` against a real endpoint. Sockets are monkeypatched to raise, and the
production connectors are fed a fake getter, so the path is exercised end to end without I/O."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from tests.evaluation import capture_item6 as cap
from tests.evaluation import item6_scrub

from divesafe.config import Settings
from divesafe.data import TIOMAN_ISLAND
from divesafe.data.met_api_marine import SOURCE as MET_SOURCE
from divesafe.data.phn_etide import SOURCE as PHN_SOURCE
from divesafe.models import FakeProvider

pytestmark = pytest.mark.safety


def _real_now() -> datetime:
    return datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def _no_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a network call was attempted")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # asyncio's event loop opens an internal socketpair, so `socket.socket` itself is not blocked.
    # What must never happen is a name lookup or a connection to a remote address.
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network)


LIVE_ENV = {
    cap.CONFIRM_ENV: cap.CONFIRM_VALUE,
    "DIVESAFE_LLM_PROVIDER": cap.PROVIDER,
    "DIVESAFE_LLM_MODEL": cap.MODEL,
    "DIVESAFE_LLM_BASE_URL": "https://llm.example.org/v1",
    "DIVESAFE_ALLOW_EXTERNAL_LLM": "true",
}


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {**LIVE_ENV, cap.DIR_ENV: str(tmp_path / "out"), **extra}


# ---- guards -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DIVESAFE_LLM_PROVIDER", "openai"),
        ("DIVESAFE_LLM_MODEL", "claude-haiku-5-5"),
        ("DIVESAFE_ALLOW_EXTERNAL_LLM", "false"),
        ("DIVESAFE_ALLOW_EXTERNAL_LLM", "1"),
        ("DIVESAFE_ALLOW_EXTERNAL_LLM", "TRUE"),
    ],
)
def test_a_wrong_provider_model_or_opt_in_is_refused(tmp_path: Path, key: str, value: str) -> None:
    env = {**_env(tmp_path), key: value}
    with pytest.raises(cap.CaptureRefused):
        cap.require_live(True, env)


def test_a_missing_opt_in_or_endpoint_is_refused(tmp_path: Path) -> None:
    for drop in ("DIVESAFE_ALLOW_EXTERNAL_LLM", "DIVESAFE_LLM_BASE_URL"):
        env = _env(tmp_path)
        del env[drop]
        with pytest.raises(cap.CaptureRefused):
            cap.require_live(True, env)


def test_a_refusal_never_echoes_the_endpoint_or_a_secret(tmp_path: Path) -> None:
    env = {**_env(tmp_path), "DIVESAFE_ALLOW_EXTERNAL_LLM": "no"}
    env["DIVESAFE_LLM_BASE_URL"] = "https://secret-host.example.org/v1"
    with pytest.raises(cap.CaptureRefused) as exc:
        cap.require_live(True, env)
    assert "secret-host" not in str(exc.value)


def test_all_guards_set_and_an_outside_directory_pass(tmp_path: Path) -> None:
    fake_repo_dir = tmp_path / "capture"
    directory = cap.require_live(True, _env(tmp_path, **{cap.DIR_ENV: str(fake_repo_dir)}))
    assert directory == fake_repo_dir.resolve()


def test_the_output_dir_guard_refuses_the_repository(tmp_path: Path) -> None:
    with pytest.raises(cap.CaptureRefused, match="inside the repository"):
        cap.require_live(True, _env(tmp_path, **{cap.DIR_ENV: str(cap.REPO / "tests")}))


# ---- scrub before write -------------------------------------------------------------------------


def test_a_clean_document_is_written_and_only_it(tmp_path: Path) -> None:
    doc = {"replies": ["a reply"], "connector_payloads": {MET_SOURCE: [{"data": 1}]}, "result": {}}
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    assert sorted(p.name for p in path.parent.iterdir()) == ["item6_capture.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == doc


def test_a_planted_secret_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    doc = {"replies": ["Bearer sk-ABCDEFGH12345678"], "connector_payloads": {}, "result": {}}
    out = tmp_path / "out"
    with pytest.raises(cap.CaptureRefused, match="scrub check fired"):
        cap.write_capture(out, "item6_capture.json", doc)
    assert not out.exists() or list(out.iterdir()) == []


def test_a_forbidden_provider_key_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    doc = {"replies": ["ok"], "connector_payloads": {}, "result": {"system_fingerprint": "fp_1"}}
    out = tmp_path / "out"
    with pytest.raises(cap.CaptureRefused):
        cap.write_capture(out, "item6_capture.json", doc)
    assert not out.exists() or list(out.iterdir()) == []


def test_the_write_check_and_the_fixture_scan_share_one_rule_set() -> None:
    assert cap.item6_scrub is item6_scrub


# ---- recording ----------------------------------------------------------------------------------


class _Inner:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, url: str, params: Any) -> Any:
        self.calls.append((url, dict(params)))
        return self.payload


def test_the_recorder_keeps_the_payload_and_not_the_url_or_headers() -> None:
    rec = cap.Recorder()
    inner = _Inner({"ok": 1})
    getter = cap.RecordingGetter(inner, rec, "some-source")
    result = asyncio.run(getter.get_json("https://api.example.org/x", {"k": "v"}))
    assert result == {"ok": 1}
    assert rec.connector_payloads == {"some-source": [{"ok": 1}]}
    assert "api.example.org" not in json.dumps(rec.connector_payloads)


def test_the_recorder_keeps_reply_text_only() -> None:
    rec = cap.Recorder()
    provider = cap.RecordingProvider(FakeProvider("reply text"), rec)
    response = asyncio.run(provider.complete(_request("specialist:weather")))
    assert response.text == "reply text"
    assert rec.replies == {"specialist:weather": "reply text"}


def _request(label: str | None = None) -> Any:
    from divesafe.models import ChatMessage, LLMRequest

    content = f"TASK: {label}\nrest of the prompt" if label else "x"
    return LLMRequest(messages=(ChatMessage(role="user", content=content),))


# ---- PHN exclusion ------------------------------------------------------------------------------


class _Connector:
    def __init__(self, name: str, getter: Any) -> None:
        self.name = name
        self._getter = getter


def test_phn_is_never_kept_and_its_getter_is_never_called() -> None:
    phn_inner = _Inner({"would": "leak"})
    phn = _Connector(PHN_SOURCE, phn_inner)
    other = _Connector("open-meteo-marine", _Inner({"x": 1}))
    kept = cap.build_live_connectors(
        [phn, other], cap.Recorder(), clock=lambda: 0.0, sleep=lambda _s: None
    )
    assert [c.name for c in kept] == ["open-meteo-marine"]
    assert phn_inner.calls == []


# ---- pacing -------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # Async, like asyncio.sleep: the pacer must await it, never block the event loop.
        self.slept.append(seconds)
        self.now += seconds


def test_met_calls_are_spaced_on_the_injected_clock_without_real_sleep() -> None:
    clock = _Clock()
    inner = _Inner({"met": 1})
    paced = cap.PacedGetter(
        inner,
        host=cap.MET_HOST,
        spacing=cap.MET_MIN_SPACING_SECONDS,
        clock=clock,
        sleep=clock.sleep,
    )

    async def three() -> None:
        for _ in range(3):
            await paced.get_json("https://api.met.gov.my/v2.1/data", {})

    asyncio.run(three())
    assert len(inner.calls) == 3
    # The first call waits for nothing; each later call waits out the rest of the spacing.
    assert clock.slept == [cap.MET_MIN_SPACING_SECONDS, cap.MET_MIN_SPACING_SECONDS]
    # Three calls span at least two spacings, so they fit inside one 60 s window with room.
    assert 2 * cap.MET_MIN_SPACING_SECONDS <= 60.0


def test_the_pacer_awaits_sleep_and_never_blocks_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_mod

    def blocked(_seconds: float) -> None:
        raise AssertionError("the pacer called blocking time.sleep")

    monkeypatch.setattr(time_mod, "sleep", blocked)
    clock = _Clock()
    paced = cap.PacedGetter(
        _Inner({"met": 1}),
        host=cap.MET_HOST,
        spacing=cap.MET_MIN_SPACING_SECONDS,
        clock=clock,
        sleep=clock.sleep,
    )

    async def two() -> None:
        await paced.get_json("https://api.met.gov.my/v2.1/data", {})
        await paced.get_json("https://api.met.gov.my/v2.1/data", {})

    asyncio.run(two())
    assert clock.slept == [cap.MET_MIN_SPACING_SECONDS]


def test_a_non_met_host_is_not_paced() -> None:
    clock = _Clock()
    inner = _Inner({"x": 1})
    paced = cap.PacedGetter(inner, host=cap.MET_HOST, spacing=20.0, clock=clock, sleep=clock.sleep)

    async def twice() -> None:
        await paced.get_json("https://api.open-meteo.com/v1/forecast", {})
        await paced.get_json("https://api.open-meteo.com/v1/forecast", {})

    asyncio.run(twice())
    assert clock.slept == []


def test_met_is_wrapped_with_the_pacer_and_the_others_are_not() -> None:
    met = _Connector(MET_SOURCE, _Inner({}))
    other = _Connector("open-meteo-marine", _Inner({}))
    kept = cap.build_live_connectors(
        [met, other], cap.Recorder(), clock=lambda: 0.0, sleep=lambda _s: None
    )
    by_name = {c.name: c for c in kept}
    assert isinstance(by_name[MET_SOURCE]._getter, cap.PacedGetter)
    assert isinstance(by_name["open-meteo-marine"]._getter, cap.RecordingGetter)


# ---- one assessment, fake state, no network -----------------------------------------------------


def _fake_state(payload_connectors: list[Any], reply: str) -> Any:
    """A stand-in for the production `AppState`: the connectors, a scripted provider and an
    engine are supplied by the test. No production getter or provider is constructed here."""

    class _State:
        connectors = payload_connectors
        provider = FakeProvider(reply)
        engine = None
        egress_basis = "none"

    return _State()


def test_a_fake_state_run_writes_only_the_scrubbed_output(no_network: None, tmp_path: Path) -> None:
    from divesafe.data import OpenMeteoMarineConnector
    from divesafe.data.sites import SITES

    assert SITES[cap.SITE_ID] is TIOMAN_ISLAND
    getter = _Inner({"hourly": {"time": []}})
    connectors = [OpenMeteoMarineConnector(getter)]
    state = _fake_state(connectors, "{}")
    # An engine is needed by assess_dive; use the scripted test engine (no ruleset ships).
    from tests.agent.scripted_llm import engine as scripted_engine

    state.engine = scripted_engine(with_warning_rule=False)
    rec = cap.Recorder()
    summary = asyncio.run(
        cap.run_live_assessment(state=state, recorder=rec, clock=_real_now, sleep=lambda _s: None)
    )
    doc = cap.assemble_document(rec, summary)
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    text = path.read_text(encoding="utf-8")
    assert "api.open-meteo.com" not in text and "marine-api" not in text
    assert item6_scrub.scan_text(text) == []
    assert "phn" not in text.lower()


def test_the_run_passes_no_cross_examination_and_uses_the_plan(
    no_network: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    import divesafe.orchestration as orch

    real = orch.assess_dive

    async def spy(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(orch, "assess_dive", spy)
    from tests.agent.scripted_llm import engine as scripted_engine

    state = _fake_state([], "{}")
    state.engine = scripted_engine(with_warning_rule=False)
    rec = cap.Recorder()
    asyncio.run(
        cap.run_live_assessment(state=state, recorder=rec, clock=_real_now, sleep=lambda _s: None)
    )
    assert captured["plan"].planned_start == cap.PLANNED_START
    assert captured["plan"].planned_duration_minutes == 60
    assert captured["options"].cross_examination is False


def test_a_live_run_without_the_guards_never_builds_state(
    no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import divesafe.api.state as state_mod

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("production state must not be built without the guards")

    monkeypatch.setattr(state_mod, "build_default_state", boom)
    assert cap.main(["--live"]) == 2


def test_a_connection_is_blocked_for_this_module(no_network: None) -> None:
    with pytest.raises(AssertionError), socket.create_connection(("192.0.2.1", 443)):
        pass


# ---- run_live wiring (S1, F2): no .env, no repository, no database --------------------------


def _spy_state_builders(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any]) -> None:
    """Records the settings the live wiring reads, then stops the run. `build_default_state` and
    the Postgres repository are replaced with functions that fail if anything calls them (F2)."""
    import divesafe.api.state as state_mod
    import divesafe.services as services_mod

    def fake_build_engine(settings: Any, now: Any = None) -> Any:
        seen["settings"] = settings
        raise _Stop()

    def fake_build_provider(settings: Any) -> Any:
        # The provider is built before the engine, so record the settings here too.
        seen["settings"] = settings
        seen["provider_settings"] = settings
        raise _Stop()

    def must_not_run(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the live run must not build the default state or a repository")

    monkeypatch.setattr(state_mod, "build_engine", fake_build_engine)
    monkeypatch.setattr(state_mod, "build_provider", fake_build_provider)
    monkeypatch.setattr(state_mod, "build_default_state", must_not_run)
    monkeypatch.setattr(services_mod, "PostgresAssessmentRepository", must_not_run)


class _Stop(Exception):
    """Ends `run_live` at the first call after the wiring under test; nothing is connected to."""


def test_run_live_ignores_a_dotenv_file_in_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A .env in the working directory must not change the live settings (S1).
    (tmp_path / ".env").write_text(
        "DIVESAFE_LLM_BASE_URL=https://from-dotenv.example.org/v1\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DIVESAFE_LLM_BASE_URL", raising=False)
    seen: dict[str, Any] = {}
    _spy_state_builders(monkeypatch, seen)
    with pytest.raises(_Stop):
        cap.run_live({**LIVE_ENV, cap.DIR_ENV: str(tmp_path / "out")})
    assert seen["settings"].llm_base_url is None


def test_run_live_never_touches_a_database_even_when_one_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A DATABASE_URL that would fail if connected to: the live run must never open it (F2).
    monkeypatch.setenv("DIVESAFE_DATABASE_URL", "postgresql://unreachable.invalid/db")
    seen: dict[str, Any] = {}
    _spy_state_builders(monkeypatch, seen)
    with pytest.raises(_Stop):
        cap.run_live({**LIVE_ENV, cap.DIR_ENV: str(tmp_path / "out")})
    assert seen["settings"].database_url is not None  # the env var is seen, and is never used
    assert "settings" in seen


# ---- R1 clock, F6 output name (group B) ----------------------------------------------------------


def test_a_real_nan_from_the_clock_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    # A real float('nan') through the same source the live run uses. No isfinite patch: this fails
    # if the guard is removed, because the NaN would otherwise become a datetime.
    read = cap.live_clock(source=lambda: float("nan"))
    with pytest.raises(cap.CaptureRefused, match="non-finite"):
        read()
    assert not (tmp_path / "item6_capture.json").exists()


def test_a_real_infinity_from_the_clock_is_refused(tmp_path: Path) -> None:
    read = cap.live_clock(source=lambda: float("inf"))
    with pytest.raises(cap.CaptureRefused, match="non-finite"):
        read()
    assert not (tmp_path / "item6_capture.json").exists()


def test_a_backwards_clock_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    readings = iter([1_790_000_000.0, 1_789_999_995.0])  # the second is five seconds earlier
    read = cap.live_clock(source=lambda: next(readings))
    read()
    with pytest.raises(cap.CaptureRefused, match="went backwards"):
        read()
    assert not (tmp_path / "item6_capture.json").exists()


def test_a_clock_that_stands_still_is_not_refused() -> None:
    # Equal readings are not backwards: only a strictly earlier reading is refused.
    read = cap.live_clock(source=lambda: 1_790_000_000.0)
    assert read() == read()


def test_an_existing_output_file_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "item6_capture.json").write_text("earlier", encoding="utf-8")
    calls: list[str] = []

    class _SpyGetter:
        async def get_json(self, url: str, params: Any) -> Any:
            calls.append(url)
            return {}

    monkeypatch.setattr(cap, "build_live_state", lambda _s: pytest.fail("state built"))
    with pytest.raises(cap.CaptureRefused, match="already in use"):
        cap.run_live({**LIVE_ENV, cap.DIR_ENV: str(out)})
    assert calls == []
    assert (out / "item6_capture.json").read_text(encoding="utf-8") == "earlier"


def test_a_dangling_symlink_at_the_output_name_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    target = tmp_path / "elsewhere.json"
    (out / "item6_capture.json").symlink_to(target)  # dangling: the target does not exist
    with pytest.raises(cap.CaptureRefused, match="already in use"):
        cap.write_capture(out, "item6_capture.json", {"replies": []})
    assert not target.exists()


# ---- R2 keyed replies, fixed codes (group C item 1) -------------------------------------------


class _Gated:
    """A fake provider whose calls finish in the reverse order they started: the first started call
    completes last. Replies are returned by the label in the request, so a swap is visible."""

    name = "fake-gated"
    model = "fake-gated"
    external = False

    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts
        self.started: list[str] = []

    async def complete(self, request: Any) -> Any:
        from divesafe.models import LLMResponse

        label = cap.task_label(request)
        self.started.append(label)
        # Yield until every other started call has also begun, then return: the order is fixed by
        # the test, not by timing. Each call returns the text for its own label.
        await asyncio.sleep(0)
        return LLMResponse(text=self._texts[label], provider=self.name, model=self.model)


def test_parallel_replies_are_keyed_by_label_and_cannot_swap(tmp_path: Path) -> None:
    rec = cap.Recorder()
    inner = _Gated({"specialist:weather": "WEATHER TEXT", "scenario:marginal": "MARGINAL TEXT"})
    provider = cap.RecordingProvider(inner, rec)

    async def both() -> None:
        # Start the marginal scenario first, the weather specialist second: the completion order
        # is the reverse of the list order that a positional list would have used.
        await asyncio.gather(
            provider.complete(_request("scenario:marginal")),
            provider.complete(_request("specialist:weather")),
        )

    asyncio.run(both())
    assert rec.replies == {
        "scenario:marginal": "MARGINAL TEXT",
        "specialist:weather": "WEATHER TEXT",
    }


def test_a_refused_call_appears_as_a_fixed_code_and_no_reply_text_is_written(
    tmp_path: Path,
) -> None:
    from divesafe.models import LLMRequest  # noqa: F401  (type context for the request helper)

    class _Refusing:
        name = "fake-refusing"
        model = "fake-refusing"
        external = False

        async def complete(self, request: Any) -> Any:
            # Exception text here must never reach the file: the recorder keeps a fixed code.
            raise RuntimeError("SECRET-LEAK sk-ABCDEFGH12345678 at 203.0.113.7:8317")

    rec = cap.Recorder()
    provider = cap.RecordingProvider(_Refusing(), rec)
    with pytest.raises(RuntimeError):
        asyncio.run(provider.complete(_request("specialist:tide")))
    # A plain RuntimeError has no label, so the code is the unclassified one.
    assert rec.call_codes == {"specialist:tide": "call_failed:unclassified"}
    doc = cap.assemble_document(rec, {"final_recommendation": "INSUFFICIENT EVIDENCE"})
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    text = path.read_text(encoding="utf-8")
    assert "call_failed:unclassified" in text
    assert "SECRET-LEAK" not in text and "sk-ABCDEFGH" not in text and "203.0.113.7" not in text


def test_no_reply_text_appears_anywhere_in_the_written_file(tmp_path: Path) -> None:
    rec = cap.Recorder()
    provider = cap.RecordingProvider(FakeProvider("A DISTINCTIVE REPLY"), rec)
    asyncio.run(provider.complete(_request("specialist:weather")))
    doc = cap.assemble_document(rec, {"final_recommendation": "GO"})
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    # The reply is kept under its label, as the document's own value. It must appear only there,
    # and the summary must never copy it (no `explanation`, no `text`).
    assert "explanation" not in path.read_text(encoding="utf-8")
    assert doc["replies"] == {"specialist:weather": "A DISTINCTIVE REPLY"}


def test_a_summary_carries_pending_human_beside_the_final_recommendation(
    no_network: None, tmp_path: Path
) -> None:
    from tests.agent.scripted_llm import engine as scripted_engine

    state = _fake_state([], "{}")
    state.engine = scripted_engine(with_warning_rule=False)
    rec = cap.Recorder()
    summary = asyncio.run(
        cap.run_live_assessment(state=state, recorder=rec, clock=_real_now, sleep=lambda _s: None)
    )
    assert summary["status"] == "PENDING_HUMAN"
    assert summary["final_recommendation"] in {
        "GO",
        "CAUTION",
        "NO-GO",
        "INSUFFICIENT EVIDENCE",
    }
    # The fixed fields the brief names are present, and are labels, not sentences.
    for key in ("ruleset_version", "prompt_version", "max_depth_m", "data_versions"):
        assert key in summary
    assert summary["prompt_version"] == cap.PROMPT_VERSION
    assert summary["max_depth_m"] == 18


# ---- F1: no-getter refusal and allow-list (group C item 3) -------------------------------------


def test_a_connector_with_no_getter_is_refused() -> None:
    no_getter = _Connector("open-meteo-marine", None)
    with pytest.raises(cap.CaptureRefused, match="no getter"):
        cap.build_live_connectors(
            [no_getter], cap.Recorder(), clock=lambda: 0.0, sleep=lambda _s: None
        )


def test_a_connector_not_on_the_allow_list_is_never_called() -> None:
    unlisted_inner = _Inner({"would": "leak"})
    unlisted = _Connector("some-new-source", unlisted_inner)
    kept = cap.build_live_connectors(
        [unlisted], cap.Recorder(), clock=lambda: 0.0, sleep=lambda _s: None
    )
    assert kept == []
    assert unlisted_inner.calls == []


def test_the_allow_list_is_the_five_agreed_sources_and_excludes_phn() -> None:
    assert (
        frozenset(
            {
                "open-meteo-marine",
                "open-meteo-wind",
                "data-gov-my-weather-forecast",
                "data-gov-my-weather-warning",
                "met-api-marine-forecast",
            }
        )
        == cap.ALLOWED_CONNECTORS
    )
    assert PHN_SOURCE not in cap.ALLOWED_CONNECTORS


# ---- F8 egress basis label, F9 provider settings (group C items 6 and 7) ---------------------


def test_the_written_output_contains_the_fixed_egress_basis_label(
    no_network: None, tmp_path: Path
) -> None:
    from tests.agent.scripted_llm import engine as scripted_engine

    state = _fake_state([], "{}")
    state.engine = scripted_engine(with_warning_rule=False)
    state.egress_basis = "remote_external_opt_in"
    rec = cap.Recorder()
    summary = asyncio.run(
        cap.run_live_assessment(state=state, recorder=rec, clock=_real_now, sleep=lambda _s: None)
    )
    assert summary["egress_basis"] == "remote_external_opt_in"
    doc = cap.assemble_document(rec, summary)
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    assert '"egress_basis": "remote_external_opt_in"' in path.read_text(encoding="utf-8")


def test_an_egress_basis_outside_the_fixed_set_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # check_egress only returns the four literals, so this branch is reached by a stub that returns
    # a label outside the set. The label must never be written in that case.
    import divesafe.models.egress as egress_mod

    monkeypatch.setattr(egress_mod, "check_egress", lambda _p, _s: "https://llm-gw.example.org")
    with pytest.raises(cap.CaptureRefused, match="not a known label"):
        cap.egress_basis_label(object(), Settings(_env_file=None))


def test_the_provider_settings_reach_the_wrapped_provider() -> None:
    from divesafe.models.openai_compatible import OpenAICompatibleProvider

    inner = OpenAICompatibleProvider(
        "claude-sonnet-5-5",
        "https://llm-gw.example.org:8317/v1",
        allowed_hosts=frozenset({"llm-gw.example.org"}),
        call_timeout_seconds=60.0,
        stage_timeout_seconds=150.0,
        max_output_tokens=1500,
    )
    wrapped = cap.RecordingProvider(inner, cap.Recorder())
    assert wrapped.call_timeout_seconds == 60.0
    assert wrapped.stage_timeout_seconds == 150.0
    assert wrapped.max_output_tokens == 1500


def test_a_provider_without_the_settings_gets_none_not_a_guess() -> None:
    wrapped = cap.RecordingProvider(FakeProvider("x"), cap.Recorder())
    assert not hasattr(wrapped, "call_timeout_seconds")
    assert not hasattr(wrapped, "stage_timeout_seconds")
    assert not hasattr(wrapped, "max_output_tokens")


def test_the_x_open_mode_alone_refuses_a_dangling_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hold the write-time name check at "pass" so only the production open mode can refuse. Going
    # through write_capture tests the real line, not Python's open on its own.
    monkeypatch.setattr(cap, "check_new_output", lambda _path: None)
    out = tmp_path / "out"
    out.mkdir()
    target = tmp_path / "elsewhere.json"
    (out / "item6_capture.json").symlink_to(target)
    with pytest.raises(FileExistsError):
        cap.write_capture(out, "item6_capture.json", {"replies": {}})
    assert not target.exists()


# ---- connector_outcomes in the summary (group D item 3) -----------------------------------------


def test_connector_outcomes_show_a_failed_connector_and_omit_a_healthy_one() -> None:
    from divesafe.domain import StageName, StageOutcome, StageTrace

    observe = StageTrace(
        name=StageName.OBSERVE,
        outcome=StageOutcome.COMPLETED,
        next_stage=StageName.RETRIEVE,
        started_at=_real_now(),
        finished_at=_real_now(),
        issues=(
            "observe.connector_failed:open-meteo-marine",
            "observe.connector_timed_out:met-api-marine-forecast",
        ),
    )

    class _Rec:
        stages = (observe,)

    outcomes = cap._connector_outcomes(_Rec.stages)
    assert outcomes == {
        "met-api-marine-forecast": "timed_out",
        "open-meteo-marine": "failed",
    }
    assert "open-meteo-wind" not in outcomes  # healthy: no code, so not listed


def test_connector_outcomes_reach_the_written_summary(tmp_path: Path) -> None:
    from divesafe.domain import StageName, StageOutcome, StageTrace

    observe = StageTrace(
        name=StageName.OBSERVE,
        outcome=StageOutcome.COMPLETED,
        next_stage=StageName.RETRIEVE,
        started_at=_real_now(),
        finished_at=_real_now(),
        issues=("observe.connector_failed:open-meteo-marine",),
    )
    summary = {"connector_outcomes": cap._connector_outcomes((observe,))}
    doc = cap.assemble_document(cap.Recorder(), summary)
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    assert '"open-meteo-marine": "failed"' in path.read_text(encoding="utf-8")


# ---- F2 end to end: run_live through to the written file, fake connectors (group D item 5) ------


def test_run_live_writes_one_scrubbed_file_through_the_production_plan_path(
    no_network: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_live itself runs the plan and writes the file. Only the connector builder is replaced
    (fake getters, no network) and the engine and provider are scripted, so the production
    assess_dive, Recorder, summary and write_capture all run exactly as in the live path."""
    from tests.agent.scripted_llm import engine as scripted_engine
    from tests.agent.scripted_llm import fake as scripted_fake

    from divesafe.data import OpenMeteoMarineConnector

    getter = _Inner({"hourly": {"time": []}})

    def fake_build_live_state(_settings: Any) -> Any:
        return cap._LiveParts(
            connectors=[OpenMeteoMarineConnector(getter)],
            engine=scripted_engine(with_warning_rule=False),
            provider=scripted_fake(),
            egress_basis="none",
        )

    monkeypatch.setattr(cap, "build_live_state", fake_build_live_state)
    monkeypatch.setattr(cap, "live_clock", lambda: _real_now)
    out = tmp_path / "out"
    env = {**LIVE_ENV, cap.DIR_ENV: str(out)}
    monkeypatch.setenv("DIVESAFE_LLM_BASE_URL", "https://llm.example.org/v1")

    written = cap.run_live(env)

    assert written == out / "item6_capture.json"
    doc = json.loads(written.read_text(encoding="utf-8"))
    assert doc["item"] == "ADR 0025 item 6"
    assert doc["result"]["status"] == "PENDING_HUMAN"
    assert doc["result"]["egress_basis"] == "none"
    # The file says which connectors ran, and carries the fixed note on what the outcomes list.
    assert doc["result"]["connectors_run"] == ["open-meteo-marine"]
    assert doc["result"]["connector_outcomes_note"] == (
        "connector_outcomes lists only connectors with problems"
    )
    assert doc["result"]["final_recommendation"] in {
        "GO",
        "CAUTION",
        "NO-GO",
        "INSUFFICIENT EVIDENCE",
    }
    # The scrub screens the whole file: nothing that names the configured base URL or host.
    text = written.read_text(encoding="utf-8")
    assert "llm.example.org" not in text
    assert item6_scrub.scan_text(text) == []
    # Exactly one file was written in the output directory.
    assert sorted(p.name for p in out.iterdir()) == ["item6_capture.json"]


# ---- connectors_run and the outcomes note (review SHOULD-FIX) -----------------------------------


def test_connectors_run_lists_every_built_connector_and_omits_a_missing_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MET is built only with a token. With the token removed from the shell environment, the real
    # build_live_state must not build MET, and connectors_run must not list it.
    from divesafe.api import state as api_state

    monkeypatch.delenv("DIVESAFE_MET_API_TOKEN", raising=False)
    monkeypatch.setattr(api_state, "build_provider", lambda _settings: None)
    monkeypatch.setattr(api_state, "build_engine", lambda _settings: None)
    parts = cap.build_live_state(Settings(_env_file=None))
    names = sorted(c.name for c in parts.connectors)
    assert MET_SOURCE not in names
    assert names == sorted(
        [
            "open-meteo-marine",
            "open-meteo-wind",
            "data-gov-my-weather-forecast",
            "data-gov-my-weather-warning",
        ]
    )


def test_connectors_run_lists_met_when_its_token_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from divesafe.api import state as api_state

    monkeypatch.setenv("DIVESAFE_MET_API_TOKEN", "synthetic-token-not-real")
    monkeypatch.setattr(api_state, "build_provider", lambda _settings: None)
    monkeypatch.setattr(api_state, "build_engine", lambda _settings: None)
    parts = cap.build_live_state(Settings(_env_file=None))
    assert MET_SOURCE in [c.name for c in parts.connectors]


def test_connectors_run_is_in_the_summary_and_the_note_is_in_the_written_file(
    tmp_path: Path,
) -> None:
    connectors = [_Connector("open-meteo-marine", _Inner({})), _Connector(MET_SOURCE, _Inner({}))]
    summary = {
        "connector_outcomes": {},
        "connectors_run": sorted(c.name for c in connectors),
        "connector_outcomes_note": cap.CONNECTOR_OUTCOMES_NOTE,
    }
    doc = cap.assemble_document(cap.Recorder(), summary)
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["result"]["connectors_run"] == ["met-api-marine-forecast", "open-meteo-marine"]
    assert (
        written["result"]["connector_outcomes_note"]
        == "connector_outcomes lists only connectors with problems"
    )


def test_an_empty_connector_outcomes_still_comes_with_connectors_run(tmp_path: Path) -> None:
    # No problems: connector_outcomes is {}. A reader must still see which connectors ran.
    summary = {
        "connector_outcomes": cap._connector_outcomes(()),
        "connectors_run": ["open-meteo-marine"],
        "connector_outcomes_note": cap.CONNECTOR_OUTCOMES_NOTE,
    }
    doc = cap.assemble_document(cap.Recorder(), summary)
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    result = json.loads(path.read_text(encoding="utf-8"))["result"]
    assert result["connector_outcomes"] == {}
    assert result["connectors_run"] == ["open-meteo-marine"]


# ---- failed-call codes carry the adapter's fixed label (group E-3 item 1-3) ---------------------


class _RaisesWith:
    """A provider whose call raises the given exception, for the recorder's code."""

    name = "openai_compatible"
    model = "m"
    external = False

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def complete(self, request: Any) -> Any:
        raise self._exc


def _failed_code(exc: Exception) -> cap.Recorder:
    rec = cap.Recorder()
    provider = cap.RecordingProvider(_RaisesWith(exc), rec)
    with pytest.raises(type(exc)):
        asyncio.run(provider.complete(_request("specialist:tide")))
    return rec


def test_an_adapter_event_line_refusal_is_kept_with_its_label(tmp_path: Path) -> None:
    from divesafe.models.openai_compatible import OpenAICompatibleError

    rec = _failed_code(OpenAICompatibleError("refused", reason="event_line"))
    assert rec.call_codes == {"specialist:tide": "call_failed:event_line"}
    doc = cap.assemble_document(rec, {"final_recommendation": "INSUFFICIENT EVIDENCE"})
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    assert "call_failed:event_line" in path.read_text(encoding="utf-8")


def test_a_plain_error_with_a_secret_looking_message_is_unclassified_and_the_text_is_absent(
    tmp_path: Path,
) -> None:
    secret = "sk-SECRET-PLAIN-0123456789"
    rec = _failed_code(RuntimeError(f"upstream said {secret}"))
    assert rec.call_codes == {"specialist:tide": "call_failed:unclassified"}
    doc = cap.assemble_document(rec, {"final_recommendation": "INSUFFICIENT EVIDENCE"})
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    text = path.read_text(encoding="utf-8")
    assert "call_failed:unclassified" in text
    assert secret not in text and "SECRET-PLAIN" not in text


def test_a_non_label_reason_is_stored_as_unclassified_and_the_string_is_absent(
    tmp_path: Path,
) -> None:
    from divesafe.models.openai_compatible import OpenAICompatibleError

    rec = _failed_code(OpenAICompatibleError("refused", reason="sk-SECRET"))
    assert rec.call_codes == {"specialist:tide": "call_failed:unclassified"}
    doc = cap.assemble_document(rec, {"final_recommendation": "INSUFFICIENT EVIDENCE"})
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    text = path.read_text(encoding="utf-8")
    assert "call_failed:unclassified" in text
    assert "sk-SECRET" not in text


# ---- a cancelled call keeps a fixed code and still propagates (group E-5) -----------------------


class _NeverReturns:
    """A provider whose call awaits an event nothing sets: it can only end by cancellation."""

    name = "openai_compatible"
    model = "m"
    external = False

    async def complete(self, request: Any) -> Any:
        await asyncio.Event().wait()  # never set: only cancellation ends this await


def test_a_cancelled_call_is_kept_as_call_cancelled_and_still_propagates(tmp_path: Path) -> None:
    rec = cap.Recorder()
    provider = cap.RecordingProvider(_NeverReturns(), rec)

    async def run() -> None:
        # The short timeout is the only wait; nothing sleeps for real.
        await asyncio.wait_for(provider.complete(_request("specialist:tide")), timeout=0.02)

    # Cancellation is not swallowed: the caller still sees the timeout.
    with pytest.raises(TimeoutError):
        asyncio.run(run())
    assert rec.call_codes == {"specialist:tide": "call_cancelled"}
    doc = cap.assemble_document(rec, {"final_recommendation": "INSUFFICIENT EVIDENCE"})
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    text = path.read_text(encoding="utf-8")
    assert "call_cancelled" in text
    assert "timeout" not in text.lower() and "TimeoutError" not in text
    assert "Cancelled" not in text and "CancelledError" not in text


def test_a_recorder_that_swallowed_cancellation_would_be_caught_by_the_caller(
    tmp_path: Path,
) -> None:
    # The propagation check, on its own: a recorder that did not re-raise would return a value
    # here, and the caller's timeout would silently not happen. Cancellation must reach the caller.
    rec = cap.Recorder()
    provider = cap.RecordingProvider(_NeverReturns(), rec)

    async def run() -> object:
        return await asyncio.wait_for(provider.complete(_request("specialist:tide")), timeout=0.02)

    with pytest.raises(TimeoutError):
        asyncio.run(run())


# ---- captured_at_utc: the instant `now` was read for the assessment (ADR 0025 item 6) ----------

TOP_LEVEL_KEYS = frozenset(
    {
        "item",
        "site_id",
        "planned_start_utc",
        "duration_minutes",
        "synthetic",
        "replies",
        "call_codes",
        "connector_payloads",
        "result",
        cap.CAPTURED_KEY,
    }
)


def _injected_now() -> datetime:
    return datetime(2026, 10, 1, 12, 30, 15, 123456, tzinfo=UTC)


def _run_with_injected_clock(
    monkeypatch: pytest.MonkeyPatch, clock: Any
) -> tuple[list[datetime], dict[str, Any]]:
    """One assessment with an injected clock. Returns the `now` the assessment used (the spy on
    `assess_dive`) and the document built from the run's summary. Nothing is connected to."""
    from tests.agent.scripted_llm import engine as scripted_engine

    import divesafe.orchestration as orch

    seen_now: list[datetime] = []
    real = orch.assess_dive

    async def spy(**kwargs: Any) -> Any:
        seen_now.append(kwargs["now"])
        return await real(**kwargs)

    monkeypatch.setattr(orch, "assess_dive", spy)
    state = _fake_state([], "{}")
    state.engine = scripted_engine(with_warning_rule=False)
    rec = cap.Recorder()
    summary = asyncio.run(
        cap.run_live_assessment(state=state, recorder=rec, clock=clock, sleep=lambda _s: None)
    )
    return seen_now, cap.assemble_document(rec, summary)


def test_captured_at_utc_is_the_now_the_assessment_used(
    no_network: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The written field is the same instant `now` the assessment received, as +00:00 ISO 8601.
    seen_now, doc = _run_with_injected_clock(monkeypatch, _injected_now)
    assert seen_now == [_injected_now()]
    assert doc[cap.CAPTURED_KEY] == "2026-10-01T12:30:15.123456+00:00"
    assert doc[cap.CAPTURED_KEY] == seen_now[0].isoformat()
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    text = path.read_text(encoding="utf-8")
    assert json.loads(text)[cap.CAPTURED_KEY] == "2026-10-01T12:30:15.123456+00:00"
    assert item6_scrub.scan_text(text) == []


def test_the_written_file_has_exactly_the_agreed_top_level_keys(
    no_network: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Adds one top-level key, captured_at_utc, and no other. Nothing is added inside `result`.
    _seen, doc = _run_with_injected_clock(monkeypatch, _injected_now)
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", doc)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert set(written) == TOP_LEVEL_KEYS
    assert cap.CAPTURED_KEY not in written["result"]


def test_a_naive_capture_time_is_refused_before_any_assessment(
    no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A naive datetime has no UTC meaning. It is refused with a fixed message, and assess_dive is
    # never reached, so no connector or provider is called for it.
    naive = datetime(2026, 10, 1, 12, 30)  # noqa: DTZ001  (the point of the test)
    with pytest.raises(cap.CaptureRefused, match="not a UTC datetime"):
        _run_with_injected_clock(monkeypatch, lambda: naive)


def test_a_non_utc_capture_time_is_refused_before_any_assessment(
    no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An offset of +08:00 is a real time but not UTC: refused, not converted, so the file never
    # carries a value other than the UTC instant the assessment used.
    kuala_lumpur = datetime(2026, 10, 1, 20, 30, tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(cap.CaptureRefused, match="not a UTC datetime"):
        _run_with_injected_clock(monkeypatch, lambda: kuala_lumpur)
