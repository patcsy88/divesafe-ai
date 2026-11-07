"""The knowledge loader's narrow settings and the API's refusal of the loader credential
(ADR 0024 A3/A4). Offline: temporary directories and explicit environment mappings only."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from divesafe.api.state import refuse_loader_credentials
from divesafe.config import loader_settings
from divesafe.config.loader_settings import (
    LoaderConfigError,
    load_loader_settings,
)
from divesafe.config.secret_files import SECRET_FILES, secret_file_name

pytestmark = pytest.mark.safety

SECRET_URL = "postgresql://loader:Zq9-never-echoed@db:5432/divesafe?sslmode=require"
FILE = secret_file_name("loader_database_url")


@pytest.fixture(autouse=True)
def _no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # so a stray ./.env in the repository never matters


def secrets_dir(tmp_path: Path, url: str = SECRET_URL, *extra: str) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / FILE).write_text(url + "\n")
    for name in extra:
        (directory / name).write_text("x\n")
    return directory


def production(directory: Path) -> dict[str, str]:
    return {"DIVESAFE_ENVIRONMENT": "production", "DIVESAFE_SECRETS_DIR": str(directory)}


def refused(env: dict[str, str]) -> str:
    with pytest.raises(LoaderConfigError) as caught:
        load_loader_settings(env)
    return str(caught.value)


# --- what it reads ---------------------------------------------------------------------------


def test_the_loader_file_is_one_of_the_known_secret_names() -> None:
    assert "loader_database_url" in SECRET_FILES and SECRET_FILES["loader_database_url"] is False


def test_production_reads_the_loader_file_and_nothing_else(tmp_path: Path) -> None:
    settings = load_loader_settings(production(secrets_dir(tmp_path)))
    assert settings.production and settings.loader_database_url.get_secret_value() == SECRET_URL
    assert settings.corpus_target is None
    assert "Zq9" not in repr(settings) and "Zq9" not in str(settings)


def test_the_model_has_only_the_listed_fields() -> None:
    assert set(loader_settings.LoaderSettings.model_fields) == {
        "environment",
        "loader_database_url",
        "corpus_target",
    }


def test_the_environment_defaults_to_development_and_must_be_known(tmp_path: Path) -> None:
    env = {"DIVESAFE_LOADER_DATABASE_URL": SECRET_URL}
    assert load_loader_settings(env).environment == "development"
    assert "known environment" in refused({**env, "DIVESAFE_ENVIRONMENT": "staging"})


# --- the secrets directory -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        "divesafe_database_url",
        "divesafe_admin_database_url",
        "divesafe_api_key_hashes",
        "db_owner_password",
        "db_app_password",
        "db_loader_password",
        "anything",
    ],
)
def test_a_secrets_directory_with_any_extra_entry_is_refused(tmp_path: Path, extra: str) -> None:
    text = refused(production(secrets_dir(tmp_path, SECRET_URL, extra)))
    assert "nothing else" in text and extra not in text and "Zq9" not in text


def test_a_secrets_directory_with_a_subdirectory_is_refused(tmp_path: Path) -> None:
    directory = secrets_dir(tmp_path)
    (directory / "sub").mkdir()
    assert "nothing else" in refused(production(directory))


def test_a_secrets_directory_without_the_loader_file_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    assert "nothing else" in refused(production(directory))


def test_an_empty_loader_file_means_not_set(tmp_path: Path) -> None:
    directory = secrets_dir(tmp_path, "   ")
    assert "not set" in refused(production(directory))


def test_an_unreadable_secrets_directory_is_refused_without_its_path(tmp_path: Path) -> None:
    text = refused(production(tmp_path / "missing"))
    assert "readable directory" in text and "missing" not in text


def test_an_env_file_beside_a_secrets_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = secrets_dir(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("DIVESAFE_ENVIRONMENT=development\n")
    monkeypatch.chdir(work)
    assert ".env" in refused(production(directory))


def test_the_same_value_in_the_environment_and_the_file_is_refused(tmp_path: Path) -> None:
    env = {**production(secrets_dir(tmp_path)), "DIVESAFE_LOADER_DATABASE_URL": SECRET_URL}
    text = refused(env)
    assert "Zq9" not in text


def test_production_without_a_secrets_directory_is_refused_even_with_the_env_var() -> None:
    env = {"DIVESAFE_ENVIRONMENT": "production", "DIVESAFE_LOADER_DATABASE_URL": SECRET_URL}
    text = refused(env)
    assert "SECRETS_DIR is required" in text and "Zq9" not in text


# --- the TLS mode and the development fallback ---------------------------------------------------


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_production_accepts_the_tls_modes(tmp_path: Path, mode: str) -> None:
    url = f"postgresql://loader:Zq9-never-echoed@db/divesafe?sslmode={mode}"
    load_loader_settings(production(secrets_dir(tmp_path, url)))


@pytest.mark.parametrize(
    "query", ["", "?sslmode=disable", "?sslmode=allow", "?sslmode=prefer", "?sslmode=REQUIRE"]
)
def test_production_refuses_a_string_that_does_not_force_tls_without_echoing_it(
    tmp_path: Path, query: str
) -> None:
    url = f"postgresql://loader:Zq9-never-echoed@db/divesafe{query}"
    text = refused(production(secrets_dir(tmp_path, url)))
    assert "sslmode" in text and "Zq9" not in text and "db/divesafe" not in text


def test_an_unparseable_string_is_refused_without_the_parser_message(tmp_path: Path) -> None:
    url = "host=db password=Zq9-never-echoed sslmode"
    text = refused(production(secrets_dir(tmp_path, url)))
    assert "Zq9" not in text


def test_the_environment_variable_is_a_fallback_outside_production_only() -> None:
    env = {"DIVESAFE_LOADER_DATABASE_URL": "postgresql://l:Zq9@127.0.0.1/x_test"}
    for name in ("development", "test"):
        settings = load_loader_settings({**env, "DIVESAFE_ENVIRONMENT": name})
        assert settings.loader_database_url.get_secret_value().startswith("postgresql://")
    assert "required in production" in refused({**env, "DIVESAFE_ENVIRONMENT": "production"})


def test_nothing_set_is_refused_without_a_value() -> None:
    assert "not set" in refused({})


def check_fallback_is_not_used_in_production(env: dict[str, str]) -> None:
    with pytest.raises(LoaderConfigError):
        load_loader_settings(env)


def test_mutation_a_fallback_that_works_in_production_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = inspect.getsource(loader_settings.load_loader_settings)
    old = (
        '    elif name == "production":\n'
        '        raise LoaderConfigError("DIVESAFE_SECRETS_DIR is required in production")\n'
    )
    assert old in source
    namespace: dict[str, object] = {}
    exec(source.replace(old, ""), vars(loader_settings), namespace)  # noqa: S102
    mutated = namespace["load_loader_settings"]
    env = {
        "DIVESAFE_ENVIRONMENT": "production",
        "DIVESAFE_LOADER_DATABASE_URL": "postgresql://l:p@db/x?sslmode=require",
    }
    assert mutated(env).production  # type: ignore[operator]
    with pytest.raises(LoaderConfigError):
        load_loader_settings(env)


def test_mutation_a_missing_tls_check_is_caught(tmp_path: Path) -> None:
    source = inspect.getsource(loader_settings.load_loader_settings)
    old = "        _require_production_shape(url)\n"
    assert old in source
    namespace: dict[str, object] = {}
    exec(source.replace(old, "        pass\n"), vars(loader_settings), namespace)  # noqa: S102
    env = production(secrets_dir(tmp_path, "postgresql://l:p@db/x?sslmode=disable"))
    namespace["load_loader_settings"](env)  # type: ignore[operator]
    with pytest.raises(LoaderConfigError):
        load_loader_settings(env)


def test_mutation_an_extra_entry_allowed_is_caught(tmp_path: Path) -> None:
    source = inspect.getsource(loader_settings._url_from_secrets_dir)
    old = "    if entries != {secret_file_name(LOADER_FIELD)}:"
    assert old in source
    namespace: dict[str, object] = {}
    mutated = source.replace(old, "    if secret_file_name(LOADER_FIELD) not in entries:")
    exec(mutated, vars(loader_settings), namespace)  # noqa: S102
    directory = secrets_dir(tmp_path, SECRET_URL, "db_owner_password")
    assert namespace["_url_from_secrets_dir"](directory, {}) == SECRET_URL  # type: ignore[operator]
    with pytest.raises(LoaderConfigError):
        load_loader_settings(production(directory))


# --- the API refuses the loader credential -------------------------------------------------------


def test_the_api_refuses_the_loader_url_in_the_environment() -> None:
    with pytest.raises(RuntimeError) as caught:
        refuse_loader_credentials({"DIVESAFE_LOADER_DATABASE_URL": SECRET_URL})
    assert "Zq9" not in str(caught.value)


def test_the_api_refuses_the_loader_file_in_the_secrets_directory(tmp_path: Path) -> None:
    directory = secrets_dir(tmp_path)
    with pytest.raises(RuntimeError) as caught:
        refuse_loader_credentials({"DIVESAFE_SECRETS_DIR": str(directory)})
    assert "Zq9" not in str(caught.value) and str(directory) not in str(caught.value)


def test_the_api_refuses_a_dangling_symlink_named_like_the_loader_file(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / FILE).symlink_to(tmp_path / "nowhere")
    with pytest.raises(RuntimeError):
        refuse_loader_credentials({"DIVESAFE_SECRETS_DIR": str(directory)})


def test_the_api_passes_without_the_loader_credential(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "divesafe_database_url").write_text("postgresql://a:b@db/x\n")
    refuse_loader_credentials({})
    refuse_loader_credentials({"DIVESAFE_SECRETS_DIR": str(directory)})
    refuse_loader_credentials({"DIVESAFE_LOADER_DATABASE_URL": ""})


def test_production_start_up_calls_the_refusal() -> None:
    from divesafe.api import state

    source = inspect.getsource(state.build_default_state)
    assert 'settings.environment == "production"' in source
    assert "refuse_loader_credentials(os.environ)" in source


def test_mutation_an_api_that_ignores_the_secret_file_is_caught(tmp_path: Path) -> None:
    from divesafe.api import state

    source = inspect.getsource(refuse_loader_credentials)
    old = "    if location and os.path.lexists(Path(location) / secret_file_name(LOADER_FIELD)):"
    assert old in source
    namespace: dict[str, object] = {}
    exec(source.replace(old, "    if False:"), vars(state), namespace)  # noqa: S102
    env = {"DIVESAFE_SECRETS_DIR": str(secrets_dir(tmp_path))}
    namespace["refuse_loader_credentials"](env)  # type: ignore[operator]
    with pytest.raises(RuntimeError):
        refuse_loader_credentials(env)


# --- round 8: the corpus target is an absolute path in production --------------------------------


@pytest.mark.parametrize("target", ["corpus", "./corpus", "../corpus", "a/b", "~/corpus"])
def test_production_refuses_a_relative_corpus_target(tmp_path: Path, target: str) -> None:
    env = {**production(secrets_dir(tmp_path)), "DIVESAFE_CORPUS_TARGET": target}
    text = refused(env)
    assert "absolute" in text and "Zq9" not in text and target not in text


def test_production_accepts_an_absolute_corpus_target(tmp_path: Path) -> None:
    target = str(tmp_path / "corpus")
    env = {**production(secrets_dir(tmp_path)), "DIVESAFE_CORPUS_TARGET": target}
    settings = load_loader_settings(env)
    assert settings.corpus_target == Path(target)


def test_production_without_a_corpus_target_is_still_accepted_for_withdraw(tmp_path: Path) -> None:
    settings = load_loader_settings(production(secrets_dir(tmp_path)))
    assert settings.corpus_target is None


def test_a_relative_corpus_target_is_allowed_outside_production(tmp_path: Path) -> None:
    for name in ("development", "test"):
        env = {
            "DIVESAFE_ENVIRONMENT": name,
            "DIVESAFE_LOADER_DATABASE_URL": "postgresql://l:p@127.0.0.1/x_test",
            "DIVESAFE_CORPUS_TARGET": "corpus",
        }
        assert load_loader_settings(env).corpus_target == Path("corpus")


def test_mutation_a_relative_target_allowed_in_production_is_caught(tmp_path: Path) -> None:
    source = inspect.getsource(loader_settings.load_loader_settings)
    old = '    if name == "production" and target and not Path(target).is_absolute():\n'
    assert old in source
    namespace: dict[str, object] = {}
    exec(source.replace(old, "    if False:\n"), vars(loader_settings), namespace)  # noqa: S102
    mutated = namespace["load_loader_settings"]
    env = {**production(secrets_dir(tmp_path)), "DIVESAFE_CORPUS_TARGET": "corpus"}
    assert mutated(env).corpus_target == Path("corpus")  # type: ignore[operator]
    with pytest.raises(LoaderConfigError):
        load_loader_settings(env)


def test_mutation_the_absolute_rule_applied_outside_production_is_caught(tmp_path: Path) -> None:
    source = inspect.getsource(loader_settings.load_loader_settings)
    old = '    if name == "production" and target and not Path(target).is_absolute():\n'
    namespace: dict[str, object] = {}
    mutated_source = source.replace(old, "    if target and not Path(target).is_absolute():\n")
    exec(mutated_source, vars(loader_settings), namespace)  # noqa: S102
    env = {
        "DIVESAFE_ENVIRONMENT": "development",
        "DIVESAFE_LOADER_DATABASE_URL": "postgresql://l:p@127.0.0.1/x_test",
        "DIVESAFE_CORPUS_TARGET": "corpus",
    }
    load_loader_settings(env)  # the real rule allows it
    with pytest.raises(LoaderConfigError):
        namespace["load_loader_settings"](env)  # type: ignore[operator]
