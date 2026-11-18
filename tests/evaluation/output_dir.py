"""The output-directory check shared by the item 6 capture and the live evaluation runs. Offline.

Neutral on purpose (ADR 0030, "AST tests"): it imports neither `capture_item6` nor `api.state`,
so `egress_gate.eval_output_dir` loads neither.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.evaluation.item6_scrub import REPO


class CaptureRefused(RuntimeError):
    """A refusal with a fixed message. Nothing has been connected to when it is raised."""


def check_output_dir(directory: Path, repo: Path = REPO) -> Path:
    """Refuse a directory inside the repository working tree, or one that cannot be resolved.

    F7: each ancestor is compared with `os.path.samefile`, not by text. On macOS's case-insensitive
    volume, `/Users/.../divesafe ai/tests` is the same directory as the repository, and a text
    comparison would miss it. An existing file given as the output directory is refused."""
    resolved = directory.expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise CaptureRefused("the capture directory is an existing file; it must be a directory")
    root = repo.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.exists() and os.path.samefile(candidate, root):
            raise CaptureRefused(
                "the capture directory is inside the repository; it must be outside"
            )
    return resolved
