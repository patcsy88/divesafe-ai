"""Loads a reviewed ruleset file of `RuleDefinition`s (ADR 0009). The repository ships none.

The file is code-equivalent: whoever can write it can change what the system recommends. So it is
read once, with a bounded read, hashed (the hash identifies exactly what was loaded), parsed
strictly (no duplicate keys, no NaN/Infinity), and every problem refuses that definition with a
reason that carries field locations and fixed messages, never submitted values. The caller decides
what a refusal means; the API refuses to start.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from divesafe.domain import RuleDefinition, ThresholdStatus
from divesafe.risk import DefinitionRule

MAX_FILE_BYTES = 1_000_000
MAX_DEFINITIONS = 500
MAX_REASON_CHARS = 300
MAX_REFUSALS_SHOWN = 10
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._#-]")


@dataclass(frozen=True)
class Refusal:
    definition_id: str
    reason: str


@dataclass(frozen=True)
class LoadedRuleset:
    version: str
    sha256: str
    rules: tuple[DefinitionRule, ...]
    refused: tuple[Refusal, ...]

    @property
    def is_clean(self) -> bool:
        return not self.refused

    @property
    def identity(self) -> str:
        """Recorded as the ruleset version: the label plus the content hash it was loaded from."""
        return f"{self.version}@{self.sha256[:12]}"

    def describe_refusals(self) -> str:
        shown = [f"{r.definition_id}: {r.reason}" for r in self.refused[:MAX_REFUSALS_SHOWN]]
        extra = len(self.refused) - len(shown)
        return "; ".join(shown) + (f"; and {extra} more" if extra > 0 else "")


class RulesetError(ValueError):
    """The file as a whole is unusable. Messages are fixed and carry no file content."""


def read_ruleset_file(path: Path) -> bytes:
    """One bounded read of a regular file. Never reads more than the cap plus one byte."""
    try:
        if not path.is_file():
            raise RulesetError("the ruleset path is not a regular file")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise RulesetError("the ruleset file could not be read") from exc
    if len(data) > MAX_FILE_BYTES:
        raise RulesetError("the ruleset file is too large")
    return data


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(_name: str) -> Any:
    raise ValueError("NaN and Infinity are not allowed")


def _reason(exc: ValidationError) -> str:
    parts = [
        f"{_SAFE_ID.sub('?', '.'.join(str(p) for p in e['loc']))[:60] or 'definition'}: "
        f"{str(e['msg'])[:120]}"
        for e in exc.errors()[:4]
    ]
    return "; ".join(parts)[:MAX_REASON_CHARS]


def _identity(raw: object, index: int) -> str:
    label = raw.get("id") if isinstance(raw, dict) else None
    if isinstance(label, str) and label:
        return _SAFE_ID.sub("?", label)[:60]
    return f"#{index}"


def load_ruleset_bytes(
    data: bytes, now: datetime, known_sites: Collection[str] | None = None
) -> LoadedRuleset:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
        parsed = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RulesetError(
            "the ruleset file is not valid UTF-8 JSON without duplicate keys"
        ) from exc
    if not isinstance(parsed, dict):
        raise RulesetError("ruleset file must be a JSON object")
    version = parsed.get("ruleset_version")
    definitions = parsed.get("definitions")
    if not isinstance(version, str) or not VERSION_PATTERN.match(version):
        raise RulesetError("ruleset_version is required (letters, digits, . _ - ; at most 64)")
    if not isinstance(definitions, list):
        raise RulesetError("definitions must be a list")
    if len(definitions) > MAX_DEFINITIONS:
        raise RulesetError("too many definitions")

    rules: list[DefinitionRule] = []
    refused: list[Refusal] = []
    seen: set[str] = set()
    for index, raw in enumerate(definitions):
        ident = _identity(raw, index)
        try:
            definition = RuleDefinition.model_validate(raw)
        except ValidationError as exc:
            refused.append(Refusal(ident, _reason(exc)))
            continue
        if definition.id in seen:
            refused.append(Refusal(ident, "duplicate definition id"))
            continue
        seen.add(definition.id)
        if definition.status != ThresholdStatus.VALIDATED:
            refused.append(Refusal(ident, f"status is {definition.status.value}, not VALIDATED"))
            continue
        assert definition.expires_on is not None and definition.signed_off_on is not None
        if now >= definition.expires_on:
            refused.append(Refusal(ident, "the signed-off review has expired"))
            continue
        if definition.signed_off_on > now:
            refused.append(Refusal(ident, "the sign-off is dated in the future"))
            continue
        unknown = sorted(set(definition.scope.site_ids) - set(known_sites or ()))
        if known_sites is not None and unknown:
            refused.append(Refusal(ident, "the scope names a site that is not registered"))
            continue
        rules.append(DefinitionRule(definition))
    return LoadedRuleset(version, digest, tuple(rules), tuple(refused))


def load_ruleset(
    text: str, now: datetime, known_sites: Collection[str] | None = None
) -> LoadedRuleset:
    return load_ruleset_bytes(text.encode(), now, known_sites)
