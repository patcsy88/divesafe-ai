"""Structured (JSON) logging using only the standard library, with secret redaction."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_SENSITIVE = re.compile(r"key|token|secret|password|authorization|credential|url", re.IGNORECASE)


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
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
