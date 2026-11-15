"""ADR 0028 decisions 3 and 7: the knowledge host, `check_egress`, `Settings` and the start-up
attestation check.

`remote_knowledge_opt_in` is returned only when every condition holds; otherwise a fixed refusal is
raised, never a fall-back to another basis. `Settings` refuses at validation, and `check_egress`
repeats each check, because `model_copy` and `model_construct` skip validators. Bases and refusal
messages are literals here, never imported from the code, so a code change cannot move the test.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError
from tests.safety.kb_fakes import mutated_module
from tests.safety.log_fields import scanned_records

from divesafe.api import state as state_module
from divesafe.api.state import (
    build_provider,
    check_knowledge_attestations,
    knowledge_goes_to_model_host,
)
from divesafe.config import Settings
from divesafe.config import settings as settings_module
from divesafe.config.settings import is_single_knowledge_host
from divesafe.models import FakeProvider
from divesafe.models import egress as egress_module
from divesafe.models.egress import check_egress
from divesafe.models.openai_compatible import OpenAICompatibleProvider
from divesafe.services import kb_attestation
from divesafe.services.repository import InMemoryAssessmentRepository

pytestmark = pytest.mark.safety

HOST = "llm.example.org"
OTHER = "other.example.org"
URL = "https://llm.example.org/v1"
OTHER_URL = "https://other.example.org/v1"
DSN = "postgresql://app@db.invalid/divesafe"

KH = "DIVESAFE_LLM_KNOWLEDGE_HOST"
# Settings (ValidationError) messages, as literals.
V_DECLARED_LOCAL = f"{KH} cannot be used with DIVESAFE_LLM_SERVER_IS_LOCAL"
V_NO_OPT_IN = f"{KH} needs DIVESAFE_ALLOW_EXTERNAL_LLM=true"
V_NO_SERVER = f"{KH} needs a model server provider and DIVESAFE_LLM_BASE_URL"
V_MALFORMED = f"{KH} must be exactly one hostname, not a loopback name"
V_NOT_ALLOWED = f"{KH} must be one of DIVESAFE_LLM_ALLOWED_HOSTS"
V_MISMATCH = f"{KH} must be the host of DIVESAFE_LLM_BASE_URL"
# check_egress (RuntimeError) messages, as literals.
R_NOT_REMOTE = f"{KH} needs a model server that is not on this machine"
R_NO_OPT_IN = V_NO_OPT_IN
R_DECLARED_LOCAL = V_DECLARED_LOCAL
R_NO_SERVER = V_NO_SERVER
R_MALFORMED = V_MALFORMED
R_NOT_ALLOWED = V_NOT_ALLOWED
R_MISMATCH = f"{KH} must be the host of DIVESAFE_LLM_BASE_URL and of the provider"
# Start-up refusals, as literals.
UNATTESTED = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set and an approved knowledge version does "
    "not attest that it may be sent to the model host; withdraw or replace it (ADR 0028)"
)
UNREADABLE = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set and the knowledge store's attestations "
    "cannot be read (ADR 0028)"
)
MALFORMED_ANSWER = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set and the knowledge store returned a "
    "malformed answer (ADR 0028)"
)
NO_STORE = (
    "refusing to start: DIVESAFE_LLM_KNOWLEDGE_HOST is set but no knowledge store is configured "
    "(DIVESAFE_DATABASE_URL; ADR 0028)"
)

VALID: dict[str, Any] = {
    "llm_provider": "openai_compatible",
    "llm_model": "m",
    "llm_base_url": URL,
    "llm_allowed_hosts": f"{HOST},{OTHER}",
    "allow_external_llm": True,
    "llm_knowledge_host": HOST,
}


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**VALID, **overrides})  # type: ignore[arg-type, call-arg]


def _construct(**overrides: Any) -> Settings:
    """Skips every validator. Defaults from the class; the other fields as given."""
    defaults = Settings(_env_file=None).model_dump()  # type: ignore[call-arg]
    return Settings.model_construct(**{**defaults, **VALID, **overrides})


def _provider(url: str = URL, hosts: frozenset[str] = frozenset({HOST, OTHER})) -> Any:
    return OpenAICompatibleProvider("m", url, allowed_hosts=hosts)


class _Stub(FakeProvider):
    """A provider whose `external` and `host` are whatever the test says."""

    def __init__(self, external: object, host: object) -> None:
        super().__init__()
        self.external = external  # type: ignore[assignment]
        self.host = host  # type: ignore[assignment]


def _assert_no_host_or_url(text: str) -> None:
    for leak in (HOST, OTHER, "example.org", "https://", "/v1", "203.0.113.7", "localhost"):
        assert leak not in text.replace("DIVESAFE_LLM_SERVER_IS_LOCAL", ""), leak


# ---------------------------------------------------------------- happy path and the absent case


def test_every_condition_holding_gives_the_knowledge_basis() -> None:
    settings = _settings()
    assert settings.llm_knowledge_host == HOST
    assert check_egress(_provider(), settings) == "remote_knowledge_opt_in"
    assert _provider().host == HOST


def test_an_upper_case_base_url_names_the_same_host_and_is_accepted() -> None:
    """Host names are case-insensitive. Pydantic and `urlsplit` lowercase the base URL's host, and
    the adapter's `host` is lowercased, so this is the same host, not a mismatch. (The ADR's Tests
    list puts an upper-case base URL among the refusals; reported, not a leak: see the reply.)"""
    settings = _settings(llm_base_url="https://LLM.Example.ORG/v1")
    provider = _provider("https://LLM.Example.ORG/v1")
    assert provider.host == HOST
    assert check_egress(provider, settings) == "remote_knowledge_opt_in"
    copied = _settings().model_copy(update={"llm_base_url": "https://LLM.Example.ORG/v1"})
    assert check_egress(provider, copied) == "remote_knowledge_opt_in"


def test_absent_keeps_the_remote_basis_and_never_the_knowledge_basis() -> None:
    settings = _settings(llm_knowledge_host=None)
    assert check_egress(_provider(), settings) == "remote_external_opt_in"
    constructed = _construct(llm_knowledge_host=None)
    assert check_egress(_provider(), constructed) == "remote_external_opt_in"


def test_empty_means_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "DIVESAFE_LLM_PROVIDER": "openai_compatible",
        "DIVESAFE_LLM_MODEL": "m",
        "DIVESAFE_LLM_BASE_URL": URL,
        "DIVESAFE_LLM_ALLOWED_HOSTS": HOST,
        "DIVESAFE_ALLOW_EXTERNAL_LLM": "true",
        KH: "",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_knowledge_host is None
    assert check_egress(_provider(), settings) == "remote_external_opt_in"
    # Empty through model_construct (no env reading) is absent as well.
    assert check_egress(_provider(), _construct(llm_knowledge_host="")) == "remote_external_opt_in"
    # Empty never counts as "knowledge goes to the model host".
    assert knowledge_goes_to_model_host(settings, "remote_external_opt_in") is False
    # And the same variable set to the host turns the basis on.
    monkeypatch.setenv(KH, HOST)
    assert check_egress(_provider(), Settings(_env_file=None)) == "remote_knowledge_opt_in"  # type: ignore[call-arg]


# ---------------------------------------------------------------- Settings refusals


SETTINGS_REFUSALS: list[tuple[str, dict[str, Any], str]] = [
    (
        "declared-local",
        {"allow_external_llm": False, "llm_server_is_local": True},
        V_DECLARED_LOCAL,
    ),
    ("no-opt-in", {"allow_external_llm": False}, V_NO_OPT_IN),
    ("fake-provider", {"llm_provider": "fake"}, V_NO_SERVER),
    ("no-base-url", {"llm_base_url": None}, V_NO_SERVER),
    ("comma-list", {"llm_knowledge_host": f"{HOST},{OTHER}"}, V_MALFORMED),
    ("comma-trailing", {"llm_knowledge_host": f"{HOST},"}, V_MALFORMED),
    ("trailing-dot", {"llm_knowledge_host": f"{HOST}."}, V_MALFORMED),
    ("upper-case-host", {"llm_knowledge_host": "LLM.example.org"}, V_MALFORMED),
    ("leading-space", {"llm_knowledge_host": f" {HOST}"}, V_MALFORMED),
    ("trailing-space", {"llm_knowledge_host": f"{HOST} "}, V_MALFORMED),
    ("ip-literal", {"llm_knowledge_host": "203.0.113.7"}, V_MALFORMED),
    ("url-not-host", {"llm_knowledge_host": URL}, V_MALFORMED),
    ("with-port", {"llm_knowledge_host": f"{HOST}:443"}, V_MALFORMED),
    (
        "localhost",
        {"llm_knowledge_host": "localhost", "llm_allowed_hosts": "localhost"},
        V_MALFORMED,
    ),
    (
        "sub-localhost",
        {"llm_knowledge_host": "a.localhost", "llm_allowed_hosts": "a.localhost"},
        V_MALFORMED,
    ),
    ("not-in-allow-list", {"llm_allowed_hosts": OTHER, "llm_base_url": OTHER_URL}, V_NOT_ALLOWED),
    ("base-url-elsewhere", {"llm_base_url": OTHER_URL}, V_MISMATCH),
]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [pytest.param(o, m, id=i) for i, o, m in SETTINGS_REFUSALS],
)
def test_settings_refuses_with_a_fixed_message(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(**overrides)
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["msg"] == f"Value error, {message}"
    _assert_no_host_or_url(message)
    # hide_input_in_errors: the rendered error does not carry the value either (the pydantic
    # documentation link is not the configured URL).
    rendered = re.sub(r"https://errors\.pydantic\.dev/\S+", "", str(caught.value))
    _assert_no_host_or_url(rendered)


def test_settings_refuses_in_production_too() -> None:
    with pytest.raises(ValidationError, match="needs DIVESAFE_ALLOW_EXTERNAL_LLM=true"):
        _settings(environment="production", allow_external_llm=False)


# ---------------------------------------------------------------- check_egress refusals

# Each as an update that `model_copy` and `model_construct` apply without validation.
EGRESS_REFUSALS: list[tuple[str, dict[str, Any], str]] = [
    ("no-opt-in", {"allow_external_llm": False}, R_NO_OPT_IN),
    ("opt-in-truthy-string", {"allow_external_llm": "true"}, R_NO_OPT_IN),
    ("declared-local", {"llm_server_is_local": True}, R_DECLARED_LOCAL),
    ("declared-local-truthy", {"llm_server_is_local": 1}, R_DECLARED_LOCAL),
    ("fake-provider", {"llm_provider": "fake"}, R_NO_SERVER),
    ("no-base-url", {"llm_base_url": None}, R_NO_SERVER),
    ("comma-list", {"llm_knowledge_host": f"{HOST},{OTHER}"}, R_MALFORMED),
    ("trailing-dot", {"llm_knowledge_host": f"{HOST}."}, R_MALFORMED),
    ("upper-case-host", {"llm_knowledge_host": "LLM.EXAMPLE.ORG"}, R_MALFORMED),
    ("ip-literal", {"llm_knowledge_host": "203.0.113.7"}, R_MALFORMED),
    ("non-string-host", {"llm_knowledge_host": 7}, R_MALFORMED),
    ("bytes-host", {"llm_knowledge_host": HOST.encode()}, R_MALFORMED),
    ("not-in-allow-list", {"llm_allowed_hosts": OTHER}, R_NOT_ALLOWED),
    ("empty-allow-list", {"llm_allowed_hosts": None}, R_NOT_ALLOWED),
    ("unreadable-allow-list", {"llm_allowed_hosts": "not a host"}, R_NOT_ALLOWED),
    ("base-url-elsewhere", {"llm_base_url": OTHER_URL}, R_MISMATCH),
    ("base-url-without-host", {"llm_base_url": "https:///v1"}, R_MISMATCH),
]


@pytest.mark.parametrize(
    ("update", "message"),
    [pytest.param(u, m, id=i) for i, u, m in EGRESS_REFUSALS],
)
@pytest.mark.parametrize("how", ["model_copy", "model_construct"])
def test_check_egress_repeats_each_refusal(update: dict[str, Any], message: str, how: str) -> None:
    settings = (
        _settings().model_copy(update=update) if how == "model_copy" else _construct(**update)
    )
    with pytest.raises(RuntimeError) as caught:
        check_egress(_provider(), settings)
    assert str(caught.value) == message
    assert caught.value.__cause__ is None
    _assert_no_host_or_url(str(caught.value))


def test_a_provider_on_another_host_is_refused_while_settings_match() -> None:
    """Settings are valid and name HOST everywhere, but the provider calls OTHER."""
    settings = _settings()
    with pytest.raises(RuntimeError) as caught:
        check_egress(_provider(OTHER_URL), settings)
    assert str(caught.value) == R_MISMATCH


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        pytest.param(_Stub(True, None), R_MISMATCH, id="provider-host-none"),
        pytest.param(_Stub(True, HOST.upper()), R_MISMATCH, id="provider-host-upper"),
        pytest.param(_Stub(True, HOST.encode()), R_MISMATCH, id="provider-host-bytes"),
        pytest.param(_Stub(False, HOST), R_NOT_REMOTE, id="not-external"),
        pytest.param(_Stub(1, HOST), R_NOT_REMOTE, id="external-truthy-int"),
        pytest.param(_Stub("yes", HOST), R_NOT_REMOTE, id="external-truthy-str"),
        pytest.param(FakeProvider(), R_NOT_REMOTE, id="exact-fake-provider"),
    ],
)
def test_check_egress_refuses_these_providers(provider: Any, message: str) -> None:
    for settings in (_settings(), _construct()):
        with pytest.raises(RuntimeError) as caught:
            check_egress(provider, settings)
        assert str(caught.value) == message


def test_a_stub_meeting_every_condition_is_accepted() -> None:
    """The control for the stub cases above: the refusals come from the field under test."""
    assert check_egress(_Stub(True, HOST), _settings()) == "remote_knowledge_opt_in"


def test_a_fake_provider_with_the_host_set_raises_rather_than_being_exempt() -> None:
    """The check runs before the exact-`FakeProvider` exemption (ADR 0028 decision 3)."""
    for llm_provider in ("fake", "openai_compatible"):
        settings = _construct(llm_provider=llm_provider)
        with pytest.raises(RuntimeError):
            check_egress(FakeProvider(), settings)
    # Without the host, the exemption still holds.
    assert check_egress(FakeProvider(), _construct(llm_knowledge_host=None)) == "none"


def test_a_refusal_never_falls_back_to_another_basis() -> None:
    """Every refusal case raises; none returns `remote_external_opt_in` or another basis."""
    for _, update, _ in EGRESS_REFUSALS:
        try:
            basis = check_egress(_provider(), _construct(**update))
        except RuntimeError:
            continue
        pytest.fail(f"returned {basis!r} for {update!r}")


@pytest.mark.parametrize(
    ("value", "single"),
    [
        (HOST, True),
        ("a-b.example.org", True),
        ("example", True),
        (f"{HOST},{OTHER}", False),
        (f"{HOST},", False),
        (f",{HOST}", False),
        (f"{HOST}.", False),
        ("LLM.example.org", False),
        (f" {HOST}", False),
        (f"{HOST}\n", False),
        ("", False),
        ("localhost", False),
        ("a.localhost", False),
        ("LOCALHOST", False),
        ("localhost.", False),
        ("127.0.0.1", False),
        ("203.0.113.7", False),
        ("0x7f.example.org", False),
        ("[::1]", False),
        ("a..b", False),
        ("-a.example.org", False),
        (f"{HOST}:443", False),
        (f"user@{HOST}", False),
    ],
)
def test_is_single_knowledge_host(value: str, single: bool) -> None:
    assert is_single_knowledge_host(value) is single


# ---------------------------------------------------------------- mutations


def _refused(module: Any, provider: Any, settings: Settings) -> bool:
    try:
        module.check_egress(provider, settings)
    except RuntimeError:
        return True
    return False


EGRESS_MUTATIONS: list[tuple[str, tuple[str, str], Any, dict[str, Any]]] = [
    (
        "loopback-or-not-remote-allowed",
        (
            "    if type(provider.external) is not bool or not provider.external:\n",
            "    if False:\n",
        ),
        _Stub(False, HOST),
        {},
    ),
    (
        "opt-in-skipped",
        ("    if settings.allow_external_llm is not True:\n", "    if False:\n"),
        None,
        {"allow_external_llm": False},
    ),
    (
        "is-local-refusal-skipped",
        ("    if settings.llm_server_is_local is not False:\n", "    if False:\n"),
        None,
        {"llm_server_is_local": True},
    ),
    (
        "no-server-skipped",
        (
            '    if settings.llm_provider == "fake" or settings.llm_base_url is None:\n',
            "    if False:\n",
        ),
        _Stub(True, HOST),
        {"llm_provider": "fake"},
    ),
    (
        "loopback-name-allowed",
        (
            "    if type(host) is not str or not is_single_knowledge_host(host):\n",
            "    if False:\n",
        ),
        _Stub(True, "localhost"),
        {
            "llm_knowledge_host": "localhost",
            "llm_allowed_hosts": "localhost",
            "llm_base_url": "http://localhost:8080/v1",
        },
    ),
    (
        "allow-list-membership-skipped",
        ("    if host not in allowed:\n", "    if False:\n"),
        _Stub(True, HOST),
        {"llm_allowed_hosts": OTHER},
    ),
    (
        "provider-host-comparison-skipped",
        (
            "    if host != provider_host or host != url_host:\n",
            "    if host != url_host:\n",
        ),
        _Stub(True, OTHER),
        {},
    ),
    (
        "url-host-equality-skipped",
        (
            "    if host != provider_host or host != url_host:\n",
            "    if host != provider_host:\n",
        ),
        _Stub(True, HOST),
        {"llm_base_url": OTHER_URL},
    ),
    (
        "host-equality-skipped",
        ("    if host != provider_host or host != url_host:\n", "    if False:\n"),
        _Stub(True, OTHER),
        {"llm_base_url": OTHER_URL},
    ),
    (
        "fake-exemption-first",
        (
            "    if _knowledge_host_set(settings):\n"
            "        return _check_knowledge_host(provider, settings)\n"
            "    if type(provider) is FakeProvider:\n"
            '        return "none"\n',
            "    if type(provider) is FakeProvider:\n"
            '        return "none"\n'
            "    if _knowledge_host_set(settings):\n"
            "        return _check_knowledge_host(provider, settings)\n",
        ),
        FakeProvider(),
        {},
    ),
    (
        "knowledge-check-skipped",
        (
            "    if _knowledge_host_set(settings):\n"
            "        return _check_knowledge_host(provider, settings)\n",
            "",
        ),
        _Stub(True, HOST),
        {"llm_base_url": OTHER_URL},
    ),
]


@pytest.mark.parametrize(
    ("edit", "provider", "update"),
    [pytest.param(e, p, u, id=i) for i, e, p, u in EGRESS_MUTATIONS],
)
def test_mutation_of_check_egress_is_caught(
    edit: tuple[str, str], provider: Any, update: dict[str, Any]
) -> None:
    provider = provider if provider is not None else _provider()
    settings = _construct(**update)
    assert _refused(egress_module, provider, settings), "the real module must refuse"
    mutant = mutated_module(egress_module, edit)
    assert not _refused(mutant, provider, settings), "the mutant is not caught"


def test_a_base_url_without_a_host_is_refused() -> None:
    """The `None` check is backed by the equality check (a str never equals None), so dropping it
    alone is an equivalent mutant; the behaviour is pinned here instead."""
    from divesafe.config.settings import base_url_host

    assert base_url_host("https://llm.example.org@/v1") is None
    with pytest.raises(RuntimeError) as caught:
        check_egress(_Stub(True, HOST), _construct(llm_base_url="https://llm.example.org@/v1"))
    assert str(caught.value) == R_MISMATCH


SETTINGS_MUTATIONS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "declared-local",
        '            raise ValueError(f"{name} cannot be used with DIVESAFE_LLM_SERVER_IS_LOCAL")\n',  # noqa: E501
        {"allow_external_llm": False, "llm_server_is_local": True},
    ),
    (
        "no-opt-in",
        '            raise ValueError(f"{name} needs DIVESAFE_ALLOW_EXTERNAL_LLM=true")\n',
        {"allow_external_llm": False},
    ),
    (
        "no-server",
        '            raise ValueError(f"{name} needs a model server provider and '
        'DIVESAFE_LLM_BASE_URL")\n',
        {"llm_provider": "fake"},
    ),
    (
        "malformed",
        '            raise ValueError(f"{name} must be exactly one hostname, not a loopback name")\n',  # noqa: E501
        {
            "llm_knowledge_host": "localhost",
            "llm_allowed_hosts": "localhost",
            "llm_base_url": "http://localhost:8080/v1",
        },
    ),
    (
        "not-allowed",
        '            raise ValueError(f"{name} must be one of DIVESAFE_LLM_ALLOWED_HOSTS")\n',
        {"llm_allowed_hosts": OTHER},
    ),
    (
        "mismatch",
        '            raise ValueError(f"{name} must be the host of DIVESAFE_LLM_BASE_URL")\n',
        {"llm_base_url": OTHER_URL},
    ),
    ("check-not-called", "        self._check_knowledge_host()\n", {"llm_base_url": OTHER_URL}),
]


@pytest.mark.parametrize(
    ("target", "overrides"),
    [pytest.param(t, o, id=i) for i, t, o in SETTINGS_MUTATIONS],
)
def test_mutation_of_settings_is_caught(target: str, overrides: dict[str, Any]) -> None:
    """The mutant accepts what the real class refuses, or refuses it with another message, which
    `test_settings_refuses_with_a_fixed_message` catches. Only `declared-local` is the second
    kind: IS_LOCAL with the opt-in is refused earlier ("contradict"), so the opt-in refusal always
    stands behind it; `check_egress` covers it on its own (`model_construct`)."""
    with pytest.raises(ValidationError) as real:
        _settings(**overrides)
    indent = target[: len(target) - len(target.lstrip())]
    mutant = mutated_module(settings_module, (target, f"{indent}pass\n"))
    try:
        mutant.Settings(_env_file=None, **{**VALID, **overrides})
    except ValidationError as mutated:
        assert mutated.errors()[0]["msg"] != real.value.errors()[0]["msg"]
        assert target.strip() == SETTINGS_MUTATIONS[0][1].strip(), "only declared-local may refuse"


@pytest.mark.parametrize(
    ("edits", "value"),
    [
        pytest.param(
            (
                ('if value != value.strip().lower() or "," in value', 'if "," in value'),
                ("    if hosts != frozenset({value}):\n        return False\n", ""),
            ),
            "LLM.example.org",
            id="case-and-round-trip-dropped",
        ),
        pytest.param(
            (
                ('or "," in value or', "or"),
                ("    if hosts != frozenset({value}):\n        return False\n", ""),
            ),
            f"{HOST},{OTHER}",
            id="comma-and-round-trip-dropped",
        ),
        pytest.param(
            (('return value != "localhost" and not value.endswith(".localhost")', "return True"),),
            "localhost",
            id="loopback-name-allowed",
        ),
        pytest.param(
            (('return value != "localhost" and not value.endswith(".localhost")', "return True"),),
            "a.localhost",
            id="sub-localhost-allowed",
        ),
    ],
)
def test_mutation_of_is_single_knowledge_host_is_caught(
    edits: tuple[tuple[str, str], ...], value: str
) -> None:
    """The strip, comma and trailing-dot guards each repeat what the `_host_set` round-trip already
    refuses (each alone is an equivalent mutant), so they are dropped in pairs here."""
    assert is_single_knowledge_host(value) is False
    mutant = mutated_module(settings_module, *edits)
    assert mutant.is_single_knowledge_host(value) is True


# ---------------------------------------------------------------- start-up attestation check


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]], log: list[str]) -> None:
        self._rows = rows
        self.log = log
        self.read_only = False

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.log.append("close")

    def execute(self, sql: str) -> _FakeCursor:
        self.log.append(f"read_only={self.read_only}:{sql.strip().split()[0]}")
        self.log.append(sql)
        return _FakeCursor(self._rows)

    def commit(self) -> None:
        self.log.append("commit")

    def rollback(self) -> None:
        self.log.append("rollback")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """`psycopg.connect` as `kb_attestation` sees it, answering with `rows` (or raising)."""
    state: dict[str, Any] = {"rows": [(0,)], "raise": None, "log": [], "dsns": []}

    def connect(dsn: str, **kwargs: Any) -> _FakeConnection:
        state["dsns"].append(dsn)
        if state["raise"] is not None:
            raise state["raise"]
        return _FakeConnection(state["rows"], state["log"])

    monkeypatch.setattr(kb_attestation.psycopg, "connect", connect)
    yield state


def _db_settings(**overrides: Any) -> Settings:
    return _settings(database_url=DSN, **overrides)


CONNECTOR = object()


def test_start_up_passes_when_every_approved_version_attests(store: dict[str, Any]) -> None:
    store["rows"] = [(0,)]
    check_knowledge_attestations(_db_settings(), "remote_knowledge_opt_in", CONNECTOR)  # type: ignore[arg-type]
    assert store["dsns"] == [DSN]
    # Read-only, and the count query runs after read_only is set.
    assert any(e.startswith("read_only=True:SELECT") for e in store["log"])
    assert "rollback" in store["log"] and store["log"][-1] == "close"


@pytest.mark.parametrize("count", [1, 2, 10_000])
def test_start_up_refuses_while_a_version_is_unattested(
    store: dict[str, Any], count: int, caplog: pytest.LogCaptureFixture
) -> None:
    store["rows"] = [(count,)]
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as caught:
        check_knowledge_attestations(_db_settings(), "remote_knowledge_opt_in", CONNECTOR)  # type: ignore[arg-type]
    assert str(caught.value) == UNATTESTED
    assert caught.value.__cause__ is None
    assert DSN not in repr([vars(r) for r in caplog.records])


def test_start_up_refuses_when_the_store_cannot_be_read(
    store: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    store["raise"] = psycopg.OperationalError("connection to db.invalid failed: secret-detail-17")
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as caught:
        check_knowledge_attestations(_db_settings(), "remote_knowledge_opt_in", CONNECTOR)  # type: ignore[arg-type]
    assert str(caught.value) == UNREADABLE
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    logged = repr([vars(r) for r in caplog.records])
    assert "secret-detail-17" not in logged and DSN not in logged


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="no-row"),
        pytest.param([(0,), (0,)], id="two-rows"),
        pytest.param([(0, 0)], id="two-columns"),
        pytest.param([()], id="no-column"),
        pytest.param([(None,)], id="null"),
        pytest.param([(False,)], id="bool-false"),
        pytest.param([(True,)], id="bool-true"),
        pytest.param([(-1,)], id="negative"),
        pytest.param([("0",)], id="string"),
        pytest.param([(0.0,)], id="float"),
    ],
)
def test_start_up_refuses_a_malformed_answer(store: dict[str, Any], rows: list[Any]) -> None:
    store["rows"] = rows
    with pytest.raises(RuntimeError) as caught:
        check_knowledge_attestations(_db_settings(), "remote_knowledge_opt_in", CONNECTOR)  # type: ignore[arg-type]
    assert str(caught.value) == MALFORMED_ANSWER


def test_start_up_refuses_without_a_knowledge_store(store: dict[str, Any]) -> None:
    store["raise"] = AssertionError("must not connect")
    with pytest.raises(RuntimeError) as caught:
        check_knowledge_attestations(_db_settings(), "remote_knowledge_opt_in", None)
    assert str(caught.value) == NO_STORE
    with pytest.raises(RuntimeError) as caught:
        check_knowledge_attestations(_settings(), "remote_knowledge_opt_in", CONNECTOR)  # type: ignore[arg-type]
    assert str(caught.value) == NO_STORE


@pytest.mark.parametrize(
    "basis",
    [
        "remote_external_opt_in",
        "declared_local",
        "loopback_external_opt_in",
        "none",
        "no_agent_stage",
    ],
)
def test_start_up_is_unaffected_when_the_variable_is_absent(
    store: dict[str, Any], basis: str
) -> None:
    store["raise"] = AssertionError("must not connect")
    settings = _db_settings(llm_knowledge_host=None)
    check_knowledge_attestations(settings, basis, CONNECTOR)  # type: ignore[arg-type]
    check_knowledge_attestations(settings, basis, None)
    assert store["dsns"] == []


def test_the_setting_alone_or_the_basis_alone_turns_the_check_on() -> None:
    with_host = _construct(llm_knowledge_host=HOST)
    without = _construct(llm_knowledge_host=None)
    assert knowledge_goes_to_model_host(with_host, "remote_external_opt_in") is True
    assert knowledge_goes_to_model_host(with_host, None) is True
    assert knowledge_goes_to_model_host(without, "remote_knowledge_opt_in") is True
    for basis in (
        "remote_external_opt_in",
        "REMOTE_KNOWLEDGE_OPT_IN",
        "remote_knowledge_opt_in ",
        None,
        b"remote_knowledge_opt_in",
        ["remote_knowledge_opt_in"],
    ):
        assert knowledge_goes_to_model_host(without, basis) is False, basis


def test_the_attestation_query_has_no_date_or_site_condition() -> None:
    """A future-dated or expired unattested version counts too (ADR 0028 decision 7). The live
    check of that is in tests/integration (skipped without a test database)."""
    sql = kb_attestation.UNATTESTED_SQL.lower()
    for absent in (
        "effective",
        "review_by",
        "site",
        "now(",
        "current_date",
        "current_timestamp",
        "%s",
        "%(",
    ):
        assert absent not in sql, absent
    assert "attests_model_host_egress is not true" in sql
    assert "withdrawn" in sql and "'approved'" in sql
    assert "count(*)" in sql


# ---------------------------------------------------------------- start-up mutations


def _check_with(module: Any, settings: Settings, basis: str) -> str | None:
    try:
        module.check_knowledge_attestations(settings, basis, CONNECTOR)
    except RuntimeError as refused:
        return str(refused)
    return None


@pytest.mark.parametrize(
    ("edit", "store_state", "settings_overrides", "basis", "expected"),
    [
        pytest.param(
            (
                "    if not knowledge_goes_to_model_host(settings, basis):\n        return\n",
                "    return\n",
            ),
            {"rows": [(1,)]},
            {},
            "remote_knowledge_opt_in",
            UNATTESTED,
            id="check-skipped",
        ),
        pytest.param(
            (
                "        raise RuntimeError(str(refused)) from None\n",
                "        pass\n",
            ),
            {"raise": psycopg.OperationalError("down")},
            {},
            "remote_knowledge_opt_in",
            UNREADABLE,
            id="refusal-swallowed",
        ),
        pytest.param(
            ("    ) or bool(settings.llm_knowledge_host)\n", "    )\n"),
            {"rows": [(1,)]},
            {},
            "remote_external_opt_in",
            UNATTESTED,
            id="keyed-on-the-basis-only",
        ),
        pytest.param(
            (
                "        isinstance(basis, str) and basis == "
                "RecordEgressBasis.REMOTE_KNOWLEDGE_OPT_IN.value\n"
                "    ) or bool(settings.llm_knowledge_host)\n",
                "        False\n    ) or bool(settings.llm_knowledge_host)\n",
            ),
            {"rows": [(1,)]},
            {"llm_knowledge_host": None},
            "remote_knowledge_opt_in",
            UNATTESTED,
            id="keyed-on-the-setting-only",
        ),
    ],
)
def test_mutation_of_the_start_up_check_is_caught(
    store: dict[str, Any],
    edit: tuple[str, str],
    store_state: dict[str, Any],
    settings_overrides: dict[str, Any],
    basis: str,
    expected: str,
) -> None:
    store.update(store_state)
    settings = _db_settings(**settings_overrides)
    assert _check_with(state_module, settings, basis) == expected
    mutant = mutated_module(state_module, edit)
    assert _check_with(mutant, settings, basis) is None


def test_mutation_passing_when_the_store_is_unreadable_is_caught(store: dict[str, Any]) -> None:
    store["raise"] = psycopg.OperationalError("down")
    with pytest.raises(kb_attestation.AttestationRefused):
        kb_attestation.check_attestations(DSN)
    mutant = mutated_module(
        kb_attestation,
        ("        raise AttestationRefused(STORE_UNREADABLE) from None\n", "        return\n"),
    )
    mutant.check_attestations(DSN)


def test_mutation_accepting_a_malformed_count_is_caught(store: dict[str, Any]) -> None:
    store["rows"] = [(True,)]
    with pytest.raises(kb_attestation.AttestationRefused):
        kb_attestation.check_attestations(DSN)
    mutant = mutated_module(
        kb_attestation,
        ("    if type(value) is not int or value < 0:\n", "    if value < 0:\n"),
    )
    # bool is an int subclass; True would count as 1, so the mutant refuses for the wrong
    # reason, and a False slips through as 0.
    store["rows"] = [(False,)]
    with pytest.raises(kb_attestation.AttestationRefused):
        kb_attestation.check_attestations(DSN)
    mutant.check_attestations(DSN)


# ---------------------------------------------------------------- build_default_state


def _start(module: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """`build_default_state` with an in-memory repository (no live database)."""
    monkeypatch.setattr(module, "build_repository", lambda s: InMemoryAssessmentRepository())
    return module.build_default_state(_db_settings())


def test_build_default_state_refuses_while_a_version_is_unattested(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store["rows"] = [(1,)]
    with pytest.raises(RuntimeError) as caught:
        _start(state_module, monkeypatch)
    assert str(caught.value) == UNATTESTED
    store["rows"] = [(0,)]
    started = _start(state_module, monkeypatch)
    assert started.egress_basis == "remote_knowledge_opt_in"
    assert started.model_host == HOST
    started.provider.close()


def test_build_default_state_without_a_database_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError) as caught:
        state_module.build_default_state(_settings())
    assert str(caught.value) == NO_STORE


def test_mutation_build_default_state_skipping_the_check_is_caught(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store["rows"] = [(1,)]
    with pytest.raises(RuntimeError, match="does not attest"):
        _start(state_module, monkeypatch)
    mutant = mutated_module(
        state_module,
        ("    check_knowledge_attestations(settings, basis, knowledge_connector)\n", ""),
    )
    started = _start(mutant, monkeypatch)
    assert started.egress_basis == "remote_knowledge_opt_in"
    started.provider.close()


# ---------------------------------------------------------------- build_provider and its log


_URL_LEAKS = ("secret-path-29", "8443", "https://")


def _knowledge_log_leaks(module: Any, caplog: pytest.LogCaptureFixture) -> list[str]:
    """Runs `module.build_provider` with a URL holding a port and a path, checks the basis and the
    logged host, and returns the URL parts found in any captured log record."""
    url = "https://llm.example.org:8443/v1/secret-path-29"
    caplog.clear()
    with caplog.at_level("WARNING"):
        provider, basis = module.build_provider(_settings(llm_base_url=url))
    assert provider is not None
    provider.close()
    assert basis == "remote_knowledge_opt_in"
    record = next(
        r for r in caplog.records if r.getMessage() == "knowledge is sent to the model host"
    )
    assert record.model_host == HOST
    assert record.egress_basis == "remote_knowledge_opt_in"
    logged = scanned_records(caplog.records)
    return [leak for leak in _URL_LEAKS if leak in logged]


def test_build_provider_gives_the_knowledge_basis_and_logs_the_host_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _knowledge_log_leaks(state_module, caplog) == []


_KNOWLEDGE_EXTRA = 'extra={"model_host": model_host(settings), "egress_basis": basis},'


@pytest.mark.parametrize(
    ("logged_host", "found"),
    [
        ('model_host(settings) + ":8443"', ["8443"]),
        ("str(settings.llm_base_url)", list(_URL_LEAKS)),
    ],
)
def test_the_knowledge_log_scan_catches_a_logged_port_or_url(
    caplog: pytest.LogCaptureFixture, logged_host: str, found: list[str]
) -> None:
    """Mutation check: a copy of the code that logs `host:8443`, or the whole URL, as the
    knowledge log's `model_host` is caught by the same scan (the real module is not changed).
    The positive `model_host == HOST` check is skipped here, as the mutant changes it."""
    mutant = mutated_module(
        state_module,
        (_KNOWLEDGE_EXTRA, _KNOWLEDGE_EXTRA.replace("model_host(settings)", logged_host)),
    )
    url = "https://llm.example.org:8443/v1/secret-path-29"
    caplog.clear()
    with caplog.at_level("WARNING"):
        provider, _ = mutant.build_provider(_settings(llm_base_url=url))
    provider.close()
    logged = scanned_records(caplog.records)
    assert [leak for leak in _URL_LEAKS if leak in logged] == found


def test_build_provider_without_the_host_does_not_log_knowledge_egress(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        provider, basis = build_provider(_settings(llm_knowledge_host=None))
    assert provider is not None
    provider.close()  # type: ignore[attr-defined]
    assert basis == "remote_external_opt_in"
    assert not any("knowledge is sent" in r.getMessage() for r in caplog.records)


def test_build_provider_refuses_a_host_mismatch_through_check_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings that pass validation, with a provider calling another host."""
    monkeypatch.setattr(state_module, "create_provider", lambda s: _provider(OTHER_URL))
    with pytest.raises(RuntimeError) as caught:
        build_provider(_settings())
    assert str(caught.value) == R_MISMATCH
