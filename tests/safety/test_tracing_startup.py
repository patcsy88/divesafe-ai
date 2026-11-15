"""ADR 0027 amendment, start-up refusals for tracing to the local Langfuse.

Tracing needs both `DIVESAFE_TRACING_ENABLED=true` and `DIVESAFE_TRACE_ENDPOINT`; either alone
refuses. The endpoint is exactly `http://<private IPv4 literal>:<port>/api/public/otel/v1/traces`.
With tracing on, any `OTEL_*` variable refuses, and the container must have an interface in the
endpoint's /24 (a UDP connect, no packet sent; an `OSError` is the same refusal). With tracing off
and no endpoint, start-up succeeds whatever the subnet or `OTEL_*` variables, and no socket is
created. Refusals are fixed text and never echo a value. Messages are literals here, never
imported from the code, so a code change cannot move the test.
"""

from __future__ import annotations

import socket
import sys
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from tests.safety.kb_fakes import mutated_module

from divesafe.api import state as state_module
from divesafe.api import tracing as tracing_module
from divesafe.config import Settings
from divesafe.config import settings as settings_module

pytestmark = pytest.mark.safety

R_PAIR = "DIVESAFE_TRACING_ENABLED and DIVESAFE_TRACE_ENDPOINT must be set together"
R_ENDPOINT = (
    "DIVESAFE_TRACE_ENDPOINT must be exactly "
    "http://<private IPv4 address>:<port>/api/public/otel/v1/traces"
)
R_KEYS = (
    "tracing needs both Langfuse production project keys (secret files divesafe_trace_public_key "
    "and divesafe_trace_secret_key)"
)
R_OTEL = (
    "refusing to start: an OTEL_* variable is set while DIVESAFE_TRACING_ENABLED=true; the "
    "environment must not change where spans go or what they hold (ADR 0027)"
)
R_SUBNET = (
    "refusing to start: this container has no interface in the /24 of DIVESAFE_TRACE_ENDPOINT, "
    "so it is not on the divesafe-trace network (ADR 0027)"
)

ADDRESS = "172.30.77.10"
ENDPOINT = f"http://{ADDRESS}:3000/api/public/otel/v1/traces"
KEYS = {"trace_public_key": "pk-lf-0000", "trace_secret_key": "sk-lf-0000"}
ON = {"tracing_enabled": True, "trace_endpoint": ENDPOINT, **KEYS}
SENTINEL = "zqxsentinel"


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type, call-arg]


def _construct(**overrides: Any) -> Settings:
    """Skips every validator, as `model_copy(update=...)` would."""
    defaults = Settings(_env_file=None).model_dump()  # type: ignore[call-arg]
    return Settings.model_construct(**{**defaults, **overrides})


