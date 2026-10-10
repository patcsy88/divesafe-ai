"""Production database settings: TLS is required and the DSN is never echoed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from divesafe.config import Settings


def _prod(dsn: str) -> Settings:
    return Settings(
        environment="production",
        llm_provider="ollama",
        auth_mode="api_key",
        database_url=dsn,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:topsecret@db/app",
        "postgresql://u:topsecret@db/app?sslmode=prefer",
        "postgresql://u:topsecret@db/app?sslmode=disable",
        "postgresql://u:topsecret@db/app?sslmode=allow",
    ],
)
def test_production_refuses_a_database_connection_that_may_be_plaintext(dsn: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _prod(dsn)
    assert "topsecret" not in str(caught.value)


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_production_accepts_an_encrypted_connection(mode: str) -> None:
    assert _prod(f"postgresql://u:p@db/app?sslmode={mode}").database_url is not None


def test_a_malformed_dsn_error_does_not_echo_the_string() -> None:
    with pytest.raises(ValidationError) as caught:
        _prod("host=db password=topsecret sslmode")
    assert "topsecret" not in str(caught.value)


def test_development_may_use_a_plain_local_connection() -> None:
    assert (
        Settings(environment="development", database_url="postgresql://u:p@localhost/x") is not None
    )  # type: ignore[arg-type]


def test_nul_characters_are_refused_at_the_boundaries_that_feed_the_store() -> None:
    from divesafe.api.schemas import ActualConditionsRequest, DecisionRequest
    from divesafe.data.errors import ConnectorResponseError
    from divesafe.data.parsing import bounded_text

    with pytest.raises(ValidationError):
        DecisionRequest(decision="GO", rationale="a\x00b")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ActualConditionsRequest(observations={"note": "a\x00b"})
    with pytest.raises(ValidationError):
        ActualConditionsRequest(observations={"x": float("nan")})
    with pytest.raises(ConnectorResponseError):
        bounded_text("warning\x00text", "text_en")


def test_db_init_needs_an_owner_dsn_and_never_the_application_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from divesafe.services import db_init

    monkeypatch.delenv("DIVESAFE_ADMIN_DATABASE_URL", raising=False)
    monkeypatch.setenv("DIVESAFE_DATABASE_URL", "postgresql://app:pw@localhost/x")
    monkeypatch.chdir("/")
    with pytest.raises(SystemExit):
        db_init.main()
