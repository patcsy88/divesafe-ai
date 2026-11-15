"""What a leak scan reads from a log record (not a test file).

`logging` adds runtime numbers to every record: `created`, `msecs`, `relativeCreated`, `thread`
and `process`. They come from the clock and the process, not from the code under test, so they can
hold any digits by chance (CI on PR #9 had `relativeCreated=84435.197371`, which looks like port
8443). Exactly those five are dropped. Everything else stays: `msg`, `args`, the rendered
message, `exc_info`, `exc_text`, `stack_info`, `name` and every `extra` field the code adds.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

RUNTIME_NUMBERS = frozenset({"created", "msecs", "relativeCreated", "thread", "process"})


def scanned_text(record: logging.LogRecord) -> str:
    """The record's attributes (less `RUNTIME_NUMBERS`) and its rendered message, as one text."""
    kept = {k: v for k, v in vars(record).items() if k not in RUNTIME_NUMBERS}
    return f"{record.getMessage()!r} {kept!r}"


def scanned_records(records: Iterable[logging.LogRecord]) -> str:
    """`scanned_text` of every record, joined."""
    return "\n".join(scanned_text(r) for r in records)
