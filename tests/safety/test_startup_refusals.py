"""Start-up refusals of the owner decisions of 2026-11-12 (ADR 0022 "Still open" items 9 and 6).

Item 9 (Known gap 12): a real LLM provider without a model host is refused in every environment;
`pipeline.record_egress` keeps its run-time fallback. Today's `openai_compatible` adapter needs
`llm_base_url`, so the case is reached with a registry stub. Item 6: production always requires
the knowledge connector, independently of the in-memory storage refusal in `create_app`.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.api import state as state_module
from divesafe.config import Settings
from divesafe.domain import RecordEgressBasis
from divesafe.models import FakeProvider
from divesafe.models import llm as llm_module
from divesafe.orchestration.pipeline import EGRESS_HOST_MISSING, record_egress
from divesafe.services import InMemoryAssessmentRepository

pytestmark = pytest.mark.safety

NO_HOST = (
    "refusing to start: the LLM provider has no model host (DIVESAFE_LLM_BASE_URL), so its "
    "egress cannot be recorded (ADR 0022 Known gap 12)"
)
NO_KNOWLEDGE = (
    "refusing to start in production without the knowledge connector, which needs "
    "DIVESAFE_DATABASE_URL "
    "(ADR 0022, owner decision 2026-11-12)"
)
REMOTE_URL = "https://llm.example/v1"
APP_DSN = "postgresql://app:pw@db.example/divesafe?sslmode=require"
KEYS = '{"' + "a" * 64 + '": {"actor": "ops", "roles": ["viewer"]}}'


class _Remote(FakeProvider):
    """Not the exact FakeProvider class, so `check_egress` treats it as a real provider."""

    def __init__(self) -> None:
        super().__init__()
        self.external = True


def _settings(environment: str = "development", **kwargs: Any) -> Settings:
    extra: dict[str, Any] = {"api_key_hashes": KEYS} if environment == "production" else {}
    return Settings(_env_file=None, environment=environment, **extra, **kwargs)  # type: ignore[arg-type, call-arg]


def _real(**kwargs: Any) -> dict[str, Any]:
    return {"llm_provider": "openai", "allow_external_llm": True, **kwargs}


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(llm_module._REGISTRY, "openai", lambda s: _Remote())


def _offline(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """A database URL in a start-up unit test: never connect."""
    monkeypatch.setattr(module, "build_repository", lambda s: InMemoryAssessmentRepository())


def _never(settings: Settings) -> Any:
    raise AssertionError("the repository was built before the start-up refusal")


# --- item 9: a real provider needs a model host, in every environment -------------------------


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_a_real_provider_without_a_model_host_is_refused(
    stub: None, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setattr(state_module, "build_repository", _never)
    settings = _settings(environment, database_url=APP_DSN, **_real())
    assert settings.llm_base_url is None
    with pytest.raises(RuntimeError) as caught:
        state_module.build_default_state(settings)
    assert str(caught.value) == NO_HOST
    assert "://" not in str(caught.value)


@pytest.mark.parametrize("environment", ["development", "production"])
def test_a_real_provider_with_a_model_host_starts_and_records_the_host_only(
    stub: None, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    _offline(monkeypatch, state_module)
    state = state_module.build_default_state(
        _settings(environment, database_url=APP_DSN, **_real(llm_base_url=REMOTE_URL))
    )
    assert isinstance(state.provider, _Remote)
    assert state.egress_basis == "remote_external_opt_in"
    assert state.model_host == "llm.example"


def test_the_fake_provider_starts_with_no_model_host() -> None:
    state = state_module.build_default_state(_settings())
    assert (state.provider, state.egress_basis, state.model_host) == (None, "no_agent_stage", None)


def test_the_run_time_fallback_is_unchanged() -> None:
    """Known gap 12's remedy is at start-up; the record still withholds without a host."""
    assert record_egress(_Remote(), "remote_external_opt_in", None) == (
        RecordEgressBasis.NONE,
        None,
        EGRESS_HOST_MISSING,
    )


def test_mutation_dropping_the_model_host_refusal_is_caught(stub: None) -> None:
    mutant = mutated_module(
        state_module, ("    if provider is not None and host is None:\n", "    if False:\n")
    )
    state = mutant.build_default_state(_settings(**_real()))
    assert state.provider is not None and state.model_host is None


def test_mutation_limiting_the_model_host_refusal_to_production_is_caught(stub: None) -> None:
    mutant = mutated_module(
        state_module,
        (
            "    if provider is not None and host is None:\n",
            '    if settings.environment == "production" and provider is not None '
            "and host is None:\n",
        ),
    )
    assert mutant.build_default_state(_settings(**_real())).model_host is None


# --- item 6: production always requires the knowledge connector -------------------------------


def test_production_without_a_database_is_refused_for_the_knowledge_connector(
    stub: None,
) -> None:
    with pytest.raises(RuntimeError) as caught:
        state_module.build_default_state(_settings("production", **_real(llm_base_url=REMOTE_URL)))
    assert str(caught.value) == NO_KNOWLEDGE


def test_production_refuses_a_missing_connector_even_with_a_database_url(
    stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent of the in-memory refusal: the connector itself is checked, before the
    repository is built (nothing connects)."""
    monkeypatch.setattr(state_module, "build_knowledge_connector", lambda s: None)
    monkeypatch.setattr(state_module, "build_repository", _never)
    with pytest.raises(RuntimeError) as caught:
        state_module.build_default_state(
            _settings("production", database_url=APP_DSN, **_real(llm_base_url=REMOTE_URL))
        )
    assert str(caught.value) == NO_KNOWLEDGE


def test_production_with_the_connector_starts(stub: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch, state_module)
    state = state_module.build_default_state(
        _settings("production", database_url=APP_DSN, **_real(llm_base_url=REMOTE_URL))
    )
    assert state.knowledge_connector is not None


@pytest.mark.parametrize("environment", ["development", "test"])
def test_outside_production_no_connector_is_allowed(environment: str) -> None:
    assert state_module.build_default_state(_settings(environment)).knowledge_connector is None


def test_create_app_refuses_before_serving(stub: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from divesafe.api import app as app_module

    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: _settings("production", **_real(llm_base_url=REMOTE_URL)),
    )
    with pytest.raises(RuntimeError) as caught:
        app_module.create_app()
    assert str(caught.value) == NO_KNOWLEDGE


def test_mutation_dropping_the_knowledge_connector_refusal_is_caught(
    stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = mutated_module(
        state_module,
        (
            '    if settings.environment == "production" and knowledge_connector is None:\n',
            "    if False:\n",
        ),
    )
    monkeypatch.setattr(mutant, "build_knowledge_connector", lambda s: None)
    _offline(monkeypatch, mutant)
    state = mutant.build_default_state(
        _settings("production", database_url=APP_DSN, **_real(llm_base_url=REMOTE_URL))
    )
    assert state.knowledge_connector is None
