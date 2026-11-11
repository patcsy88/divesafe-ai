"""Secrets read from files (ADR 0024). A secret must reach Settings from a file only when it is on
the allow-list, never together with an environment value, and never appear in an error or repr."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from divesafe.config.secret_files import (
    MAX_SECRET_BYTES,
    SECRET_FILES,
    SecretFileError,
    read_secret_files,
    secret_file_name,
)
from divesafe.config.settings import get_settings, load_settings

pytestmark = pytest.mark.safety

KEY = "k3y-from-a-file-0123456789abcdef"


def _write(directory: Path, field: str, text: str) -> None:
    (directory / secret_file_name(field)).write_text(text)


@pytest.fixture
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    monkeypatch.setenv("DIVESAFE_SECRETS_DIR", str(directory))
    return directory


def test_without_the_variable_files_are_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "llm_api_key", KEY)
    monkeypatch.chdir(tmp_path)
    assert load_settings().llm_api_key is None


def test_a_listed_secret_is_read_from_its_file(secrets: Path) -> None:
    _write(secrets, "llm_api_key", KEY + "\n")
    settings = load_settings()
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == KEY


def test_get_settings_reads_the_files_too(secrets: Path) -> None:
    _write(secrets, "met_api_token", "tok-123")
    assert get_settings().met_api_token is not None


@pytest.mark.parametrize(
    "field", ["environment", "allow_external_llm", "llm_server_is_local", "auth_mode", "log_level"]
)
def test_a_file_cannot_set_an_ordinary_setting(secrets: Path, field: str) -> None:
    """Only the names on the allow-list are read: a stray file must not flip a safety setting."""
    assert field not in SECRET_FILES
    _write(secrets, field, "production" if field == "environment" else "true")
    settings = load_settings()
    assert settings.environment == "development"
    assert settings.allow_external_llm is False
    assert settings.llm_server_is_local is False


def test_the_secrets_directory_cannot_be_set_from_a_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    _write(other, "llm_api_key", KEY)
    (tmp_path / ".env").write_text(f"DIVESAFE_SECRETS_DIR={other}\n")
    monkeypatch.chdir(tmp_path)
    assert load_settings().llm_api_key is None


def test_a_value_in_both_the_environment_and_a_file_is_refused_without_the_value(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(secrets, "llm_api_key", KEY)
    monkeypatch.setenv("DIVESAFE_LLM_API_KEY", "env-key-value-9876543210")
    with pytest.raises(SecretFileError, match="set only one") as caught:
        load_settings()
    assert KEY not in str(caught.value) and "env-key-value" not in str(caught.value)


def test_an_empty_environment_variable_does_not_conflict_with_a_file(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings treat an empty variable as unset, so an empty one next to a file is not a clash."""
    _write(secrets, "llm_api_key", KEY)
    monkeypatch.setenv("DIVESAFE_LLM_API_KEY", "")
    settings = load_settings()
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == KEY


