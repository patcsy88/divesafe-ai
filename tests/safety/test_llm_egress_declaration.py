"""A loopback address does not show where a model server sends the prompt (ADR 0023).

A local proxy on localhost can forward every prompt to a hosted model, so a loopback server needs
the operator's declaration (or the external opt-in) before any plan or evidence data reaches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from divesafe.api.state import build_provider
from divesafe.config import Settings
from divesafe.models import FakeProvider
from divesafe.models import llm as llm_module
from divesafe.models.egress import check_egress
from divesafe.models.ollama import OllamaProvider
from divesafe.models.openai_compatible import OpenAICompatibleProvider

pytestmark = pytest.mark.safety

LOOPBACK_URL = "http://127.0.0.1:8317/v1"


class _Stub(FakeProvider):
    def __init__(self, external: bool) -> None:
        super().__init__()
        self.external = external


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type, call-arg]


def _with_stub(monkeypatch: pytest.MonkeyPatch, external: bool) -> None:
    monkeypatch.setitem(llm_module._REGISTRY, "openai", lambda s: _Stub(external))


def test_a_loopback_server_with_no_declaration_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_stub(monkeypatch, external=False)
    with pytest.raises(RuntimeError, match="loopback"):
        build_provider(_settings(llm_provider="openai"))


def test_the_declaration_or_the_external_opt_in_lets_a_loopback_server_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_stub(monkeypatch, external=False)
    assert build_provider(_settings(llm_provider="openai", llm_server_is_local=True)) is not None
    assert build_provider(_settings(llm_provider="openai", allow_external_llm=True)) is not None


def test_the_declaration_cannot_cover_a_server_that_is_not_on_this_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_stub(monkeypatch, external=True)
    with pytest.raises(RuntimeError, match="not on this machine"):
        build_provider(_settings(llm_provider="openai", llm_server_is_local=True))
    with pytest.raises(RuntimeError, match="off-machine"):
        build_provider(_settings(llm_provider="openai"))
    assert build_provider(_settings(llm_provider="openai", allow_external_llm=True)) is not None


def test_the_declaration_and_the_external_opt_in_contradict_each_other() -> None:
    with pytest.raises(ValidationError, match="contradict"):
        _settings(llm_server_is_local=True, allow_external_llm=True)


def test_only_the_exact_fake_provider_is_exempt() -> None:
    settings = _settings(llm_provider="fake")
    check_egress(FakeProvider(), settings)

    class _Subclass(FakeProvider):
        pass

    with pytest.raises(RuntimeError, match="loopback"):
        check_egress(_Subclass(), settings)


def test_the_real_adapters_on_loopback_need_the_declaration() -> None:
    compat = OpenAICompatibleProvider("m", LOOPBACK_URL)
    ollama = OllamaProvider("m", "http://localhost:11434")
    assert compat.external is False and ollama.external is False
    for provider in (compat, ollama):
        with pytest.raises(RuntimeError, match="loopback"):
            check_egress(provider, _settings())
        check_egress(provider, _settings(llm_server_is_local=True))


def test_the_declaration_defaults_to_off() -> None:
    assert _settings().llm_server_is_local is False


def test_the_declaration_is_read_from_the_environment_and_empty_means_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIVESAFE_LLM_SERVER_IS_LOCAL", "true")
    assert Settings(_env_file=None).llm_server_is_local is True  # type: ignore[call-arg]
    monkeypatch.setenv("DIVESAFE_LLM_SERVER_IS_LOCAL", "")
    assert Settings(_env_file=None).llm_server_is_local is False  # type: ignore[call-arg]
    monkeypatch.setenv("DIVESAFE_LLM_SERVER_IS_LOCAL", "true")
    monkeypatch.setenv("DIVESAFE_ALLOW_EXTERNAL_LLM", "true")
    with pytest.raises(ValidationError, match="contradict"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_default_application_state_goes_through_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from divesafe.api.state import build_default_state

    _with_stub(monkeypatch, external=False)
    with pytest.raises(RuntimeError, match="loopback"):
        build_default_state(_settings(llm_provider="openai"))
    assert build_default_state(_settings(llm_provider="openai", llm_server_is_local=True))


def test_the_basis_says_why_a_provider_was_let_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from divesafe.models.egress import check_egress

    assert check_egress(FakeProvider(), _settings()) == "none"
    assert check_egress(_Stub(False), _settings(llm_server_is_local=True)) == "declared_local"
    assert (
        check_egress(_Stub(False), _settings(allow_external_llm=True)) == "loopback_external_opt_in"
    )
    assert check_egress(_Stub(True), _settings(allow_external_llm=True)) == "remote_external_opt_in"


def test_the_start_up_log_records_the_basis(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_stub(monkeypatch, external=False)
    with caplog.at_level("WARNING"):
        build_provider(_settings(llm_provider="openai", allow_external_llm=True))
    record = next(r for r in caplog.records if r.getMessage() == "agent stage is ON")
    assert record.egress_basis == "loopback_external_opt_in"  # type: ignore[attr-defined]
    assert record.address_is_remote is False  # type: ignore[attr-defined]
    caplog.clear()
    _with_stub(monkeypatch, external=True)
    with caplog.at_level("WARNING"):
        build_provider(_settings(llm_provider="openai", allow_external_llm=True))
    record = next(r for r in caplog.records if r.getMessage() == "agent stage is ON")
    assert record.egress_basis == "remote_external_opt_in"  # type: ignore[attr-defined]
    assert record.address_is_remote is True  # type: ignore[attr-defined]


_CALLERS = {
    # name -> the only files that may mention it at all (call, index, pass as a value, iterate)
    "create_provider": {"models/llm.py", "models/__init__.py", "api/state.py"},
    "OllamaProvider": {"models/ollama.py"},
    "OpenAICompatibleProvider": {"models/openai_compatible.py"},
    "_factory": {"models/ollama.py", "models/openai_compatible.py"},
    "_REGISTRY": {"models/llm.py"},
}


def _mentions(name: str, text: str) -> bool:
    import re

    return bool(re.search(rf"(?<!\w){name}(?!\w)", text))


def test_only_the_start_up_path_builds_a_real_provider() -> None:
    """`create_provider`, the adapter constructors, their module factories and the registry do not
    check egress, so no other runtime code may mention them: a new caller must go through
    `build_provider` or call `check_egress`. A text scan per file under `src/divesafe`: a mention in
    a comment fails it too (safe), and an `as` alias, `getattr`, `functools.partial`, a second
    function in an allowed file, or code outside `src/divesafe` passes."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "divesafe"
    found: dict[str, set[str]] = {name: set() for name in _CALLERS}
    seen_where_allowed: dict[str, set[str]] = {name: set() for name in _CALLERS}
    for path in src.rglob("*.py"):
        rel = path.relative_to(src).as_posix()
        text = path.read_text()
        for name, allowed in _CALLERS.items():
            if _mentions(name, text):
                (seen_where_allowed if rel in allowed else found)[name].add(rel)
    assert found == {name: set() for name in _CALLERS}
    # Positive control: the pattern still matches in the allowed files (a definition counts), so
    # an edit that stops it matching cannot make this test pass by finding nothing.
    assert seen_where_allowed == _CALLERS


