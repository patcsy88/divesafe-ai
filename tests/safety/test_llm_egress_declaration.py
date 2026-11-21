"""A loopback address does not show where a model server sends the prompt (ADR 0023).

A local proxy on localhost can forward every prompt to a hosted model, so a loopback server needs
the operator's declaration (or the external opt-in) before any plan or evidence data reaches it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.safety.kb_fakes import mutated_module
from tests.safety.log_fields import scanned_text

from divesafe.api import state as state_module
from divesafe.api.state import build_provider
from divesafe.config import Settings
from divesafe.models import FakeProvider
from divesafe.models import llm as llm_module
from divesafe.models.egress import check_egress
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
    provider, basis = build_provider(_settings(llm_provider="openai", llm_server_is_local=True))
    assert provider is not None and basis == "declared_local"
    provider, basis = build_provider(_settings(llm_provider="openai", allow_external_llm=True))
    assert provider is not None and basis == "loopback_external_opt_in"


def test_the_declaration_cannot_cover_a_server_that_is_not_on_this_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_stub(monkeypatch, external=True)
    with pytest.raises(RuntimeError, match="not on this machine"):
        build_provider(_settings(llm_provider="openai", llm_server_is_local=True))
    with pytest.raises(RuntimeError, match="off-machine"):
        build_provider(_settings(llm_provider="openai"))
    provider, basis = build_provider(_settings(llm_provider="openai", allow_external_llm=True))
    assert provider is not None and basis == "remote_external_opt_in"


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


def test_the_real_adapter_on_loopback_needs_the_declaration() -> None:
    provider = OpenAICompatibleProvider("m", LOOPBACK_URL)
    assert provider.external is False
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
    # A model host is required since the owner decision of 2026-11-12 (ADR 0022 Known gap 12).
    assert build_default_state(
        _settings(llm_provider="openai", llm_server_is_local=True, llm_base_url=LOOPBACK_URL)
    )


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


_URL_PARTS = ("pw-in-url-31", "secret-path", "token=q-9", "8443")


def _start_up_log_leaks(
    module: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> list[str]:
    """Runs `module.build_provider` with a URL holding a password, port, path and query, checks
    the logged host, and returns the URL parts found in the start-up log record."""
    _with_stub(monkeypatch, external=True)
    url = "https://user:pw-in-url-31@wall-e.ch30ng.org:8443/v1/secret-path?token=q-9"
    caplog.clear()
    with caplog.at_level("WARNING"):
        module.build_provider(
            _settings(llm_provider="openai", allow_external_llm=True, llm_base_url=url)
        )
    record = next(r for r in caplog.records if r.getMessage() == "agent stage is ON")
    assert record.model_host == "wall-e.ch30ng.org"
    logged = scanned_text(record)
    return [text for text in _URL_PARTS if text in logged]


def test_the_start_up_log_names_the_model_host_and_nothing_else_of_the_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The host tells two servers apart in the logs. The path, port, credentials and query of the
    URL are not logged."""
    assert _start_up_log_leaks(state_module, monkeypatch, caplog) == []


_HOST_EXTRA = '            "model_host": model_host(settings),\n            "address_is_remote"'


@pytest.mark.parametrize(
    ("added", "found"),
    [
        ('"port": urlsplit(str(settings.llm_base_url)).port,', ["8443"]),
        ('"base_url": str(settings.llm_base_url),', list(_URL_PARTS)),
    ],
)
def test_the_start_up_log_scan_catches_a_logged_port_or_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    added: str,
    found: list[str],
) -> None:
    """Mutation check: a copy of the code that also logs the port, or the whole URL, as an extra
    field is caught by the same scan (the real module is not changed)."""
    mutant = mutated_module(
        state_module,
        (_HOST_EXTRA, _HOST_EXTRA.replace('"model_host"', f'{added}\n            "model_host"')),
    )
    assert _start_up_log_leaks(mutant, monkeypatch, caplog) == found


# Literal knowledge-basis settings, real adapter (ADR 0028).
_KNOWLEDGE_SETTINGS: dict[str, object] = {
    "llm_provider": "openai_compatible",
    "llm_model": "m",
    "llm_base_url": "https://llm.example.org/v1",
    "llm_allowed_hosts": "llm.example.org",
    "allow_external_llm": True,
    "llm_knowledge_host": "llm.example.org",
}


# ADR 0029 decision 12: the same warning for the tides.today item, under every basis not local.
_TIDES_ON: dict[str, object] = {
    "tides_today_enabled": True,
    "tides_today_api_key": "key-2",
}
_TIDES_WARNING = "tides.today data is ON and the model server is not declared local"


def _tides_warned(caplog: pytest.LogCaptureFixture, build: object, settings: Settings) -> bool:
    caplog.clear()
    with caplog.at_level("WARNING"):
        provider, _ = build(settings)  # type: ignore[operator]
    provider.close()
    return any(_TIDES_WARNING in r.getMessage() for r in caplog.records)


def _tides_cases(build: object, caplog: pytest.LogCaptureFixture) -> list[str]:
    bad: list[str] = []
    for label, flags in (
        ("external", {**_KNOWLEDGE_SETTINGS, "llm_knowledge_host": None}),
        ("knowledge", _KNOWLEDGE_SETTINGS),
    ):
        if not _tides_warned(caplog, build, _settings(**flags, **_TIDES_ON)):
            bad.append(f"{label}: not warned when on")
        if _tides_warned(caplog, build, _settings(**flags)):
            bad.append(f"{label}: warned when off")
    local = _settings(
        llm_provider="openai_compatible",
        llm_model="m",
        llm_base_url=LOOPBACK_URL,
        llm_server_is_local=True,
        **_TIDES_ON,
    )
    if _tides_warned(caplog, build, local):
        bad.append("declared local: warned")
    return bad