def test_the_conflict_check_ignores_the_case_of_the_variable_name(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(secrets, "met_api_token", "tok-123")
    monkeypatch.setenv("divesafe_met_api_token", "tok-456")
    with pytest.raises(SecretFileError, match="set only one"):
        read_secret_files(secrets, {"divesafe_met_api_token": "tok-456"})


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_an_empty_file_means_not_set(secrets: Path, text: str) -> None:
    _write(secrets, "llm_api_key", text)
    assert load_settings().llm_api_key is None


def test_a_single_line_secret_with_extra_lines_is_refused_without_the_value(
    secrets: Path,
) -> None:
    _write(secrets, "llm_api_key", f"{KEY}\nsecond-line-9876")
    with pytest.raises(SecretFileError, match="single line") as caught:
        load_settings()
    assert KEY not in str(caught.value) and "second-line" not in str(caught.value)


def test_the_key_hash_table_may_span_lines(secrets: Path) -> None:
    table = '{\n  "' + "a" * 64 + '": {"actor": "x", "roles": ["viewer"]}\n}\n'
    _write(secrets, "api_key_hashes", table)
    settings = load_settings()
    assert settings.api_key_hashes is not None
    assert "viewer" in settings.api_key_hashes.get_secret_value()


def test_a_missing_directory_is_an_error_not_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mistyped path must not start the server with no secrets and a development default."""
    monkeypatch.setenv("DIVESAFE_SECRETS_DIR", str(tmp_path / "nope"))
    with pytest.raises(SecretFileError, match="not a directory"):
        load_settings()


def test_a_directory_in_place_of_a_file_is_refused(secrets: Path) -> None:
    (secrets / secret_file_name("llm_api_key")).mkdir()
    with pytest.raises(SecretFileError, match="not a file"):
        load_settings()


def test_an_oversized_file_is_refused(secrets: Path) -> None:
    _write(secrets, "llm_api_key", "x" * (MAX_SECRET_BYTES + 1))
    with pytest.raises(SecretFileError, match="larger than"):
        load_settings()


@pytest.mark.parametrize("raw", [b"\xff\xfe\xfa", b"abc\x00def"])
def test_binary_content_is_refused(secrets: Path, raw: bytes) -> None:
    (secrets / secret_file_name("llm_api_key")).write_bytes(raw)
    with pytest.raises(SecretFileError):
        load_settings()


def test_a_symlinked_file_is_refused(secrets: Path, tmp_path: Path) -> None:
    """O_NOFOLLOW: a link could point anywhere the process can read, so it is refused."""
    target = tmp_path / "real"
    target.write_text(KEY)
    (secrets / secret_file_name("llm_api_key")).symlink_to(target)
    with pytest.raises(SecretFileError, match="cannot be read") as caught:
        load_settings()
    assert KEY not in str(caught.value)


def test_a_secret_never_appears_in_the_settings_repr(secrets: Path) -> None:
    _write(secrets, "llm_api_key", KEY)
    _write(secrets, "database_url", "postgresql://app:pw-in-a-file-77@db:5432/x?sslmode=require")
    text = repr(load_settings()) + str(load_settings().model_dump())
    assert KEY not in text and "pw-in-a-file-77" not in text


def test_a_badly_shaped_secret_from_a_file_is_refused_without_the_value(secrets: Path) -> None:
    bad = "not allowed: spaces and $ign5"
    _write(secrets, "met_api_token", bad)
    with pytest.raises(ValidationError) as caught:
        load_settings()
    assert bad not in str(caught.value)


def test_production_still_demands_tls_for_a_database_url_read_from_a_file(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVESAFE_ENVIRONMENT", "production")
    monkeypatch.setenv("DIVESAFE_LLM_PROVIDER", "openai_compatible")
    _write(secrets, "api_key_hashes", '{"' + "a" * 64 + '": {"actor": "x", "roles": ["viewer"]}}')
    _write(secrets, "database_url", "postgresql://app:pw-in-a-file-77@db:5432/x")
    with pytest.raises(ValidationError, match="sslmode") as caught:
        load_settings()
    assert "pw-in-a-file-77" not in str(caught.value)
    _write(secrets, "database_url", "postgresql://app:pw-in-a-file-77@db:5432/x?sslmode=require")
    assert load_settings().environment == "production"


def test_db_init_reads_the_admin_url_from_its_file(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.services import db_init

    _write(secrets, "admin_database_url", "postgresql://owner:pw-owner-55@db:5432/x")
    _write(secrets, "database_url", "postgresql://app:pw-app-66@db:5432/x")
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setenv("DIVESAFE_APP_DB_ROLE", "divesafe_app")
    monkeypatch.setattr(db_init, "install_schema", lambda dsn, role: seen.append((dsn, role)))
    db_init.main()
    assert seen == [("postgresql://owner:pw-owner-55@db:5432/x", "divesafe_app")]


def test_a_dotenv_file_with_a_secrets_directory_is_refused_not_dropped_or_overridden(
    secrets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env value must neither lose quietly to a secret file nor be ignored without notice (a
    dropped production setting would fall back to a development default). The start is refused,
    and the message names the file, never a value."""
    from divesafe.config import Settings

    # tests/conftest.py turns .env reading off for every test; this one needs it on
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / ".env").write_text("DIVESAFE_LLM_API_KEY=from-dotenv\nDIVESAFE_LOG_LEVEL=DEBUG\n")
    _write(secrets, "llm_api_key", KEY)
    monkeypatch.chdir(workdir)
    with pytest.raises(SecretFileError, match=r"\.env") as caught:
        load_settings()
    assert "from-dotenv" not in str(caught.value)
    assert KEY not in str(caught.value)