def test_the_caller_scan_pattern_matches_the_ways_a_provider_is_reached() -> None:
    for text in (
        "create_provider(s)",
        "models.create_provider (s)",
        "OllamaProvider('m', 'u')",
        "ollama._factory(s)",
        "llm._REGISTRY['ollama'](s)",
        "llm._REGISTRY.get('ollama')(s)",
        "for f in llm._REGISTRY.values(): f(s)",
        "loop.run_in_executor(None, create_provider, s)",
        "list(map(OllamaProvider, xs))",
        "from divesafe.models import create_provider as cp",
    ):
        assert any(_mentions(name, text) for name in _CALLERS), text
    assert not _mentions("create_provider", "my_create_provider(s)")
    assert not _mentions("create_provider", "create_provider_x(s)")


def test_every_registered_real_adapter_runs_the_loopback_peer_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`declared_local` is returned for any provider whose address is on this machine, and ADR 0022
    treats it as unfiltered, so each real adapter must verify the connected peer. A new adapter
    fails here until it does (or the exception is written down in ADR 0023)."""
    import divesafe.models.ollama  # noqa: F401  (registers the adapter)
    import divesafe.models.openai_compatible  # noqa: F401
    from divesafe.models.loopback import LoopbackHTTPHandler, LoopbackHTTPSHandler

    real = {name: factory for name, factory in llm_module._REGISTRY.items() if name != "fake"}
    assert {"ollama", "openai_compatible"} <= set(real)
    seen: dict[str, list[type]] = {}
    for name, factory in real.items():
        handlers: list[type] = []
        monkeypatch.setattr(
            "urllib.request.build_opener",
            lambda *hs, _h=handlers: (_h.extend(type(h) for h in hs), _Opener())[1],
        )
        provider = factory(
            _settings(
                llm_provider=name,
                llm_model="m",
                llm_base_url="http://127.0.0.1:1" if name == "ollama" else LOOPBACK_URL,
                llm_server_is_local=True,
            )
        )
        import asyncio

        from divesafe.models import ChatMessage, LLMRequest

        request = LLMRequest(messages=(ChatMessage(role="user", content="x"),))
        with pytest.raises(Exception):  # noqa: B017, PT011  (the opener is a stub that fails)
            asyncio.run(provider.complete(request))
        seen[name] = handlers
    for name, handlers in seen.items():
        assert LoopbackHTTPHandler in handlers and LoopbackHTTPSHandler in handlers, name


class _Opener:
    def open(self, *args: object, **kwargs: object) -> object:
        raise OSError("stub opener: nothing is sent")


def test_the_live_evaluation_setup_runs_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live module is skipped offline, so its setup is exercised here with the module
    settings patched. Nothing is sent: only the provider object is built."""
    import os

    from tests.evaluation import test_evaluation_live as live

    for name in [n for n in os.environ if n.startswith("DIVESAFE_EVAL_")]:
        monkeypatch.delenv(name)  # the developer's own evaluation settings must not matter
    monkeypatch.setattr(live, "MODEL", "m")
    monkeypatch.setattr(live, "PROVIDER", "openai_compatible")
    monkeypatch.setenv("DIVESAFE_EVAL_BASE_URL", LOOPBACK_URL)
    monkeypatch.setattr(live, "ALLOW_EXTERNAL", "false")
    monkeypatch.setattr(live, "SERVER_IS_LOCAL", "false")
    with pytest.raises(pytest.skip.Exception, match="loopback"):
        live._setup()

    def basis() -> str:
        try:
            return str(live._setup()[2])
        except pytest.skip.Exception as exc:  # a skip here would hide a failure as a skip
            raise AssertionError(f"the gate refused a declared server: {exc}") from None

    monkeypatch.setattr(live, "SERVER_IS_LOCAL", "TRUE")
    assert basis() == "declared_local"
    monkeypatch.setattr(live, "SERVER_IS_LOCAL", "false")
    monkeypatch.setattr(live, "ALLOW_EXTERNAL", "1")
    assert basis() == "loopback_external_opt_in"
    monkeypatch.setattr(live, "SERVER_IS_LOCAL", "true")
    with pytest.raises(ValidationError, match="contradict"):
        live._setup()


