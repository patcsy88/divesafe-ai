"""Build `manifest.json` for the knowledge loader from an operator's draft (owner request,
2026-11-12). A convenience tool, NOT a gate: the loader (`kb_manifest.read_corpus`) stays the
only authority, and re-checks everything this tool prints.

    python -m divesafe.services.kb_manifest_build <corpus dir>/<draft>.json > manifest.json
    python -m divesafe.services.kb_manifest_build --check <corpus dir>

It never writes a file: the manifest goes to stdout, once, as the exact bytes that were
validated (UTF-8 JSON, two-space indent, one final newline); messages go to stderr, fixed, echoing
no value, path or parser text. It runs with no network and a read-only corpus.

The draft (JSON, in the corpus directory; document paths are relative to that directory) holds
every human-supplied field and nothing else: top level exactly `format`,
`attests_no_generated_text`, `attests_no_personal_names`, `documents`; each document exactly
`document_id`, `version`, `title`, `origin`, `author_role`, `effective_date`, `review_by_date`,
`licence`, `licence_reference`, `attribution`, `licence_requires_personal_name`, `site_id`,
`all_sites`, `display_permitted`, `display_permission_reference`, `supersedes`, `path`. A missing
or extra key, or a duplicate key, is refused; `null` must be written out where the manifest allows
it (`attribution`, `site_id`, `supersedes`). Values are copied verbatim: nothing is invented,
defaulted or corrected, and the loader's parser judges them.

The tool computes only `file.sha256`, `chunks` and `gaps`. Chunking rule (deterministic):

- The file must be UTF-8, NFC, with no `\\r`. A line is its text up to and including `\\n`.
- A line starting with `## ` is a heading. Its bytes (and any blank lines after it) are a gap with
  reason `heading`; its text after `## `, without surrounding spaces, is the `section` of the
  chunks that follow (1-200 characters, the manifest field rules). Any other line is body text
  (the loader's character screen refuses `#` there).
- Body text before the first heading uses the document's `title` as its section.
- A blank line is an empty line. A paragraph is a run of non-blank, non-heading lines plus the
  blank lines after it (blank lines at the very start of the file go with the first item).
- Paragraphs are packed in order into chunks of at most `kb_common.MAX_CHUNK_CHARACTERS`
  characters; a heading always ends a chunk. A paragraph over the limit is refused, never split.
- Chunks are numbered 1..n in file order; chunks and gaps cover every byte, in order.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from divesafe.services import kb_common, kb_manifest, kb_screen
from divesafe.services.kb_common import LoaderRefusal

HEADING = "## "
FLAG_CHECK = "--check"

TOP_KEYS = ("format", "attests_no_generated_text", "attests_no_personal_names", "documents")
DOCUMENT_KEYS = (
    "document_id",
    "version",
    "title",
    "origin",
    "author_role",
    "effective_date",
    "review_by_date",
    "licence",
    "licence_reference",
    "attribution",
    "licence_requires_personal_name",
    "site_id",
    "all_sites",
    "display_permitted",
    "display_permission_reference",
    "supersedes",
)
PATH_KEY = "path"


@dataclass(frozen=True)
class Outcome:
    status: int
    message: str


OK_BUILD = Outcome(0, "built: the manifest is on stdout; hash the written file")
OK_CHECK = Outcome(0, "checked: the loader accepts this corpus")
FAILED_ARGUMENTS = Outcome(
    2, "usage: kb_manifest_build <draft.json> | kb_manifest_build --check <corpus dir>"
)
FAILED_UNICODE = Outcome(3, "refused: the builder needs the reviewed Python Unicode data")
FAILED_DRAFT_READ = Outcome(5, "refused: the draft cannot be read")
FAILED_DRAFT = Outcome(6, "refused: the draft is not valid (a field missing, extra or repeated)")
FAILED_FILE = Outcome(7, "refused: a corpus file cannot be read or is not accepted")
FAILED_HEADING = Outcome(8, "refused: a heading is not accepted as a section")
FAILED_PARAGRAPH = Outcome(9, "refused: a paragraph is over the chunk length limit")
FAILED_TEXT = Outcome(10, "refused: a chunk has text the loader does not accept")
FAILED_ZERO_CHUNKS = Outcome(11, "refused: a document has no chunks")
FAILED_LIMIT = Outcome(12, "refused: the corpus is over a loader limit")
FAILED_LOADER = Outcome(13, "refused: the loader would not accept this manifest")
FAILED_CHECK = Outcome(14, "refused: the loader does not accept this corpus")
FAILED_OUTPUT = Outcome(15, "failed: the output could not be written")


class BuildRefused(Exception):
    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def parse_draft(raw: bytes) -> dict[str, Any]:
    """Shape only: exact key sets, no duplicates. Values are not judged here (the loader's
    parser judges the built manifest), except that `path` and `title` must be strings."""
    try:
        draft = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        raise BuildRefused(FAILED_DRAFT) from None
    if not isinstance(draft, dict) or set(draft) != set(TOP_KEYS):
        raise BuildRefused(FAILED_DRAFT)
    documents = draft["documents"]
    if not isinstance(documents, list) or not documents:
        raise BuildRefused(FAILED_DRAFT)
    for doc in documents:
        if not isinstance(doc, dict) or set(doc) != {*DOCUMENT_KEYS, PATH_KEY}:
            raise BuildRefused(FAILED_DRAFT)
        if not isinstance(doc[PATH_KEY], str) or not isinstance(doc["title"], str):
            raise BuildRefused(FAILED_DRAFT)
    return draft


def _lines(text: str) -> list[str]:
    pieces = text.split("\n")
    return [p + "\n" for p in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])


def _section_ok(value: str) -> bool:
    return (
        1 <= len(value) <= 200
        and kb_screen.allowed_text(value, newlines=False)
        and not kb_screen.contains_evidence_id(value)
    )


@dataclass
class _Unit:
    heading: str | None  # the section text for a heading, None for a paragraph
    text: str
    closed: bool = False  # a paragraph is closed by a blank line


def _units(text: str) -> list[_Unit]:
    units: list[_Unit] = []
    prefix = ""  # blank lines at the very start of the file
    for line in _lines(text):
        if line.startswith(HEADING):
            section = line[len(HEADING) :].removesuffix("\n").strip(" ")
            if not _section_ok(section):
                raise BuildRefused(FAILED_HEADING)
            units.append(_Unit(section, prefix + line))
        elif line == "\n":
            if units:
                units[-1].text += line
                units[-1].closed = True
            else:
                prefix += line
            continue
        elif units and units[-1].heading is None and not units[-1].closed:
            units[-1].text += line
        else:
            units.append(_Unit(None, prefix + line))
        prefix = ""
    if prefix:  # a file of blank lines only: no chunks
        raise BuildRefused(FAILED_ZERO_CHUNKS)
    return units


def chunk_file(data: bytes, title: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The chunk and gap entries for one file, by the rule in the module docstring."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BuildRefused(FAILED_FILE) from None
    if not kb_manifest.whole_file_ok(text):
        raise BuildRefused(FAILED_FILE)
    chunks: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    section = title
    before_first_heading = True  # the section is the title, not yet screened
    position = 0  # bytes
    current: tuple[int, str, str] | None = None  # (start byte, text, section)

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        start, piece, piece_section = current
        if not kb_screen.allowed_text(piece, newlines=True) or kb_screen.contains_evidence_id(
            piece
        ):
            raise BuildRefused(FAILED_TEXT)
        raw = piece.encode("utf-8")
        chunks.append(
            {
                "chunk": len(chunks) + 1,
                "section": piece_section,
                "start": start,
                "end": start + len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        current = None

    for unit in _units(text):
        size = len(unit.text.encode("utf-8"))
        if unit.heading is not None:
            flush()
            gaps.append({"start": position, "end": position + size, "reason": "heading"})
            section = unit.heading
            before_first_heading = False
        else:
            if len(unit.text) > kb_common.MAX_CHUNK_CHARACTERS:
                raise BuildRefused(FAILED_PARAGRAPH)
            if (
                current is not None
                and len(current[1]) + len(unit.text) <= kb_common.MAX_CHUNK_CHARACTERS
            ):
                current = (current[0], current[1] + unit.text, current[2])
            else:
                flush()
                if before_first_heading and not _section_ok(title):
                    raise BuildRefused(FAILED_HEADING)
                current = (position, unit.text, section)
        position += size
    flush()
    if not chunks:
        raise BuildRefused(FAILED_ZERO_CHUNKS)
    return chunks, gaps


def _valid_path(path: str) -> bool:
    """The manifest's path rules, and never the manifest's own name (any case: some file systems
    fold case), which the operator is about to replace."""
    if path.casefold() == kb_manifest.MANIFEST_FILENAME:
        return False
    try:
        kb_manifest.FileEntry.model_validate({"path": path, "sha256": "0" * 64, "chunks": ()})
    except ValidationError:
        return False
    return True


def render(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def validate(raw: bytes, files: Sequence[bytes]) -> None:
    """The loader's own checks on the exact bytes to be printed and the files as read, as
    `read_corpus` runs them (it cannot run here: nothing is written)."""
    try:
        manifest, _ = kb_manifest.parse_manifest(raw)
        if len(raw) + sum(len(f) for f in files) > kb_common.MAX_MANIFEST_TOTAL_BYTES:
            raise kb_common.LimitExceeded("over the size limit")
        total = 0
        for entry, data in zip(manifest.documents, files, strict=True):
            chunks = kb_manifest._split_chunks(entry, data)
            if not chunks:
                raise BuildRefused(FAILED_ZERO_CHUNKS)
            total += len(chunks)
        if total > kb_common.MAX_CHUNKS_PER_LOAD:
            raise kb_common.LimitExceeded("over the chunk limit")
    except (kb_common.LimitExceeded, kb_manifest.ManifestTooLarge):
        raise BuildRefused(FAILED_LIMIT) from None
    except (LoaderRefusal, ValueError):
        raise BuildRefused(FAILED_LOADER) from None


def build(draft_path: Path) -> bytes:
    """The manifest bytes for the draft at `draft_path`; document paths are relative to its
    directory. Reads only; raises BuildRefused with a fixed outcome."""
    if draft_path.name in ("", ".", "..", kb_manifest.MANIFEST_FILENAME):
        raise BuildRefused(FAILED_ARGUMENTS)
    try:
        root_fd = kb_manifest._open_dir(draft_path.parent)
    except OSError:
        raise BuildRefused(FAILED_DRAFT_READ) from None
    try:
        seen: set[tuple[int, int]] = set()  # the draft too: a document cannot be the draft
        try:
            raw_draft = kb_manifest.read_regular_file(
                root_fd, draft_path.name, kb_common.MAX_MANIFEST_TOTAL_BYTES, seen
            )
        except (OSError, ValueError, LoaderRefusal):
            raise BuildRefused(FAILED_DRAFT_READ) from None
        draft = parse_draft(raw_draft)
        budget = kb_common.MAX_MANIFEST_TOTAL_BYTES
        files: list[bytes] = []
        documents: list[dict[str, Any]] = []
        for doc in draft["documents"]:
            path = doc[PATH_KEY]
            if not _valid_path(path):
                raise BuildRefused(FAILED_FILE)
            try:
                data = kb_manifest.read_regular_file(root_fd, path, budget, seen)
            except kb_manifest.ManifestTooLarge:
                raise BuildRefused(FAILED_LIMIT) from None
            except (OSError, ValueError, LoaderRefusal):  # ValueError: a NUL or lone surrogate
                raise BuildRefused(FAILED_FILE) from None
            budget -= len(data)
            chunks, gaps = chunk_file(data, doc["title"])
            entry = {key: doc[key] for key in DOCUMENT_KEYS}
            entry["file"] = {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "chunks": chunks,
                "gaps": gaps,
            }
            documents.append(entry)
            files.append(data)
            if sum(len(d["file"]["chunks"]) for d in documents) > kb_common.MAX_CHUNKS_PER_LOAD:
                raise BuildRefused(FAILED_LIMIT)
    finally:
        os.close(root_fd)
    manifest = {key: draft[key] for key in TOP_KEYS[:-1]}
    manifest["documents"] = documents
    try:
        raw = render(manifest)
    except (ValueError, TypeError):
        raise BuildRefused(FAILED_LOADER) from None
    validate(raw, files)
    return raw


def check(target: Path) -> str:
    """`read_corpus` on a corpus directory; the summary to print."""
    try:
        corpus = kb_manifest.read_corpus(target)
    except LoaderRefusal:
        raise BuildRefused(FAILED_CHECK) from None
    lines = [f"manifest_sha256 {corpus.manifest_sha256}"]
    lines += [
        f"document {d.entry.document_id} version {d.entry.version} chunks {len(d.chunks)}"
        for d in corpus.documents
    ]
    return "\n".join(lines) + "\n"


def _emit(text: str) -> None:
    with contextlib.suppress(OSError):
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


def _main(args: list[str]) -> Outcome:
    if len(args) == 1 and not args[0].startswith("-"):
        mode, operand = "build", args[0]
    elif len(args) == 2 and args[0] == FLAG_CHECK:
        mode, operand = "check", args[1]
    else:
        return FAILED_ARGUMENTS
    try:
        kb_screen.require_reviewed_unicode()
    except kb_screen.UnicodeNotReviewed:
        return FAILED_UNICODE
    try:
        out = build(Path(operand)) if mode == "build" else check(Path(operand)).encode()
    except BuildRefused as refused:
        return refused.outcome
    try:
        sys.stdout.buffer.write(out)  # once: the exact bytes that were validated
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        return FAILED_OUTPUT
    return OK_BUILD if mode == "build" else OK_CHECK


def main(argv: Sequence[str] | None = None) -> int:
    outcome = _main(list(sys.argv[1:] if argv is None else argv))
    _emit(outcome.message)
    return outcome.status


if __name__ == "__main__":
    raise SystemExit(main())