def test_without_a_dotenv_file_a_secrets_directory_loads_normally(
    secrets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    workdir = tmp_path / "empty"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    _write(secrets, "llm_api_key", KEY)
    assert load_settings().llm_api_key is not None


def test_a_dotenv_file_is_still_read_when_no_secrets_directory_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is specific to the secrets mode; development keeps using .env."""
    from divesafe.config import Settings

    monkeypatch.delenv("DIVESAFE_SECRETS_DIR", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    (tmp_path / ".env").write_text("DIVESAFE_LOG_LEVEL=DEBUG\n")
    monkeypatch.chdir(tmp_path)
    assert load_settings().log_level == "DEBUG"


def test_the_allow_list_is_exactly_these_six_names() -> None:
    """The sixth, `loader_database_url`, is read only by the loader's own narrow settings (ADR 0024
    A4); the shared `Settings` has no field for it and the API refuses it in production."""
    from divesafe.config import Settings

    assert set(SECRET_FILES) == {
        "database_url",
        "admin_database_url",
        "llm_api_key",
        "met_api_token",
        "api_key_hashes",
        "loader_database_url",
    }
    assert "loader_database_url" not in Settings.model_fields
    assert all(
        name in Settings.model_fields for name in SECRET_FILES if name != "loader_database_url"
    )


def test_a_dangling_symlink_is_refused_not_treated_as_unset(secrets: Path, tmp_path: Path) -> None:
    (secrets / secret_file_name("met_api_token")).symlink_to(tmp_path / "gone")
    with pytest.raises(SecretFileError, match="cannot be read"):
        load_settings()


def test_a_named_pipe_is_refused_without_hanging(secrets: Path) -> None:
    import os

    os.mkfifo(secrets / secret_file_name("llm_api_key"))
    with pytest.raises(SecretFileError, match="not a file"):
        load_settings()


@pytest.mark.parametrize("separator", ["\x0b", "\x0c", "\x85", "\u2028", "\u2029", "\x1c"])
def test_other_line_separators_in_a_single_line_secret_are_refused(
    secrets: Path, separator: str
) -> None:
    _write(secrets, "llm_api_key", f"{KEY}{separator}{KEY}")
    with pytest.raises(SecretFileError, match="single line") as caught:
        load_settings()
    assert KEY not in str(caught.value)


def test_the_running_application_refuses_the_owner_url_in_production(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only db_init may hold the owner-role URL (ADR 0024): the API refuses to start with it."""
    from divesafe.api.state import build_default_state

    monkeypatch.setenv("DIVESAFE_ENVIRONMENT", "production")
    monkeypatch.setenv("DIVESAFE_LLM_PROVIDER", "openai_compatible")
    _write(secrets, "api_key_hashes", '{"' + "a" * 64 + '": {"actor": "x", "roles": ["viewer"]}}')
    _write(secrets, "database_url", "postgresql://app:pw-app-66@db:5432/x?sslmode=require")
    _write(secrets, "admin_database_url", "postgresql://owner:pw-owner-55@db:5432/x")
    settings = load_settings()
    with pytest.raises(RuntimeError, match="owner-role") as caught:
        build_default_state(settings)
    assert "pw-owner-55" not in str(caught.value)