_PLACEHOLDER_KEY = "placeholder-not-a-real-key-7f3a9c"


def _live_tide_setup(
    monkeypatch: pytest.MonkeyPatch, out: Path, *, basis_flags: tuple[str, str], acknowledged: bool
) -> object:
    import os

    from tests.evaluation import test_evaluation_live as live

    for name in [n for n in os.environ if n.startswith("DIVESAFE_EVAL_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("DIVESAFE_EVAL_OUT", str(out))
    monkeypatch.setenv("DIVESAFE_EVAL_BASE_URL", LOOPBACK_URL)
    monkeypatch.setenv("DIVESAFE_EVAL_API_KEY", _PLACEHOLDER_KEY)
    monkeypatch.setattr(live, "MODEL", "m")
    monkeypatch.setattr(live, "PROVIDER", "openai_compatible")
    monkeypatch.setattr(live, "ALLOW_EXTERNAL", basis_flags[0])
    monkeypatch.setattr(live, "SERVER_IS_LOCAL", basis_flags[1])
    monkeypatch.setattr(live, "ACCEPT_PHN_TERMS_OPEN", acknowledged)
    monkeypatch.setattr(live, "_sha", lambda: "test-sha")  # no subprocess in a safety test
    return live


def test_the_live_tide_check_sends_nothing_without_the_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recorded PHN item must not reach a model that is not declared local unless the
    acknowledgement is set (PHN's terms are not on file, ADR 0021). The skip leaves a record, and
    it never replaces an earlier record."""
    import json

    live = _live_tide_setup(
        monkeypatch, tmp_path, basis_flags=("true", "false"), acknowledged=False
    )

    def must_not_be_called(_provider: object) -> None:
        raise AssertionError("the tide item was sent")

    monkeypatch.setattr(live, "run_tide_check", must_not_be_called)  # type: ignore[attr-defined]
    earlier = tmp_path / "tide_check_openai_compatible.json"
    earlier.write_text("an earlier raw record")
    test = live.test_a_real_model_given_a_tide_item_is_not_told_it_may_read_a_current  # type: ignore[attr-defined]
    with pytest.raises(pytest.skip.Exception, match="PHN"):
        test(tmp_path)
    assert earlier.read_text() == "an earlier raw record"
    (skip_file,) = [p for p in tmp_path.glob("tide_check_openai_compatible_*.json")]
    record = json.loads(skip_file.read_text())
    assert record["status"] == "skipped"
    assert record["egress_basis"] == "loopback_external_opt_in"
    assert record["host"] == "127.0.0.1"
    assert _PLACEHOLDER_KEY not in skip_file.read_text()


@pytest.mark.parametrize(
    ("flags", "acknowledged"),
    [(("false", "true"), False), (("true", "false"), True)],
)
def test_the_live_tide_check_runs_when_declared_local_or_acknowledged_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flags: tuple[str, str],
    acknowledged: bool,
) -> None:
    import json
    from types import SimpleNamespace

    live = _live_tide_setup(monkeypatch, tmp_path, basis_flags=flags, acknowledged=acknowledged)
    sent: list[object] = []

    def stub(provider: object) -> SimpleNamespace:
        sent.append(provider)  # the provider object only; nothing is sent by this stub
        record = SimpleNamespace(
            findings=("f",),
            scenarios=(),
            proposed_recommendation=None,
            model_dump_json=lambda: "{}",
        )
        return SimpleNamespace(hits=[], record=record, stayed_insufficient=True)

    monkeypatch.setattr(live, "run_tide_check", stub)  # type: ignore[attr-defined]
    live.test_a_real_model_given_a_tide_item_is_not_told_it_may_read_a_current(tmp_path)  # type: ignore[attr-defined]
    assert len(sent) == 1
    (written,) = list(tmp_path.glob("tide_check_openai_compatible*.json"))
    record = json.loads(written.read_text())
    assert record["status"] == "ran"
    assert record["phn_terms_acknowledged"] is acknowledged
    assert record["egress_basis"] == (
        "loopback_external_opt_in" if acknowledged else "declared_local"
    )
    assert _PLACEHOLDER_KEY not in written.read_text()


@pytest.mark.parametrize("extra", [{}, {"llm_provider": "fake"}])
def test_production_cannot_run_with_the_agent_stage_off(extra: dict[str, str]) -> None:
    """Owner decision (ADR 0023, 2026-10-18): production needs a real model server, so the agent
    stage cannot be off. `fake` is the default provider and is the only one that turns the stage
    off (`build_provider` returns None for it), so a production start-up that forgets to set a
    provider must fail, not run without agents. The match pins the cause to the provider: the
    fake-provider check runs before the API-key check."""
    with pytest.raises(ValidationError, match="fake LLM provider cannot be used in production"):
        Settings(environment="production", **extra)  # type: ignore[arg-type]


def test_the_fake_provider_still_turns_the_agent_stage_off_outside_production() -> None:
    """The other side of the same decision: development and test may run without agents, and
    that is the only way the stage is off. If this stopped being true the production refusal
    above would no longer be the whole story."""
    for environment in ("development", "test"):
        assert build_provider(Settings(environment=environment)) is None  # type: ignore[arg-type]
