"""A loopback address does not show where a model server sends the prompt (ADR 0023).

A local proxy on localhost can forward every prompt to a hosted model, so a loopback server needs
the operator's declaration (or the external opt-in) before any plan or evidence data reaches it.
"""

from __future__ import annotations

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


def test_only_the_start_up_path_builds_a_real_provider() -> None:
    """`create_provider` and the adapter constructors do not check egress, so no other runtime
    code may call them: a new caller must go through `build_provider` or call `check_egress`."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "divesafe"
    allowed = {
        "create_provider": {"models/llm.py", "api/state.py"},
        "OllamaProvider": {"models/ollama.py"},
        "OpenAICompatibleProvider": {"models/openai_compatible.py"},
    }
    found: dict[str, set[str]] = {name: set() for name in allowed}
    for path in src.rglob("*.py"):
        rel = path.relative_to(src).as_posix()
        text = path.read_text()
        for name in allowed:
            if re.search(rf"(?<![\w.])(?:\w+\.)*{name}\(", text) and rel not in allowed[name]:
                found[name].add(rel)
    assert found == {name: set() for name in allowed}
    assert "create_provider(settings)" in (src / "api" / "state.py").read_text()


def test_the_live_evaluation_setup_runs_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live module is skipped offline, so its setup is exercised here with the module
    settings patched. Nothing is sent: only the provider object is built."""
    from tests.evaluation import test_evaluation_live as live

    monkeypatch.setattr(live, "MODEL", "m")
    monkeypatch.setattr(live, "PROVIDER", "openai_compatible")
    monkeypatch.setenv("DIVESAFE_EVAL_BASE_URL", LOOPBACK_URL)
    monkeypatch.setattr(live, "ALLOW_EXTERNAL", "false")
    monkeypatch.setattr(live, "SERVER_IS_LOCAL", "false")
    with pytest.raises(pytest.skip.Exception):
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
