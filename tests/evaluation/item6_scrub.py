"""ADR 0025 item 6: the scrub rules, shared by the fixture scan (tests/safety) and the capture's
write-time check (tests/evaluation/capture_item6.py). Offline.

A screen, not a proof: a paraphrase, or a secret in a form no rule names, is not detected.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
# Each rule: (name, compiled pattern). Matching is case-insensitive.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ip_address", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("loopback_literal", re.compile(r"localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]", re.IGNORECASE)),
    ("proxy_software", re.compile(r"cli-?proxy|cliproxyapi", re.IGNORECASE)),
    ("bearer_or_key", re.compile(r"bearer\s+\S|sk-[A-Za-z0-9_-]{8,}|metToken\s", re.IGNORECASE)),
    (
        "token_or_key_field",
        re.compile(
            r"[\"']?(?:api[_-]?key|token|access[_-]?token|secret|password|authorization)[\"']?"
            r"\s*[:=]",
            re.IGNORECASE,
        ),
    ),
    ("http_url", re.compile(r"https?://", re.IGNORECASE)),
    (
        "provider_metadata_key",
        re.compile(
            r"[\"'](?:system_fingerprint|created|object|choices|usage|finish_reason|"
            r"x_request_id|request_id)[\"']\s*:|chatcmpl-",
            re.IGNORECASE,
        ),
    ),
    ("users_path", re.compile(r"/Users/|/home/|/private/var")),
    ("met_token_label", re.compile(r"METToken:", re.IGNORECASE)),
    # F11: a host name followed by a port, e.g. "llm.example.org:8317" or "203.0.113.7:8080".
    ("host_port", re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+:\d{2,5}(?!\d)")),
)

# Keys that must never appear as a key in a written payload. Plain `id` is not listed: a connector
# payload may carry it legitimately, and provider replies are written as text only.
FORBIDDEN_KEYS = frozenset(
    {
        "url",
        "headers",
        "authorization",
        "system_fingerprint",
        "created",
        "object",
        "usage",
        "choices",
        "model",
        "proxy",
        "host",
        "ip",
        "port",
    }
)


def scan_text(text: str) -> list[str]:
    """Names of every rule that matches the text."""
    return [name for name, pattern in RULES if pattern.search(text)]


def find_forbidden_keys(value: Any) -> list[str]:
    """Every forbidden key name found anywhere in a JSON-like value."""
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                found.append(str(k))
            found.extend(find_forbidden_keys(v))
    elif isinstance(value, list | tuple):
        for v in value:
            found.extend(find_forbidden_keys(v))
    return found


def label(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def scan_dirs(dirs: tuple[Path, ...]) -> list[str]:
    """One line per (file, rule) hit. Empty means clean."""
    hits: list[str] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        # F10: every file in the directory, whatever its suffix (a .bin or .log can carry a secret).
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            for name in scan_text(path.read_text(encoding="utf-8", errors="replace")):
                hits.append(f"{label(path)}: {name}")
    return hits
