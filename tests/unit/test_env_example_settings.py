"""Every setting `.env.example` and `docker/production.env.example` name is a real `Settings`
field (or, for the production file, a listed compose-only name). `Settings` uses extra="ignore",
so a stale name would be dropped silently, and a removed one may stop start-up (ADR 0021
WITHDRAWN: `DIVESAFE_PHN_ETIDE_ENABLED`)."""

from __future__ import annotations

import re
from pathlib import Path

from divesafe.config import Settings

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
# Commented-out lines count on purpose: a stale name there would be uncommented later.
NAME = re.compile(r"^#?[ \t]*(DIVESAFE_[A-Z0-9_]+)=", re.MULTILINE)


def _names(text: str) -> set[str]:
    return set(NAME.findall(text))


def test_every_setting_in_the_env_example_is_a_settings_field() -> None:
    names = _names(EXAMPLE.read_text(encoding="utf-8"))
    assert names
    fields = {f"DIVESAFE_{name.upper()}" for name in Settings.model_fields}
    assert sorted(names - fields) == []


def test_a_removed_setting_in_the_example_would_be_caught() -> None:
    text = EXAMPLE.read_text(encoding="utf-8") + "DIVESAFE_PHN_ETIDE_ENABLED=false\n"
    fields = {f"DIVESAFE_{name.upper()}" for name in Settings.model_fields}
    assert _names(text) - fields == {"DIVESAFE_PHN_ETIDE_ENABLED"}


PRODUCTION_EXAMPLE = EXAMPLE.parent / "docker" / "production.env.example"
COMPOSE = EXAMPLE.parent / "docker" / "docker-compose.production.yml"
# Read by Docker Compose (bind-mount sources), never by `Settings`.
COMPOSE_ONLY = frozenset({"DIVESAFE_CORPUS_DIR", "DIVESAFE_RULESET_FILE"})


def test_every_setting_in_the_production_env_example_is_a_settings_field_or_compose_only() -> None:
    names = _names(PRODUCTION_EXAMPLE.read_text(encoding="utf-8"))
    assert names
    fields = {f"DIVESAFE_{name.upper()}" for name in Settings.model_fields}
    assert sorted(names - fields - COMPOSE_ONLY) == []
    compose = COMPOSE.read_text(encoding="utf-8")
    for name in COMPOSE_ONLY:
        assert name not in fields
        assert f"${{{name}:?" in compose, name


def test_a_removed_setting_in_the_production_example_would_be_caught() -> None:
    text = PRODUCTION_EXAMPLE.read_text(encoding="utf-8") + "DIVESAFE_PHN_ETIDE_ENABLED=false\n"
    fields = {f"DIVESAFE_{name.upper()}" for name in Settings.model_fields}
    assert _names(text) - fields - COMPOSE_ONLY == {"DIVESAFE_PHN_ETIDE_ENABLED"}