def test_the_owner_url_refusal_is_for_production_only(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Development may hold both URLs (the development stack does); the refusal must not leak
    into it, or it would be removed by the first person it got in the way of."""
    from divesafe.api.state import build_default_state

    _write(secrets, "admin_database_url", "postgresql://owner:pw-owner-55@localhost:1/x")
    assert load_settings().environment == "development"
    build_default_state(load_settings())


def test_a_dotenv_file_in_a_later_entry_of_a_list_is_refused_too(
    secrets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", (".env.first", ".env.second"))
    (tmp_path / ".env.second").write_text("DIVESAFE_LOG_LEVEL=DEBUG\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SecretFileError, match=r"\.env\.second"):
        load_settings()


def test_a_dotenv_file_in_the_home_directory_is_found_through_a_tilde(
    secrets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.config import Settings

    (tmp_path / ".env.home").write_text("DIVESAFE_LOG_LEVEL=DEBUG\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setitem(Settings.model_config, "env_file", "~/.env.home")
    with pytest.raises(SecretFileError, match=r"\.env\.home"):
        load_settings()


def test_a_dotenv_file_that_appears_after_the_check_is_still_not_read(
    secrets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal runs first, and the settings object is then built with .env reading off."""
    from divesafe.config import Settings, secret_files

    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    monkeypatch.chdir(tmp_path)

    original = secret_files.read_secret_files

    def late(location: Path, environ: object) -> dict[str, object]:
        (tmp_path / ".env").write_text("DIVESAFE_LOG_LEVEL=DEBUG\n")
        return original(location, environ)  # type: ignore[arg-type]

    monkeypatch.setattr(secret_files, "read_secret_files", late)
    assert load_settings().log_level != "DEBUG"


# --- the loader name is opened only when asked for (ADR 0024 A4) ---


def _opened_names(monkeypatch: pytest.MonkeyPatch, directory: Path, **kwargs: object) -> list[str]:
    import os

    from divesafe.config import secret_files

    opened: list[str] = []
    real = os.open

    def spy(path: object, flags: int, *rest: int) -> int:
        opened.append(Path(str(path)).name)
        return real(path, flags, *rest)  # type: ignore[arg-type]

    monkeypatch.setattr(secret_files.os, "open", spy)
    secret_files.read_secret_files(directory, {}, **kwargs)  # type: ignore[arg-type]
    return opened


def test_the_default_read_never_opens_the_loader_file(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.config.secret_files import SECRET_FILES

    for field in SECRET_FILES:
        (secrets / secret_file_name(field)).write_text("postgresql://u:p@h/d\n")
    opened = _opened_names(monkeypatch, secrets)
    assert secret_file_name("loader_database_url") not in opened
    assert len(opened) == len(SECRET_FILES) - 1


def test_the_loader_field_is_read_when_asked_for_and_only_it(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.config.secret_files import LOADER_SECRET_FIELD, SECRET_FILES

    for field in SECRET_FILES:
        (secrets / secret_file_name(field)).write_text("postgresql://u:p@h/d\n")
    opened = _opened_names(monkeypatch, secrets, fields=(LOADER_SECRET_FIELD,))
    assert opened == [secret_file_name(LOADER_SECRET_FIELD)]


def test_mutation_a_default_that_includes_the_loader_file_is_caught(
    secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect

    from divesafe.config import secret_files

    source = inspect.getsource(secret_files.read_secret_files)
    old = "{f for f in SECRET_FILES if f != LOADER_SECRET_FIELD}"
    assert old in source
    namespace: dict[str, object] = {}
    exec(source.replace(old, "set(SECRET_FILES)"), vars(secret_files), namespace)  # noqa: S102
    (secrets / secret_file_name("loader_database_url")).write_text("postgresql://u:p@h/d\n")
    found = namespace["read_secret_files"](secrets, {})  # type: ignore[operator]
    assert "loader_database_url" in found  # the mutant reads it
    assert "loader_database_url" not in secret_files.read_secret_files(secrets, {})


def test_the_loader_settings_call_passes_only_the_loader_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from divesafe.config import loader_settings
    from divesafe.config.secret_files import LOADER_SECRET_FIELD

    directory = tmp_path / "d"
    directory.mkdir()
    (directory / secret_file_name(LOADER_SECRET_FIELD)).write_text(
        "postgresql://u:p@h/d?sslmode=require\n"
    )
    monkeypatch.chdir(tmp_path)
    seen: list[object] = []
    real = loader_settings.read_secret_files

    def spy(d: Path, env: object, fields: object = None) -> object:
        seen.append(fields)
        return real(d, env, fields=fields)  # type: ignore[arg-type]

    monkeypatch.setattr(loader_settings, "read_secret_files", spy)
    loader_settings._url_from_secrets_dir(directory, {})
    assert seen == [(LOADER_SECRET_FIELD,)]
