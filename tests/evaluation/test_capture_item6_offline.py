"""ADR 0025 item 6: the capture refuses what it must refuse, and its dry run opens no connection.
Offline. The live capture is never run by a test."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from tests.evaluation import capture_item6 as cap

from divesafe.config import Settings

pytestmark = pytest.mark.safety

CONFIRM = {cap.CONFIRM_ENV: cap.CONFIRM_VALUE}


def _no_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a network call was attempted")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)


def _settings() -> Settings:
    # example.org is a placeholder, not the owner's host.
    return Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_model="claude-sonnet-5-5",
        llm_base_url="https://llm.example.org/v1",  # type: ignore[arg-type]
        llm_allowed_hosts="llm.example.org",
        allow_external_llm=True,
    )


def test_the_planned_run_is_the_item_6_plan() -> None:
    plan = cap.build_plan().describe()
    assert plan == {
        "site_id": "my-pahang-pulau-tioman",
        "planned_start_utc": "2026-10-30T03:00:00+00:00",
        "duration_minutes": 60,
        "provider": "openai_compatible",
        "model": "claude-sonnet-5-5",
    }


def test_the_plan_prints_no_host_url_or_key() -> None:
    text = repr(cap.build_plan().describe()).lower()
    assert "http" not in text and "example" not in text and "token" not in text


def test_refuses_without_the_live_flag(tmp_path: Path) -> None:
    with pytest.raises(cap.CaptureRefused, match="--live"):
        cap.require_live(False, {**CONFIRM, cap.DIR_ENV: str(tmp_path)})


def test_refuses_without_the_owner_confirmation(tmp_path: Path) -> None:
    for env in (
        {cap.DIR_ENV: str(tmp_path)},
        {cap.CONFIRM_ENV: "yes", cap.DIR_ENV: str(tmp_path)},
        {cap.CONFIRM_ENV: "ITEM6", cap.DIR_ENV: str(tmp_path)},
    ):
        with pytest.raises(cap.CaptureRefused, match="owner"):
            cap.require_live(True, env)


PROVIDER_OK = {
    "DIVESAFE_LLM_PROVIDER": cap.PROVIDER,
    "DIVESAFE_LLM_MODEL": cap.MODEL,
    "DIVESAFE_LLM_BASE_URL": "https://llm.example.org/v1",
    "DIVESAFE_ALLOW_EXTERNAL_LLM": "true",
}


def test_refuses_without_an_output_directory() -> None:
    with pytest.raises(cap.CaptureRefused, match=cap.DIR_ENV):
        cap.require_live(True, {**CONFIRM, **PROVIDER_OK})


def test_refuses_an_output_directory_inside_the_repository() -> None:
    inside = cap.REPO / "tests" / "fixtures" / "item6_capture"
    with pytest.raises(cap.CaptureRefused, match="inside the repository"):
        cap.require_live(True, {**CONFIRM, **PROVIDER_OK, cap.DIR_ENV: str(inside)})
    with pytest.raises(cap.CaptureRefused, match="inside the repository"):
        cap.check_output_dir(cap.REPO)


def test_a_directory_outside_the_repository_is_accepted(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    outside = tmp_path / "capture"
    assert cap.check_output_dir(outside, repo=fake_repo) == outside.resolve()


def test_a_symlink_into_the_repository_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(cap.REPO / "tests")
    with pytest.raises(cap.CaptureRefused, match="inside the repository"):
        cap.check_output_dir(link)


def test_the_dry_run_makes_no_network_call(no_network: None, tmp_path: Path) -> None:
    report = cap.run_dry(_settings())
    assert report["mode"] == "dry_run"
    assert report["egress_basis"] == "remote_external_opt_in"
    rendered = repr(report)
    assert "llm.example.org" not in rendered and "https://" not in rendered


def test_the_dry_run_entry_point_makes_no_network_call(
    no_network: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(cap.CONFIRM_ENV, raising=False)
    # No LLM settings in the environment: the dry run refuses with a fixed message, offline.
    assert cap.main([]) == 2
    assert "DIVESAFE_LLM_BASE_URL" in capsys.readouterr().err


def test_a_live_entry_point_without_confirmation_refuses(
    no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(cap.CONFIRM_ENV, raising=False)
    assert cap.main(["--live"]) == 2


def test_forbidden_keys_refuse_the_whole_capture_and_write_nothing(tmp_path: Path) -> None:
    payload = {
        "reply": "ok",
        "system_fingerprint": "fp_1",
        "created": 1,
        "meta": {"url": "https://x.example.org", "headers": {"Authorization": "Bearer t"}},
    }
    out = tmp_path / "out"
    with pytest.raises(cap.CaptureRefused, match="scrub check fired"):
        cap.write_capture(out, "reply.json", payload)
    assert not out.exists() or list(out.iterdir()) == []


def test_a_capture_never_replaces_an_earlier_file(tmp_path: Path) -> None:
    cap.write_capture(tmp_path, "reply.json", {"reply": "one"})
    with pytest.raises(cap.CaptureRefused, match="already in use"):
        cap.write_capture(tmp_path, "reply.json", {"reply": "two"})


# ---- S2 write-time screen on configured values (group C item 2) -------------------------------

_S2_BASE = "https://llm-gw.example.org:8317/v1"
_S2_KEY = "ZQx7-fake-key-value-99"
_S2_MET = "met-fake-token-00421"


@pytest.mark.parametrize(
    ("category", "value"),
    [
        ("base_url", _S2_BASE),
        ("host", "llm-gw.example.org"),
        ("port", "8317"),
        ("api_key", _S2_KEY),
        ("met_token", _S2_MET),
    ],
)
def test_a_configured_value_in_the_text_refuses_the_whole_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, category: str, value: str
) -> None:
    env = {
        "DIVESAFE_LLM_BASE_URL": _S2_BASE,
        "DIVESAFE_LLM_API_KEY": _S2_KEY,
        "DIVESAFE_MET_API_TOKEN": _S2_MET,
    }
    for key in env:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cap.os, "environ", env)
    out = tmp_path / "out"
    payload = {"replies": {"specialist:weather": f"the value {value} appeared"}}
    with pytest.raises(cap.CaptureRefused) as excinfo:
        cap.write_capture(out, "item6_capture.json", payload)
    # Only the category name is reported, never the value.
    message = str(excinfo.value)
    assert f"configured:{category}" in message
    assert value not in message
    assert not (out / "item6_capture.json").exists()


def test_a_value_that_matches_no_configured_category_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cap.os,
        "environ",
        {
            "DIVESAFE_LLM_BASE_URL": _S2_BASE,
            "DIVESAFE_LLM_API_KEY": _S2_KEY,
            "DIVESAFE_MET_API_TOKEN": _S2_MET,
        },
    )
    payload = {"replies": {"specialist:weather": "wind 12 knots, visibility 8 m"}}
    path = cap.write_capture(tmp_path / "out", "item6_capture.json", payload)
    assert "wind 12 knots" in path.read_text(encoding="utf-8")


def test_a_short_configured_port_does_not_refuse_every_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A port of 80 would match digits everywhere. Values shorter than four characters are skipped.
    monkeypatch.setattr(cap.os, "environ", {"DIVESAFE_LLM_BASE_URL": "https://h.example.org:80/v1"})
    payload = {"replies": {"x": "a clear day with 80 people"}}
    cap.write_capture(tmp_path / "out", "item6_capture.json", payload)


# ---- F5: unexpected errors print the class name only (group C item 4) -------------------------


def test_an_unexpected_exception_prints_only_its_class_name_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class SecretBearingError(Exception):
        pass

    def boom(_environ: object) -> None:
        raise SecretBearingError("https://llm-gw.example.org:8317 key=sk-ABCDEFGH12345678")

    monkeypatch.delenv(cap.CONFIRM_ENV, raising=False)
    monkeypatch.setattr(cap, "run_live", boom)
    code = cap.main(["--live"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.strip() == "capture stopped: SecretBearingError"
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    assert "llm-gw.example.org" not in combined and "sk-ABCDEFGH" not in combined


def test_the_egress_skip_path_returns_a_fixed_message_and_no_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.evaluation.egress_gate import egress_gate

    from divesafe.models.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        "m",
        "https://llm-gw.example.org:8317/v1",
        allowed_hosts=frozenset({"llm-gw.example.org"}),
        api_key=None,
    )
    with pytest.raises(pytest.skip.Exception) as excinfo:
        egress_gate(provider, allow_external=False, server_is_local=False)
    message = str(excinfo.value)
    assert message == "egress refused: the LLM server is not declared (see ADR 0023)"
    assert "llm-gw.example.org" not in message and "8317" not in message


# ---- F7: samefile ancestors and an existing file (group C item 5) ---------------------------


def test_a_path_differing_only_by_case_inside_the_repo_is_refused() -> None:
    # On a case-insensitive volume the upper-cased name is the same directory as the repository.
    upper = cap.REPO.parent / cap.REPO.name.upper() / "tests" / "fixtures" / "capture"
    if not os.path.samefile(cap.REPO, cap.REPO.parent / cap.REPO.name.upper()):
        pytest.skip("this volume is case-sensitive; the case-only check has nothing to test")
    assert str(upper) != str(cap.REPO / "tests" / "fixtures" / "capture")
    with pytest.raises(cap.CaptureRefused, match="inside the repository"):
        cap.check_output_dir(upper)


def test_an_existing_file_as_the_output_directory_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-dir"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(cap.CaptureRefused, match="existing file"):
        cap.check_output_dir(plain, repo=tmp_path / "repo")


# ---- S2 screen from the run's Settings, and case-insensitive host (review should-fixes) -------

_CASE_BASE = "https://llm-gw.example.org:8317/v1"
_CASE_KEY = "ZQx7-case-key-55"
_CASE_MET = "met-case-token-77"


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.lower().startswith("divesafe_"):
            monkeypatch.delenv(name, raising=False)


def test_a_value_supplied_through_settings_is_refused_in_the_written_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The screened key arrives through the Settings object the run uses, with no environment
    # variable at all. It must be refused, and the refusal must not print the value.
    _clear_llm_env(monkeypatch)
    settings = Settings(_env_file=None, llm_api_key=_CASE_KEY)  # type: ignore[call-arg]
    out = tmp_path / "out"
    payload = {"replies": {"specialist:weather": f"the key {_CASE_KEY} appeared"}}
    with pytest.raises(cap.CaptureRefused, match="configured:api_key") as info:
        cap.write_capture(out, "item6_capture.json", payload, settings)
    assert _CASE_KEY not in str(info.value)
    assert not out.exists() or list(out.iterdir()) == []


def test_a_lowercase_environment_variable_cannot_leave_a_value_unscreened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Settings reads DIVESAFE_* case-insensitively, so a lowercase variable DOES configure the run.
    # The screen must see that value too, not only exact-case names.
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("divesafe_llm_api_key", _CASE_KEY)
    settings = Settings(_env_file=None)
    assert settings.llm_api_key is not None  # the run really uses the lowercase value
    payload = {"replies": {"x": f"leaked {_CASE_KEY}"}}
    with pytest.raises(cap.CaptureRefused, match="configured:api_key"):
        cap.write_capture(tmp_path / "out", "item6_capture.json", payload, settings)


def test_an_upper_case_host_is_refused_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The configured host is lowercase; the text carries it in upper case. It must be refused.
    _clear_llm_env(monkeypatch)
    settings = Settings(_env_file=None, llm_base_url=_CASE_BASE)  # type: ignore[arg-type]
    out = tmp_path / "out"
    payload = {"replies": {"x": "sent to LLM-GW.EXAMPLE.ORG and back"}}
    with pytest.raises(cap.CaptureRefused, match="configured:host") as info:
        cap.write_capture(out, "item6_capture.json", payload, settings)
    assert "llm-gw.example.org" not in str(info.value).lower()
    assert not out.exists() or list(out.iterdir()) == []


def test_the_refusal_message_names_only_the_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_llm_env(monkeypatch)
    settings = Settings(_env_file=None, llm_base_url=_CASE_BASE)  # type: ignore[arg-type]
    payload = {"replies": {"x": "LLM-GW.EXAMPLE.ORG"}}
    with pytest.raises(cap.CaptureRefused) as info:
        cap.write_capture(tmp_path / "out", "item6_capture.json", payload, settings)
    message = str(info.value)
    assert "configured:host" in message
    assert "LLM-GW" not in message and "example.org" not in message.lower()


# ---- egress refusal is a CaptureRefused, not a traceback; a lowercase env var is re-checked
# (security review should-fixes 1 and 2) -----------------------------------------------------------


def test_an_undeclared_loopback_dry_run_refuses_cleanly_and_connects_to_nothing(
    no_network: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A loopback server, no declaration and no opt-in: the egress gate refuses. The refusal must
    # reach main as CaptureRefused (exit 2, fixed line), not as a skip or a traceback.
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DIVESAFE_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("DIVESAFE_LLM_MODEL", cap.MODEL)
    monkeypatch.setenv("DIVESAFE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    try:
        code = cap.main([])
    except pytest.skip.Exception:
        pytest.fail("the egress refusal escaped main as a skip, not a CaptureRefused")
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("refused: the egress check stopped (")
    assert "Traceback" not in captured.err
    assert "127.0.0.1" not in captured.err and "11434" not in captured.err


def test_a_lowercase_model_variable_that_changes_the_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exact-case name is the owner's model; a lowercase variant sets a different one, and
    # Settings uses the lowercase value. The run must refuse before any state is built.
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("DIVESAFE_LLM_PROVIDER", cap.PROVIDER)
    monkeypatch.setenv("DIVESAFE_LLM_MODEL", cap.MODEL)
    monkeypatch.setenv("DIVESAFE_LLM_BASE_URL", "https://llm.example.org/v1")
    monkeypatch.setenv("DIVESAFE_ALLOW_EXTERNAL_LLM", "true")
    monkeypatch.setenv("divesafe_llm_model", "other-model")
    assert Settings(_env_file=None).llm_model == "other-model"  # the run really uses the variant
    monkeypatch.setattr(cap, "build_live_state", lambda _s: pytest.fail("state built"))
    env = {**LIVE_ENV_FOR_RECHECK, cap.DIR_ENV: str(tmp_path / "out")}
    with pytest.raises(cap.CaptureRefused, match="case variant of a guarded") as info:
        cap.run_live(env)
    assert "other-model" not in str(info.value)
    assert "divesafe_llm_model" not in str(info.value).lower()
    assert not (tmp_path / "out").exists()


LIVE_ENV_FOR_RECHECK = {
    cap.CONFIRM_ENV: cap.CONFIRM_VALUE,
    "DIVESAFE_LLM_PROVIDER": cap.PROVIDER,
    "DIVESAFE_LLM_MODEL": cap.MODEL,
    "DIVESAFE_LLM_BASE_URL": "https://llm.example.org/v1",
    "DIVESAFE_ALLOW_EXTERNAL_LLM": "true",
}