def test_tides_today_data_with_a_model_server_not_declared_local_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _tides_cases(build_provider, caplog) == []


@pytest.mark.parametrize(
    "edit",
    [
        (
            'if settings.tides_today_enabled and basis != "declared_local":',
            'if settings.tides_today_enabled and basis == "remote_external_opt_in":',
        ),
        ('if settings.tides_today_enabled and basis != "declared_local":', "if False:"),
        ('if settings.tides_today_enabled and basis != "declared_local":', "if True:"),
    ],
    ids=["knowledge-basis-skipped", "never-warned", "warned-when-off"],
)
def test_mutations_of_the_tides_today_warning_are_caught(
    caplog: pytest.LogCaptureFixture, edit: tuple[str, str]
) -> None:
    from tests.safety.kb_fakes import mutated_module

    from divesafe.api import state as state_module

    mutant = mutated_module(state_module, edit)
    assert _tides_cases(mutant.build_provider, caplog) != []


_CALLERS = {
    # name -> the only files that may mention it at all (call, index, pass as a value, iterate)
    "create_provider": {"models/llm.py", "models/__init__.py", "api/state.py"},
    "OpenAICompatibleProvider": {"models/openai_compatible.py"},
    "_factory": {"models/openai_compatible.py"},
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
        "OpenAICompatibleProvider('m', 'u')",
        "openai_compatible._factory(s)",
        "llm._REGISTRY['openai_compatible'](s)",
        "llm._REGISTRY.get('openai_compatible')(s)",
        "for f in llm._REGISTRY.values(): f(s)",
        "loop.run_in_executor(None, create_provider, s)",
        "list(map(OpenAICompatibleProvider, xs))",
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
    import divesafe.models.openai_compatible  # noqa: F401
    from divesafe.models.loopback import LoopbackHTTPHandler, LoopbackHTTPSHandler

    real = {name: factory for name, factory in llm_module._REGISTRY.items() if name != "fake"}
    assert {"openai_compatible"} <= set(real)
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
                llm_base_url=LOOPBACK_URL,
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
    with pytest.raises(
        pytest.skip.Exception, match="egress refused: the LLM server is not declared"
    ):
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

    # ADR 0028: the evaluation's knowledge host gives the new basis only when it is the base URL's.
    monkeypatch.setattr(live, "SERVER_IS_LOCAL", "false")
    monkeypatch.setenv("DIVESAFE_EVAL_BASE_URL", "https://llm.example.org/v1")
    monkeypatch.setenv("DIVESAFE_EVAL_ALLOWED_HOSTS", "llm.example.org,other.example.org")
    assert basis() == "remote_external_opt_in"
    monkeypatch.setenv("DIVESAFE_EVAL_KNOWLEDGE_HOST", "llm.example.org")
    assert basis() == "remote_knowledge_opt_in"
    monkeypatch.setenv("DIVESAFE_EVAL_KNOWLEDGE_HOST", "other.example.org")
    with pytest.raises(ValidationError, match="must be the host of DIVESAFE_LLM_BASE_URL"):
        live._setup()  # a mismatch fails the run, never a skip


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
        assert build_provider(Settings(environment=environment)) == (None, "no_agent_stage")  # type: ignore[arg-type]


def test_check_egress_refuses_both_flags_even_when_settings_validation_was_skipped() -> None:
    """`model_construct` and `model_copy` skip the `Settings` validators, so the gate checks the
    pair itself: both flags set must never read as `declared_local`."""
    both = Settings.model_construct(llm_server_is_local=True, allow_external_llm=True)
    for external in (False, True):
        with pytest.raises(RuntimeError, match="contradict"):
            check_egress(_Stub(external), both)
    with pytest.raises(RuntimeError, match="contradict"):
        check_egress(OpenAICompatibleProvider("m", LOOPBACK_URL), both)
    copied = _settings(llm_server_is_local=True).model_copy(update={"allow_external_llm": True})
    with pytest.raises(RuntimeError, match="contradict"):
        check_egress(_Stub(False), copied)


def test_a_remote_host_must_be_listed_even_with_the_external_opt_in() -> None:
    """The allow-list is the last guard against a mistyped https host receiving the prompt."""
    url = "https://llm.example.org/v1"
    with pytest.raises(ValueError, match="DIVESAFE_LLM_ALLOWED_HOSTS"):
        OpenAICompatibleProvider("m", url)
    with pytest.raises(ValueError, match="DIVESAFE_LLM_ALLOWED_HOSTS"):
        OpenAICompatibleProvider("m", url, allowed_hosts=frozenset({"other.example.org"}))
    with pytest.raises(ValueError, match="DIVESAFE_LLM_ALLOWED_HOSTS"):
        build_provider(
            _settings(
                llm_provider="openai_compatible",
                llm_model="m",
                llm_base_url=url,
                allow_external_llm=True,
            )
        )
    with pytest.raises(ValueError, match="must use https"):
        OpenAICompatibleProvider(
            "m", "http://llm.example.org/v1", allowed_hosts=frozenset({"llm.example.org"})
        )
    built, basis = build_provider(
        _settings(
            llm_provider="openai_compatible",
            llm_model="m",
            llm_base_url=url,
            llm_allowed_hosts="llm.example.org",
            allow_external_llm=True,
        )
    )
    assert built is not None and basis == "remote_external_opt_in"
    listed = OpenAICompatibleProvider("m", url, allowed_hosts=frozenset({"llm.example.org"}))
    check_egress(listed, _settings(allow_external_llm=True))
