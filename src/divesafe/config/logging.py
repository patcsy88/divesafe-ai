"""Structured (JSON) logging using only the standard library, with secret redaction.

The request id (ADR 0027 item 1) lives in a context variable: the API's middleware binds it for
one request, and the run manager binds it explicitly for the run that request started. The
formatter adds it as `request_id` to every line written while it is bound. Only a value matching
`REQUEST_ID_PATTERN` is ever bound.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_SENSITIVE = re.compile(r"key|token|secret|password|authorization|credential|url", re.IGNORECASE)

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")
_REQUEST_ID: ContextVar[str | None] = ContextVar("divesafe_request_id", default=None)


def valid_request_id(value: object) -> bool:
    return type(value) is str and REQUEST_ID_PATTERN.fullmatch(value) is not None


def bind_request_id(value: str | None) -> Token[str | None]:
    """Bind `value` for this context; anything that is not a valid id binds nothing."""
    return _REQUEST_ID.set(value if valid_request_id(value) else None)


def unbind_request_id(token: Token[str | None]) -> None:
    _REQUEST_ID.reset(token)


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def _redact(key: str, value: Any) -> Any:
    return "[REDACTED]" if _SENSITIVE.search(key) else value


class JsonFormatter(logging.Formatter):
    """Do not log prompts, personal data or secrets. Fields with sensitive names are masked."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: _redact(k, v) for k, v in record.__dict__.items() if k not in _RESERVED})
        # Last, so an `extra` of the same name can never replace or forge it.
        payload.pop("request_id", None)
        request_id = _REQUEST_ID.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
