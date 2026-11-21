"""build_default_state wires the connectors the docs say it does, with the narrow getters."""

from __future__ import annotations

import pytest

from divesafe.api.state import build_default_state
from divesafe.config import Settings
from divesafe.data import (
    CachingRateLimitedGetter,
    MetApiMarineConnector,
    TidesTodayConnector,
    UrllibJsonGetter,
)


def _state(monkeypatch: pytest.MonkeyPatch, *, token: str | None):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DIVESAFE_MET_API_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("DIVESAFE_MET_API_TOKEN", token)
    return build_default_state(Settings(_env_file=None))  # type: ignore[call-arg]


def test_the_met_connector_has_its_own_credentialed_getter(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(monkeypatch, token="tok-SECRET-123")
    (met,) = [c for c in state.connectors if isinstance(c, MetApiMarineConnector)]
    met_getter = met._getter  # noqa: SLF001
    assert isinstance(met_getter, CachingRateLimitedGetter)
    others = [c._getter for c in state.connectors if c is not met]  # type: ignore[attr-defined]  # noqa: SLF001
    assert all(g is not met_getter for g in others)
    assert all(isinstance(getattr(g, "_inner", None), UrllibJsonGetter) for g in others)


def test_the_tides_today_connector_has_its_own_keyed_getter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIVESAFE_TIDES_TODAY_ENABLED", "true")
    monkeypatch.setenv("DIVESAFE_TIDES_TODAY_API_KEY", "tt-SECRET-456")
    state = _state(monkeypatch, token="tok-SECRET-123")
    (tides,) = [c for c in state.connectors if isinstance(c, TidesTodayConnector)]
    tides_getter = tides._getter  # noqa: SLF001
    assert isinstance(tides_getter, CachingRateLimitedGetter)
    others = [c._getter for c in state.connectors if c is not tides]  # type: ignore[attr-defined]  # noqa: SLF001
    assert others
    assert all(g is not tides_getter for g in others)
    assert all(getattr(g, "_inner", None) is not tides_getter._inner for g in others)  # noqa: SLF001
    assert tides_getter._inner._auth_header == ("x-api-key", "tt-SECRET-456")  # noqa: SLF001
    # Only MET's getter carries a credential besides it, and never the tides.today key.
    headers = [getattr(getattr(g, "_inner", None), "_auth_header", None) for g in others]
    assert all(h is None or h[0] != "x-api-key" for h in headers)
    assert all(h is None or "tt-SECRET-456" not in h[1] for h in headers)


def test_a_leftover_ollama_variable_is_warned_about_by_name_count_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DIVESAFE_OLLAMA_MODEL", "SECRET-VALUE")
    with caplog.at_level("WARNING", logger="divesafe.api.state"):
        _state(monkeypatch, token=None)
    hits = [r for r in caplog.records if "Ollama adapter was removed" in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].count == 1  # type: ignore[attr-defined]
    from divesafe.config.logging import JsonFormatter

    line = JsonFormatter().format(hits[0])
    assert "SECRET-VALUE" not in line and "DIVESAFE_OLLAMA_MODEL" not in line


def test_no_ollama_warning_without_a_leftover_variable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for key in [k for k in __import__("os").environ if k.startswith("DIVESAFE_OLLAMA_")]:
        monkeypatch.delenv(key)
    with caplog.at_level("WARNING", logger="divesafe.api.state"):
        _state(monkeypatch, token=None)
    assert not [r for r in caplog.records if "Ollama" in r.getMessage()]
