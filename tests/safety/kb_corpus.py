"""Builds a small, valid corpus on disk for the loader tests (not a test file). Every text here is
synthetic. `write_corpus` returns the SHA-256 of the manifest bytes as written."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from divesafe.data.sites import SITES

SITE = next(iter(SITES))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_spec(
    document_id: str = "site-guide",
    version: int = 1,
    chunks: Sequence[str] = ("Alpha text.\n", "Beta text.\n"),
    gap: str = "",
    **over: Any,
) -> dict[str, Any]:
    """One manifest document plus its file content. `chunks` are consecutive, whole-file spans."""
    data = ("".join(chunks) + gap).encode()
    gaps = []
    entries = []
    position = 0
    for number, text in enumerate(chunks, start=1):
        raw = text.encode()
        entries.append(
            {
                "chunk": number,
                "section": "Intro",
                "start": position,
                "end": position + len(raw),
                "sha256": sha(raw),
            }
        )
        position += len(raw)
    if gap:
        gaps.append({"start": position, "end": position + len(gap.encode()), "reason": "heading"})
    entry: dict[str, Any] = {
        "document_id": document_id,
        "version": version,
        "title": "Synthetic guide",
        "origin": "dive_professional",
        "author_role": "dive_professional",
        "effective_date": "2026-01-31",
        "review_by_date": "2026-12-31",
        "licence": "Synthetic test licence",
        "licence_reference": "synthetic-licence-ref",
        "licence_requires_personal_name": False,
        "site_id": None,
        "all_sites": True,
        "display_permitted": True,
        "display_permission_reference": "synthetic-permission-ref",
        "file": {
            "path": f"{document_id}-v{version}.txt",
            "sha256": sha(data),
            "chunks": entries,
            "gaps": gaps,
        },
    }
    entry.update(over)
    return {"entry": entry, "data": data}


def write_corpus(
    root: Path,
    specs: Sequence[dict[str, Any]] | None = None,
    *,
    top: dict[str, Any] | None = None,
) -> str:
    specs = list(specs) if specs is not None else [doc_spec()]
    manifest: dict[str, Any] = {
        "format": 1,
        "attests_no_generated_text": True,
        "attests_no_personal_names": True,
        "documents": [s["entry"] for s in specs],
    }
    manifest.update(top or {})
    root.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        (root / spec["entry"]["file"]["path"]).write_bytes(spec["data"])
    raw = json.dumps(manifest).encode()
    (root / "manifest.json").write_bytes(raw)
    return sha(raw)
