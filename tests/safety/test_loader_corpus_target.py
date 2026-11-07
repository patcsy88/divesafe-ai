"""`DIVESAFE_CORPUS_TARGET` (ADR 0024 A3): required for `load` and `approve`, optional for
`withdraw`, and never echoed. Offline. Mutations run on COPIES of the module source."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from tests.safety.kb_fakes import mutated_module

from divesafe.config import loader_settings
from divesafe.config.loader_settings import (
    CORPUS_TARGET_KEY,
    LoaderConfigError,
    LoaderSettings,
    load_loader_settings,
)
from divesafe.services import kb_withdraw

pytestmark = pytest.mark.safety

URL = "postgresql://loader:Zq9-never-echoed@db:5432/divesafe_test?sslmode=require"
BASE = {"DIVESAFE_ENVIRONMENT": "test", "DIVESAFE_LOADER_DATABASE_URL": URL}
CAUGHT = (AssertionError, Exception, pytest.fail.Exception)


def settings(target: Path | None) -> LoaderSettings:
    return LoaderSettings(
        environment="test", loader_database_url=SecretStr(URL), corpus_target=target
    )


def check_required(module: Any) -> None:
    with pytest.raises(module.LoaderConfigError):
        module.LoaderSettings(
            environment="test", loader_database_url=SecretStr(URL)
        ).require_corpus_target()
    found = module.LoaderSettings(
        environment="test", loader_database_url=SecretStr(URL), corpus_target=Path("/corpus")
    ).require_corpus_target()
    assert found == Path("/corpus")


def test_the_key_name_is_fixed() -> None:
    assert CORPUS_TARGET_KEY == "DIVESAFE_CORPUS_TARGET"


def test_load_and_approve_need_the_corpus_target() -> None:
    check_required(loader_settings)


def test_the_environment_variable_is_read_into_the_setting(tmp_path: Path) -> None:
    loaded = load_loader_settings({**BASE, CORPUS_TARGET_KEY: str(tmp_path)})
    assert loaded.corpus_target == tmp_path
    assert loaded.require_corpus_target() == tmp_path


@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_or_empty_target_is_not_a_target(value: str | None) -> None:
    env = dict(BASE)
    if value is not None:
        env[CORPUS_TARGET_KEY] = value
    loaded = load_loader_settings(env)
    assert loaded.corpus_target is None
    with pytest.raises(LoaderConfigError):
        loaded.require_corpus_target()


def test_withdraw_works_without_a_corpus_target() -> None:
    loaded = load_loader_settings(BASE)  # no target: settings still load
    assert loaded.corpus_target is None
    source = inspect.getsource(kb_withdraw)
    assert "require_corpus_target" not in source
    assert "corpus_target" not in source


def test_the_refusal_message_does_not_contain_any_value() -> None:
    with pytest.raises(LoaderConfigError) as caught:
        settings(None).require_corpus_target()
    text = str(caught.value)
    assert CORPUS_TARGET_KEY in text  # the NAME of the setting is fine
    assert "Zq9" not in text and "never-echoed" not in text and "divesafe_test" not in text


def test_the_target_path_is_not_echoed_by_a_bad_configuration(tmp_path: Path) -> None:
    secret_path = tmp_path / "private-corpus-location"
    with pytest.raises(LoaderConfigError) as caught:
        load_loader_settings(
            {"DIVESAFE_ENVIRONMENT": "nonsense", CORPUS_TARGET_KEY: str(secret_path)}
        )
    assert "private-corpus-location" not in str(caught.value)


def test_mutation_a_missing_target_that_is_not_refused_is_caught() -> None:
    mutant = mutated_module(
        loader_settings,
        (
            "        if self.corpus_target is None:\n            raise LoaderConfigError(",
            "        if False:\n            raise LoaderConfigError(",
        ),
    )
    with pytest.raises(CAUGHT):
        check_required(mutant)
    check_required(loader_settings)


def test_mutation_a_default_target_is_caught() -> None:
    mutant = mutated_module(
        loader_settings,
        ("    corpus_target: Path | None = None", '    corpus_target: Path | None = Path("/")'),
    )
    with pytest.raises(CAUGHT):
        check_required(mutant)
