"""Builds a manifest-builder draft and corpus on disk for the builder tests (not a test file),
under safety review because `tests/safety/test_kb_manifest_build_no_defaults.py` depends on it.
The unit tests in `tests/unit/test_kb_manifest_build.py` import it too. Every text here is
synthetic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.safety.kb_fakes import mutated_module

from divesafe.services.kb_manifest_build import OK_BUILD, Outcome

__all__ = [
    "CRASHED",
    "GUIDE",
    "corpus",
    "document",
    "draft",
    "manifest_of",
    "mutated_module",
    "outcome",
]

GUIDE = (
    "Intro line.\n\n## Entry and exit\n\nEnter at the steps.\nSecond line.\n\n"
    "Another paragraph.\n## Hazards\nBoat traffic.\n"
)


def document(path: str = "guide.txt", **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "document_id": "site-guide",
        "version": 1,
        "title": "Synthetic guide",
        "origin": "dive_professional",
        "author_role": "dive_professional",
        "effective_date": "2026-01-31",
        "review_by_date": "2026-12-31",
        "licence": "Synthetic test licence",
        "licence_reference": "synthetic-licence-ref",
        "attribution": None,
        "licence_requires_personal_name": False,
        "site_id": None,
        "all_sites": True,
        "display_permitted": True,
        "display_permission_reference": "synthetic-permission-ref",
        "supersedes": None,
        "path": path,
    }
    doc.update(over)
    return doc


def draft(*docs: dict[str, Any], **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "format": 1,
        "attests_no_generated_text": True,
        "attests_no_personal_names": True,
        "documents": list(docs) or [document()],
    }
    out.update(over)
    return out


def corpus(root: Path, files: dict[str, str | bytes], the_draft: Any = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    path = root / "draft.json"
    raw = the_draft if isinstance(the_draft, bytes) else json.dumps(the_draft or draft()).encode()
    path.write_bytes(raw)
    return path


CRASHED = Outcome(-1, "crashed")


def outcome(module: Any, path: Path) -> Outcome:
    """As a REAL Outcome: a mutant COPY defines its own Outcome class, which never compares equal,
    so a mutant's result is rebuilt from its status and message."""
    try:
        module.build(path)
    except module.BuildRefused as refused:
        return Outcome(refused.outcome.status, refused.outcome.message)
    except Exception:  # noqa: BLE001 - a mutant may crash instead of refusing
        return CRASHED
    return OK_BUILD


def manifest_of(raw: bytes) -> dict[str, Any]:
    return json.loads(raw)  # type: ignore[no-any-return]
