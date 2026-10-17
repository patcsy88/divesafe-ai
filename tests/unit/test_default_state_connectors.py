"""build_default_state wires the connectors the docs say it does, with the narrow getters."""

from __future__ import annotations

import pytest

from divesafe.api.state import build_default_state
from divesafe.config import Settings
from divesafe.data import (
    CachingRateLimitedGetter,
    MetApiMarineConnector,
    PhnEtideConnector,
    UrllibFormPostJsonGetter,
    UrllibJsonGetter,
)


def _state(monkeypatch: pytest.MonkeyPatch, *, token: str | None, phn: str | None):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DIVESAFE_MET_API_TOKEN", raising=False)
    monkeypatch.delenv("DIVESAFE_PHN_ETIDE_ENABLED", raising=False)
    if token is not None:
        monkeypatch.setenv("DIVESAFE_MET_API_TOKEN", token)
    if phn is not None:
        monkeypatch.setenv("DIVESAFE_PHN_ETIDE_ENABLED", phn)
    return build_default_state(Settings(_env_file=None))  # type: ignore[call-arg]


def test_the_phn_connector_is_off_unless_the_setting_turns_it_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(_env_file=None).phn_etide_enabled is False  # type: ignore[call-arg]
    for value in (None, "false", "0"):
        state = _state(monkeypatch, token=None, phn=value)
        assert not any(isinstance(c, PhnEtideConnector) for c in state.connectors)


def test_the_phn_connector_is_wired_with_its_own_post_getter_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(monkeypatch, token=None, phn="true")
    (phn,) = [c for c in state.connectors if isinstance(c, PhnEtideConnector)]
    getter = phn._getter  # noqa: SLF001
    assert isinstance(getter, CachingRateLimitedGetter)
    assert isinstance(getter._inner, UrllibFormPostJsonGetter)  # noqa: SLF001
    assert not any(isinstance(c, MetApiMarineConnector) for c in state.connectors)


def test_no_other_connector_shares_the_phn_getter(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(monkeypatch, token="tok-SECRET-123", phn="true")
    phn_getter = next(c for c in state.connectors if isinstance(c, PhnEtideConnector))._getter  # noqa: SLF001
    others = [c._getter for c in state.connectors if not isinstance(c, PhnEtideConnector)]  # type: ignore[attr-defined]  # noqa: SLF001
    assert all(g is not phn_getter for g in others)
    assert all(not isinstance(getattr(g, "_inner", None), UrllibFormPostJsonGetter) for g in others)
    assert any(isinstance(getattr(g, "_inner", None), UrllibJsonGetter) for g in others)
