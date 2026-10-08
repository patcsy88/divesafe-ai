from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from divesafe.config import Settings
from divesafe.config.logging import JsonFormatter


def test_fake_llm_rejected_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", llm_provider="fake")


def test_max_age_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(evidence_max_age_minutes=0)


def test_secrets_do_not_appear_in_repr() -> None:
    settings = Settings(llm_api_key="sk-test-value", database_url="postgresql://u:pw@h/db")
    assert "sk-test-value" not in repr(settings)
    assert "pw@h" not in repr(settings)


def test_log_fields_with_sensitive_names_are_redacted() -> None:
    record = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
    record.api_key = "sk-test-value"
    record.site = "site-1"
    out = json.loads(JsonFormatter().format(record))
    assert out["api_key"] == "[REDACTED]"
    assert out["site"] == "site-1"
