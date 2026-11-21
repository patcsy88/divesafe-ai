"""PHN e-Tide was removed (ADR 0021 WITHDRAWN, 2026-11-21). Its switch stops start-up whatever its
value, its host is on no getter's list, and its source name stays a refused, screened prefix."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.api import state as state_module
from divesafe.config import Settings
from divesafe.data import http as http_module
from divesafe.data.removed_sources import (
    PHN_ETIDE_HOSTS,
    PHN_ETIDE_REFUSAL,
    PHN_ETIDE_SETTING,
    PHN_ETIDE_SOURCE,
)
from divesafe.services import kb_screen

pytestmark = pytest.mark.safety

SRC = Path(__file__).resolve().parents[2] / "src" / "divesafe"
CALL = "    refuse_removed_settings(os.environ)\n"


def _refused(module: object, monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> bool:
    for key in ("DIVESAFE_MET_API_TOKEN", "DIVESAFE_TIDES_TODAY_ENABLED", "DIVESAFE_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(name, value)
    try:
        module.build_default_state(Settings(_env_file=None))  # type: ignore[attr-defined,call-arg]
    except RuntimeError as exc:
        assert str(exc) == PHN_ETIDE_REFUSAL
        return True
    finally:
        monkeypatch.delenv(name)
    return False


@pytest.mark.parametrize("value", ["true", "false", "0", "", "yes"])
@pytest.mark.parametrize("name", [PHN_ETIDE_SETTING, PHN_ETIDE_SETTING.lower()])
def test_the_removed_switch_stops_start_up_whatever_its_value(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    assert _refused(state_module, monkeypatch, name, value)


def test_without_the_switch_start_up_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PHN_ETIDE_SETTING, raising=False)
    for key in ("DIVESAFE_MET_API_TOKEN", "DIVESAFE_TIDES_TODAY_ENABLED", "DIVESAFE_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    assert state_module.build_default_state(Settings(_env_file=None)) is not None  # type: ignore[call-arg]


def test_the_refusal_is_the_first_thing_start_up_does() -> None:
    import inspect

    body = inspect.getsource(state_module.build_default_state).split("\n", 1)[1]
    assert body.startswith(CALL)


@pytest.mark.parametrize(
    "edits",
    [
        pytest.param([(CALL, "")], id="call-removed"),
        pytest.param(
            [("k.upper() == PHN_ETIDE_SETTING", "k == PHN_ETIDE_SETTING")], id="case-sensitive"
        ),
    ],
)
def test_every_mutation_of_the_refusal_is_caught(
    monkeypatch: pytest.MonkeyPatch, edits: list[tuple[str, str]]
) -> None:
    mutant = mutated_module(state_module, *edits)
    assert not all(
        _refused(mutant, monkeypatch, name, "false")
        for name in (PHN_ETIDE_SETTING, PHN_ETIDE_SETTING.lower())
    )


def test_the_setting_the_connector_and_its_getter_are_gone() -> None:
    assert "phn_etide_enabled" not in Settings.model_fields
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("divesafe.data.phn_etide")
    assert not hasattr(http_module, "UrllibFormPostJsonGetter")


def test_the_phn_host_is_on_no_getter_list_and_no_getter_accepts_it() -> None:
    for hosts in (http_module.ALLOWED_HOSTS, *http_module.CREDENTIAL_HOST_LISTS):
        assert not hosts & PHN_ETIDE_HOSTS
    for hosts in (PHN_ETIDE_HOSTS, PHN_ETIDE_HOSTS | http_module.ALLOWED_HOSTS):
        with pytest.raises(ValueError, match="PHN e-Tide host was removed"):
            http_module.UrllibJsonGetter(allowed_hosts=hosts)
        for header in (("x-api-key", "k"), ("Authorization", "METToken k")):
            with pytest.raises(ValueError):
                http_module.UrllibJsonGetter(allowed_hosts=hosts, auth_header=header)


def test_removing_the_getters_phn_refusal_is_caught() -> None:
    mutant = mutated_module(http_module, ("if hosts & PHN_ETIDE_HOSTS:", "if False:"))
    assert mutant.UrllibJsonGetter(allowed_hosts=PHN_ETIDE_HOSTS | mutant.ALLOWED_HOSTS)


def test_only_the_removed_sources_module_names_the_phn_host() -> None:
    (host,) = PHN_ETIDE_HOSTS
    named = sorted(
        p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py") if host in p.read_text()
    )
    assert named == ["data/removed_sources.py"]


def test_the_source_name_stays_a_screened_prefix_and_a_refused_evaluation_source() -> None:
    from tests.evaluation import langfuse_checks

    assert PHN_ETIDE_SOURCE == "phn-etide-prediction"
    assert PHN_ETIDE_SOURCE in kb_screen.CONNECTOR_PREFIXES
    assert PHN_ETIDE_SOURCE in langfuse_checks.REFUSED_SOURCES
