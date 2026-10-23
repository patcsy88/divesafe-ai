"""The loader connection string's shape: connect_timeout rules (every environment) and the
production shape (user, password, no Unix-socket host). ADR 0024 A1/A3. Offline. Mutations run on
COPIES of the source. Messages must never echo the string."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from divesafe.config import loader_settings
from divesafe.config.loader_settings import LoaderConfigError, load_loader_settings
from divesafe.services import kb_common

pytestmark = pytest.mark.safety

BASE = "postgresql://loader:Zq9-never-echoed@db.example/divesafe"
SSL = "sslmode=require"


def dev(url: str) -> dict[str, str]:
    return {"DIVESAFE_ENVIRONMENT": "development", "DIVESAFE_LOADER_DATABASE_URL": url}


def prod_check(url: str) -> None:
    loader_settings._check_uri_and_timeout(url)
    loader_settings._require_production_shape(url)


# --- connect_timeout ---

REFUSED_TIMEOUT_URLS = [
    f"{BASE}?connect_timeout=0",
    f"{BASE}?connect_timeout=05",
    f"{BASE}?connect_timeout=6",
    f"{BASE}?connect_timeout=60",
    f"{BASE}?connect_timeout=abc",
    f"{BASE}?connect_timeout=",
    f"{BASE}?connect_timeout=-1",
    f"{BASE}?connect_timeout=%2B5",
    f"{BASE}?connect%5Ftimeout=1",  # %-encoded key: libpq decodes it
    f"{BASE}?connect_timeout=1&connect_timeout=1",  # repeated key
    f"{BASE}?connect_timeout=1&c%6Fnnect_timeout=60",
    f"{BASE}?sslmode=require&sslmode=disable",
    "host=db user=loader password=Zq9-never-echoed connect_timeout=1",  # keyword/value form
    "mysql://loader:Zq9-never-echoed@db/divesafe",
    "http://loader:Zq9-never-echoed@db/divesafe",
    "",
]


def refuses(check: Callable[[str], Any], url: str) -> bool:
    try:
        check(url)
    except LoaderConfigError:
        return True
    return False


@pytest.mark.parametrize("url", REFUSED_TIMEOUT_URLS)
def test_each_bad_string_is_refused_in_every_environment_without_an_echo(url: str) -> None:
    for name in ("development", "test"):  # production reads a secrets dir; see test_loader_settings
        env = {"DIVESAFE_ENVIRONMENT": name, "DIVESAFE_LOADER_DATABASE_URL": url}
        with pytest.raises(LoaderConfigError) as caught:
            load_loader_settings(env)
        assert "Zq9" not in str(caught.value) and "db.example" not in str(caught.value)
    assert refuses(loader_settings._check_uri_and_timeout, url)


@pytest.mark.parametrize("value", ["1", "2", "3", "4", "5"])
def test_a_timeout_of_one_to_five_is_accepted(value: str) -> None:
    loader_settings._check_uri_and_timeout(f"{BASE}?connect_timeout={value}")
    assert load_loader_settings(dev(f"{BASE}?connect_timeout={value}")).loader_database_url


@pytest.mark.parametrize("url", [BASE, f"{BASE}?{SSL}", "postgres://u:p@h/d"])
def test_an_absent_timeout_is_accepted(url: str) -> None:
    loader_settings._check_uri_and_timeout(url)


def _checker(old: str, new: str) -> Callable[[str], None]:
    source = inspect.getsource(loader_settings._check_uri_and_timeout)
    assert source.count(old) == 1, old
    namespace: dict[str, Any] = {}
    exec(source.replace(old, new), vars(loader_settings), namespace)  # noqa: S102
    return namespace["_check_uri_and_timeout"]  # type: ignore[no-any-return]


def every_bad_string_refused(check: Callable[[str], Any]) -> None:
    for url in REFUSED_TIMEOUT_URLS:
        assert refuses(check, url), url


def test_the_unmutated_check_refuses_every_bad_string() -> None:
    every_bad_string_refused(loader_settings._check_uri_and_timeout)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("percent in key allowed", 'if "%" in key or key in seen:', "if key in seen:"),
        ("repeated key allowed", 'if "%" in key or key in seen:', 'if "%" in key:'),
        ("ceiling removed", "or int(value) > _CONNECT_TIMEOUT_CEILING", ""),
        ("ceiling raised", "_CONNECT_TIMEOUT_CEILING\n", "60\n"),
        (
            "leading zero allowed",
            "_TIMEOUT_VALUE.fullmatch(value) is None",
            "not value.isdigit() or int(value) == 0",
        ),
        ("keyword form allowed", "if not dsn.startswith(_URI_SCHEMES):", "if False:"),
        ("scheme unchecked", "if not dsn.startswith(_URI_SCHEMES):", "if not dsn:"),
    ],
)
def test_mutation_a_weaker_timeout_check_is_caught(label: str, old: str, new: str) -> None:
    source = inspect.getsource(loader_settings._check_uri_and_timeout)
    if old not in source:
        pytest.fail(f"mutation target missing: {label}")
    if label == "ceiling raised":
        # the ceiling is a module constant: mutate it in a patched namespace
        namespace: dict[str, Any] = dict(vars(loader_settings), _CONNECT_TIMEOUT_CEILING=60)
        exec(source, namespace)  # noqa: S102
        mutated = namespace["_check_uri_and_timeout"]
    else:
        mutated = _checker(old, new)
    with pytest.raises(AssertionError):
        every_bad_string_refused(mutated)


# --- the keyword wins, offline ---


def test_the_keyword_overrides_a_value_in_the_string() -> None:
    parts = conninfo_to_dict("postgresql://u:p@h/d?connect_timeout=60", connect_timeout=5)
    assert parts["connect_timeout"] == 5


def test_connect_passes_the_timeout_as_a_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(dsn: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "c"

    monkeypatch.setattr(psycopg, "connect", fake)
    kb_common.connect("postgresql://u:p@h/d?connect_timeout=60")
    assert seen["connect_timeout"] == kb_common.CONNECT_TIMEOUT_SECONDS == 5


def test_mutation_a_connect_that_drops_the_keyword_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = inspect.getsource(kb_common.connect)
    old = "dsn, connect_timeout=require_limit(CONNECT_TIMEOUT_SECONDS), autocommit=False"
    assert old in source
    namespace: dict[str, Any] = {}
    exec(source.replace(old, "dsn, autocommit=False"), vars(kb_common), namespace)  # noqa: S102
    seen: dict[str, Any] = {}
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kw: seen.update(kw))
    namespace["connect"]("postgresql://u:p@h/d")
    assert "connect_timeout" not in seen  # the mutant loses the bound
    seen.clear()
    kb_common.connect("postgresql://u:p@h/d")
    assert seen["connect_timeout"] == 5


# --- the production shape ---

GOOD_PROD = f"{BASE}?{SSL}"
BAD_SHAPES = [
    f"postgresql://db.example/divesafe?{SSL}",  # no user, no password
    f"postgresql://loader@db.example/divesafe?{SSL}",  # no password
    f"postgresql://:Zq9-never-echoed@db.example/divesafe?{SSL}",  # no user
    f"postgresql://loader:@db.example/divesafe?{SSL}",  # empty password
    f"postgresql://loader:Zq9-never-echoed@/divesafe?host=/var/run/postgresql&{SSL}",
    f"postgresql://loader:Zq9-never-echoed@/divesafe?host=db.example,/var/run&{SSL}",
    f"postgresql://loader:Zq9-never-echoed@/divesafe?host=/tmp,db.example&{SSL}",
    "postgresql://loader:Zq9-never-echoed@db.example/divesafe",  # sslmode defaults to prefer
    "postgresql://loader:Zq9-never-echoed@db.example/divesafe?sslmode=disable",
]


def test_a_good_production_string_passes() -> None:
    prod_check(GOOD_PROD)


@pytest.mark.parametrize("url", BAD_SHAPES)
def test_each_bad_production_shape_is_refused_without_an_echo(url: str) -> None:
    with pytest.raises(LoaderConfigError) as caught:
        prod_check(url)
    text = str(caught.value)
    assert "Zq9" not in text and "db.example" not in text and "/var/run" not in text


def _shape(old: str, new: str) -> Callable[[str], None]:
    source = inspect.getsource(loader_settings._require_production_shape)
    assert source.count(old) == 1, old
    namespace: dict[str, Any] = {}
    exec(source.replace(old, new), vars(loader_settings), namespace)  # noqa: S102
    return namespace["_require_production_shape"]  # type: ignore[no-any-return]


def every_bad_shape_refused(check: Callable[[str], Any]) -> None:
    for url in BAD_SHAPES:
        assert refuses(check, url), url


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        (
            "user not required",
            'if not parts.get("user") or not parts.get("password"):',
            'if not parts.get("password"):',
        ),
        (
            "password not required",
            'if not parts.get("user") or not parts.get("password"):',
            'if not parts.get("user"):',
        ),
        ("socket host allowed", 'if any(h.startswith("/") for h in hosts):', "if False:"),
        (
            "only the first host checked",
            'if any(h.startswith("/") for h in hosts):',
            'if hosts[0].startswith("/"):',
        ),
        ("tls not required", "if mode not in _TLS_MODES:", "if False:"),
    ],
)
def test_mutation_a_weaker_production_shape_is_caught(label: str, old: str, new: str) -> None:
    every_bad_shape_refused(loader_settings._require_production_shape)
    with pytest.raises(AssertionError):
        every_bad_shape_refused(_shape(old, new))


@pytest.mark.parametrize("url", [*REFUSED_TIMEOUT_URLS[:11], *BAD_SHAPES])
def test_production_refuses_through_the_secrets_directory_without_an_echo(
    url: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "divesafe_loader_database_url").write_text(url + "\n")
    env = {"DIVESAFE_ENVIRONMENT": "production", "DIVESAFE_SECRETS_DIR": str(directory)}
    if url in (GOOD_PROD,):
        return
    # REFUSED_TIMEOUT_URLS lack sslmode, so they may be refused for TLS first: still refused.
    with pytest.raises(LoaderConfigError) as caught:
        load_loader_settings(env)
    assert "Zq9" not in str(caught.value) and "db.example" not in str(caught.value)