def _refusal(**overrides: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        _settings(**overrides)
    messages = [str(e["msg"]) for e in caught.value.errors()]
    assert len(messages) == 1, messages
    assert SENTINEL not in str(caught.value)
    return messages[0].removeprefix("Value error, ")


class _Probe:
    """A UDP socket stand-in: records each creation and returns a chosen local address."""

    created: list[tuple[int, int]] = []
    local = "172.30.77.2"
    fail: bool | str = False

    def __init__(self, family: int = -1, kind: int = -1, *rest: Any) -> None:
        _Probe.created.append((family, kind))

    def __enter__(self) -> _Probe:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def connect_ex(self, target: tuple[str, int]) -> int:
        if _Probe.fail == "raise":
            raise OSError("Network is unreachable " + SENTINEL)
        return 101 if _Probe.fail else 0

    def getsockname(self) -> tuple[str, int]:
        return (_Probe.local, 40000)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> type[_Probe]:
    _Probe.created = []
    _Probe.local = "172.30.77.2"
    _Probe.fail = False
    monkeypatch.setattr(socket, "socket", _Probe)
    return _Probe


# ---------------------------------------------------------------- settings: the pair


def test_tracing_enabled_without_an_endpoint_is_refused() -> None:
    assert _refusal(tracing_enabled=True, **KEYS) == R_PAIR


def test_an_endpoint_without_the_flag_is_refused() -> None:
    assert _refusal(trace_endpoint=ENDPOINT) == R_PAIR
    assert _refusal(trace_endpoint=ENDPOINT + SENTINEL) == R_PAIR


def test_an_empty_endpoint_with_the_flag_is_refused() -> None:
    s = _construct(**{**ON, "trace_endpoint": ""})
    with pytest.raises(RuntimeError) as caught:
        tracing_module.check_tracing_startup(s, {})
    assert str(caught.value) == f"refusing to start: {R_PAIR}"


def test_the_default_is_off_and_accepted() -> None:
    s = _settings()
    assert s.tracing_enabled is False and s.trace_endpoint is None
    assert s.trace_public_key is None and s.trace_secret_key is None


def test_the_fixed_form_with_both_keys_is_accepted() -> None:
    s = _settings(**ON)
    assert s.tracing_enabled is True and s.trace_endpoint == ENDPOINT


@pytest.mark.parametrize(
    "missing", ["trace_public_key", "trace_secret_key"], ids=["no-public", "no-secret"]
)
def test_tracing_without_both_keys_is_refused(missing: str) -> None:
    overrides = {k: v for k, v in ON.items() if k != missing}
    assert _refusal(**overrides) == R_KEYS


@pytest.mark.parametrize("field", ["trace_public_key", "trace_secret_key"])
def test_a_key_of_the_wrong_shape_is_refused_without_echo(field: str) -> None:
    message = _refusal(**{**ON, field: f"pk {SENTINEL}\n"})
    assert message == "a Langfuse project key has an unexpected shape"


@pytest.mark.parametrize("field", ["trace_public_key", "trace_secret_key"])
def test_a_key_of_the_wrong_shape_is_accepted_while_tracing_is_off(field: str) -> None:
    """An unused feature never stops the service: a key prepared in advance is not checked."""
    _settings(**{field: f'"pk {SENTINEL}"'})
    tracing_module.check_tracing_startup(_settings(**{field: f'"pk {SENTINEL}"'}), {})


@pytest.mark.parametrize("field", ["trace_public_key", "trace_secret_key"])
def test_the_start_up_check_repeats_the_key_shape_check(field: str) -> None:
    s = _construct(**{**ON, **{k: SecretStr(v) for k, v in KEYS.items()}, field: SecretStr("k x")})
    with pytest.raises(RuntimeError) as caught:
        tracing_module.check_tracing_startup(s, {})
    assert str(caught.value) == "refusing to start: a Langfuse project key has an unexpected shape"


def test_a_key_that_is_not_a_secret_is_refused_at_start_up() -> None:
    s = _construct(**{**ON, "trace_public_key": "pk-lf-0", "trace_secret_key": SecretStr("sk")})
    with pytest.raises(RuntimeError) as caught:
        tracing_module.check_tracing_startup(s, {})
    assert str(caught.value) == "refusing to start: a Langfuse project key has an unexpected shape"


# ---------------------------------------------------------------- settings: the endpoint form

BAD_ENDPOINTS = {
    "host-name": f"http://langfuse-{SENTINEL}:3000/api/public/otel/v1/traces",
    "localhost": "http://localhost:3000/api/public/otel/v1/traces",
    "https": f"https://{ADDRESS}:3000/api/public/otel/v1/traces",
    "upper-scheme": f"HTTP://{ADDRESS}:3000/api/public/otel/v1/traces",
    "no-port": f"http://{ADDRESS}/api/public/otel/v1/traces",
    "port-zero": f"http://{ADDRESS}:0/api/public/otel/v1/traces",
    "port-leading-zero": f"http://{ADDRESS}:03000/api/public/otel/v1/traces",
    "port-too-big": f"http://{ADDRESS}:65536/api/public/otel/v1/traces",
    "other-path": f"http://{ADDRESS}:3000/api/public/ingestion",
    "trailing-slash": f"http://{ADDRESS}:3000/api/public/otel/v1/traces/",
    "query": f"http://{ADDRESS}:3000/api/public/otel/v1/traces?{SENTINEL}=1",
    "fragment": f"http://{ADDRESS}:3000/api/public/otel/v1/traces#{SENTINEL}",
    "credentials": f"http://{SENTINEL}:pw@{ADDRESS}:3000/api/public/otel/v1/traces",
    "public": "http://8.8.8.8:3000/api/public/otel/v1/traces",
    "loopback": "http://127.0.0.1:3000/api/public/otel/v1/traces",
    "link-local": "http://169.254.1.10:3000/api/public/otel/v1/traces",
    "cgnat": "http://100.64.1.10:3000/api/public/otel/v1/traces",
    "outside-172-16": "http://172.32.1.10:3000/api/public/otel/v1/traces",
    "network-address": "http://172.30.77.0:3000/api/public/otel/v1/traces",
    "gateway-address": "http://172.30.77.1:3000/api/public/otel/v1/traces",
    "broadcast-address": "http://172.30.77.255:3000/api/public/otel/v1/traces",
    "leading-zero-octet": "http://172.30.077.10:3000/api/public/otel/v1/traces",
    "short-form": "http://10.1:3000/api/public/otel/v1/traces",
    "octet-too-big": "http://10.1.2.300:3000/api/public/otel/v1/traces",
    "ipv6": "http://[fd00::10]:3000/api/public/otel/v1/traces",
    "mapped-ipv6": f"http://[::ffff:{ADDRESS}]:3000/api/public/otel/v1/traces",
    "space": f" http://{ADDRESS}:3000/api/public/otel/v1/traces",
    "newline": f"http://{ADDRESS}:3000/api/public/otel/v1/traces\n",
    "backslash": f"http://{ADDRESS}:3000\\api/public/otel/v1/traces",
    "dot-segment": f"http://{ADDRESS}:3000/api/public/otel/v1/../v1/traces",
    "encoded-path": f"http://{ADDRESS}:3000/api/public/otel/v1/%74races",
}


@pytest.mark.parametrize("value", list(BAD_ENDPOINTS.values()), ids=list(BAD_ENDPOINTS))
def test_an_endpoint_not_in_the_fixed_form_is_refused_without_echo(value: str) -> None:
    assert _refusal(**{**ON, "trace_endpoint": value}) == R_ENDPOINT


@pytest.mark.parametrize("value", list(BAD_ENDPOINTS.values()), ids=list(BAD_ENDPOINTS))
def test_the_start_up_check_repeats_the_form_check(value: str, probe: type[_Probe]) -> None:
    with pytest.raises(RuntimeError) as caught:
        tracing_module.check_tracing_startup(_construct(**{**ON, "trace_endpoint": value}), {})
    assert str(caught.value) == f"refusing to start: {R_ENDPOINT}"
    assert probe.created == [], "no socket before the value check passes"


@pytest.mark.parametrize(
    "value",
    [
        "http://10.0.0.2:1/api/public/otel/v1/traces",
        "http://172.16.0.254:65535/api/public/otel/v1/traces",
        "http://172.31.255.10:3000/api/public/otel/v1/traces",
        "http://192.168.200.2:4318/api/public/otel/v1/traces",
    ],
)
def test_each_private_range_is_accepted(value: str) -> None:
    assert _settings(**{**ON, "trace_endpoint": value}).trace_endpoint == value


def test_the_start_up_check_repeats_the_pair_and_key_checks() -> None:
    for overrides, message in (
        ({"tracing_enabled": True}, R_PAIR),
        ({"trace_endpoint": ENDPOINT}, R_PAIR),
        ({"tracing_enabled": True, "trace_endpoint": ENDPOINT}, R_KEYS),
    ):
        with pytest.raises(RuntimeError) as caught:
            tracing_module.check_tracing_startup(_construct(**overrides), {})
        assert str(caught.value) == f"refusing to start: {message}"


# ---------------------------------------------------------------- tracing on: OTEL_* and subnet


@pytest.mark.parametrize(
    "name", ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SDK_DISABLED", "otel_bsp_max_queue_size"]
)
def test_any_otel_variable_is_refused_while_tracing_is_on(name: str, probe: type[_Probe]) -> None:
    with pytest.raises(RuntimeError) as caught:
        tracing_module.check_tracing_startup(_settings(**ON), {name: f"http://{SENTINEL}"})
    assert str(caught.value) == R_OTEL
    assert SENTINEL not in str(caught.value) and name not in str(caught.value)


def test_an_empty_otel_variable_is_still_refused(probe: type[_Probe]) -> None:
    with pytest.raises(RuntimeError, match="OTEL_"):
        tracing_module.check_tracing_startup(_settings(**ON), {"OTEL_SERVICE_NAME": ""})


def test_an_interface_in_the_subnet_starts(probe: type[_Probe]) -> None:
    tracing_module.check_tracing_startup(_settings(**ON), {"DIVESAFE_X": "1"})
    assert probe.created == [(socket.AF_INET, socket.SOCK_DGRAM)]


@pytest.mark.parametrize(
    "local", ["192.168.65.3", "172.30.78.2", "0.0.0.0", ADDRESS, "not-an-address"]
)
def test_no_interface_in_the_subnet_is_refused(local: str, probe: type[_Probe]) -> None:
    probe.local = local
    with pytest.raises(RuntimeError) as caught:
        tracing_module.check_tracing_startup(_settings(**ON), {})
    assert str(caught.value) == R_SUBNET
    assert ADDRESS not in str(caught.value)


@pytest.mark.parametrize("fail", [True, "raise"], ids=["error-number", "oserror"])
def test_a_failed_connect_is_the_same_refusal(probe: type[_Probe], fail: bool | str) -> None:
    probe.fail = fail
    with pytest.raises(RuntimeError) as caught:
        tracing_module.check_tracing_startup(_settings(**ON), {})
    assert str(caught.value) == R_SUBNET
    assert caught.value.__cause__ is None and SENTINEL not in str(caught.value)


def test_build_default_state_runs_the_check(probe: type[_Probe]) -> None:
    probe.local = "192.168.65.3"
    with pytest.raises(RuntimeError) as caught:
        state_module.build_default_state(_settings(**ON))
    assert str(caught.value) == R_SUBNET


# ---------------------------------------------------------------- tracing off


OTEL_SET = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://203.0.113.9:4318", "OTEL_SDK_DISABLED": "1"}


def _off_starts(module: Any, monkeypatch: pytest.MonkeyPatch, probe: type[_Probe]) -> bool:
    """True when start-up with tracing off succeeds with `OTEL_*` set and a failing subnet,
    creating no socket and importing no OpenTelemetry module."""
    for name, value in OTEL_SET.items():
        monkeypatch.setenv(name, value)
    probe.fail = True
    before = {m for m in sys.modules if m.startswith("opentelemetry")}
    try:
        module.check_tracing_startup(_settings(), dict(OTEL_SET))
    except RuntimeError:
        return False
    after = {m for m in sys.modules if m.startswith("opentelemetry")}
    return probe.created == [] and after == before


def test_tracing_off_starts_with_otel_set_and_creates_no_socket(
    monkeypatch: pytest.MonkeyPatch, probe: type[_Probe]
) -> None:
    assert _off_starts(tracing_module, monkeypatch, probe)


def test_tracing_off_build_default_state_creates_no_socket(
    monkeypatch: pytest.MonkeyPatch, probe: type[_Probe]
) -> None:
    for name, value in OTEL_SET.items():
        monkeypatch.setenv(name, value)
    probe.fail = True
    state_module.build_default_state(_settings())
    assert probe.created == []


def test_mutation_running_the_otel_refusal_unconditionally_is_caught(
    monkeypatch: pytest.MonkeyPatch, probe: type[_Probe]
) -> None:
    mutant = mutated_module(
        tracing_module, ("    if not settings.tracing_enabled:\n        return\n", "")
    )
    assert not _off_starts(mutant, monkeypatch, probe)


def test_mutation_running_the_subnet_check_unconditionally_is_caught(
    monkeypatch: pytest.MonkeyPatch, probe: type[_Probe]
) -> None:
    mutant = mutated_module(
        tracing_module,
        (
            "    if not settings.tracing_enabled:\n",
            "    _has_interface_in_subnet(ipaddress.IPv4Address('172.30.77.10'), 3000)\n"
            "    if not settings.tracing_enabled:\n",
        ),
    )
    assert not _off_starts(mutant, monkeypatch, probe)


# ---------------------------------------------------------------- mutations of the refusals


def test_mutation_dropping_the_otel_refusal_is_caught(probe: type[_Probe]) -> None:
    mutant = mutated_module(
        tracing_module,
        ('    if any(name.upper().startswith("OTEL_") for name in environ):\n', "    if False:\n"),
    )
    mutant.check_tracing_startup(_settings(**ON), {"OTEL_EXPORTER_OTLP_ENDPOINT": "x"})


def test_mutation_dropping_the_subnet_comparison_is_caught(probe: type[_Probe]) -> None:
    probe.local = "192.168.65.3"
    mutant = mutated_module(
        tracing_module, ("    return local in subnet and local != address\n", "    return True\n")
    )
    mutant.check_tracing_startup(_settings(**ON), {})


def test_mutation_letting_an_oserror_through_as_success_is_caught(probe: type[_Probe]) -> None:
    probe.fail = "raise"
    mutant = mutated_module(
        tracing_module,
        (
            "    except OSError:\n        return False\n",
            "    except OSError:\n        return True\n",
        ),
    )
    mutant.check_tracing_startup(_settings(**ON), {})


def test_mutation_ignoring_a_failed_connect_is_caught(probe: type[_Probe]) -> None:
    probe.fail = True
    mutant = mutated_module(
        tracing_module,
        (
            "            if probe.connect_ex((str(address), port)) != 0:\n"
            "                return False\n",
            "            probe.connect_ex((str(address), port))\n",
        ),
    )
    mutant.check_tracing_startup(_settings(**ON), {})


def test_mutation_dropping_the_repeated_settings_check_is_caught(probe: type[_Probe]) -> None:
    mutant = mutated_module(
        tracing_module,
        (
            "    if refusal is not None:\n        raise RuntimeError",
            "    if False:\n        raise RuntimeError",
        ),
    )
    mutant.check_tracing_startup(_construct(trace_endpoint=ENDPOINT), {})


def test_mutation_dropping_the_start_up_call_is_caught(probe: type[_Probe]) -> None:
    probe.local = "192.168.65.3"
    mutant = mutated_module(state_module, ("    check_tracing_startup(settings, os.environ)\n", ""))
    mutant.build_default_state(_settings(**ON))


SETTINGS_MUTATIONS = {
    "pair": (
        "    if settings.tracing_enabled != has_endpoint:\n",
        "    if False:\n",
        {"trace_endpoint": ENDPOINT},
    ),
    "form": (
        "    if trace_endpoint_address(endpoint) is None:\n",
        "    if False:\n",
        {**ON, "trace_endpoint": "http://langfuse:3000/api/public/otel/v1/traces"},
    ),
    "keys": (
        "    if settings.trace_public_key is None or settings.trace_secret_key is None:\n",
        "    if False:\n",
        {"tracing_enabled": True, "trace_endpoint": ENDPOINT},
        "a Langfuse project key has an unexpected shape",
    ),
    "key-shape": (
        "        if not isinstance(key, SecretStr) or _TRACE_KEY.fullmatch(key.get_secret_value()) "
        "is None:\n",
        "        if False:\n",
        {**ON, "trace_public_key": "pk bad"},
    ),
    "private-range": (
        "    if not any(address in network for network in _TRACE_RANGES):\n",
        "    if False:\n",
        {**ON, "trace_endpoint": "http://8.8.8.8:3000/api/public/otel/v1/traces"},
    ),
    "edge-address": (
        "    if address.packed[-1] in (0, 1, 255):\n",
        "    if False:\n",
        {**ON, "trace_endpoint": "http://10.1.2.255:3000/api/public/otel/v1/traces"},
    ),
    "gateway-only": (
        "    if address.packed[-1] in (0, 1, 255):\n",
        "    if address.packed[-1] in (0, 255):\n",
        {**ON, "trace_endpoint": "http://10.1.2.1:3000/api/public/otel/v1/traces"},
    ),
    "port-range": (
        "    if port > 65535:\n",
        "    if False:\n",
        {**ON, "trace_endpoint": "http://10.1.2.3:99999/api/public/otel/v1/traces"},
    ),
}


@pytest.mark.parametrize("edit", list(SETTINGS_MUTATIONS.values()), ids=list(SETTINGS_MUTATIONS))
def test_mutations_of_the_settings_checks_are_caught(edit: tuple[Any, ...]) -> None:
    """Each mutant accepts the input, except `keys`: dropping the presence check still refuses a
    missing key, by the shape check, under that check's message (pinned here exactly)."""
    old, new, overrides, *rest = edit
    expected = rest[0] if rest else None
    with pytest.raises(ValidationError):
        _settings(**overrides)
    mutant = mutated_module(settings_module, (old, new))
    if expected is None:
        mutant.Settings(_env_file=None, **overrides)
    else:
        with pytest.raises(ValidationError) as mutated:
            mutant.Settings(_env_file=None, **overrides)
        assert str(mutated.value.errors()[0]["msg"]).removeprefix("Value error, ") == expected
