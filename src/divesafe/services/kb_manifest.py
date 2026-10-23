"""Manifest model and bounded corpus reading for the knowledge loader (ADR 0022 decisions 6, 7, 12,
13; ADR 0024 A3). The input is untrusted: every refusal is a fixed message, never a value, a path
or the parser's text. JSON only (no YAML, no new dependency).

The manifest is `manifest.json` at the corpus mount root (owner-approved format, Q6,
2026-10-23; ADR 0024 A3).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import stat
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from divesafe.data.sites import SITES
from divesafe.services import kb_common, kb_screen
from divesafe.services.kb_common import LoaderRefusal

MANIFEST_FILENAME = "manifest.json"
APPROVER_ROLE = "dive_professional"  # the only value the table allows

_ID = re.compile(r"[a-z0-9]+(-[a-z0-9]+){0,2}")
_SHA = re.compile(r"[0-9a-f]{64}")


class ManifestInvalid(LoaderRefusal):
    pass


class ManifestTooLarge(LoaderRefusal):
    pass


class ZeroChunks(LoaderRefusal):
    pass


class FileHashMismatch(LoaderRefusal):
    pass


class ChunkHashMismatch(LoaderRefusal):
    pass


def add_months(value: date, months: int) -> date:
    """Calendar months, clamped to the last day of the target month (ADR 0022 decision 7)."""
    index = value.year * 12 + (value.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def whole_file_ok(value: str) -> bool:
    """Decision 13 / 7: the whole file must be NFC with no `\\r` (the span offsets depend on it).
    Stated gap bytes are NOT screened further and are not stored."""
    return unicodedata.normalize("NFC", value) == value and "\r" not in value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


class _Strict(BaseModel):
    # strict: no coercion (true is not 1, "1" is not 1, 1 is not "1"); AGENTS.md section 10.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True, strict=True)


def _field_text(value: str) -> str:
    """Allow-list and id screen for every stored manifest string field (decisions 9, 13)."""
    if not kb_screen.allowed_text(value, newlines=False) or kb_screen.contains_evidence_id(value):
        raise ValueError("text not accepted")
    return value


class ChunkEntry(_Strict):
    chunk: StrictInt = Field(ge=1, le=999999)
    section: StrictStr = Field(min_length=1, max_length=200)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=0)
    sha256: StrictStr

    _section = field_validator("section")(_field_text)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        if not _SHA.fullmatch(v):
            raise ValueError("bad hash")
        return v


class GapEntry(_Strict):
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=0)
    reason: Literal["heading", "licence_notice"]


class FileEntry(_Strict):
    path: StrictStr = Field(min_length=1, max_length=200)
    sha256: StrictStr
    chunks: tuple[ChunkEntry, ...] = Field(max_length=kb_common.MAX_CHUNKS_PER_LOAD)
    gaps: tuple[GapEntry, ...] = Field(default=(), max_length=kb_common.MAX_CHUNKS_PER_LOAD)

    @field_validator("sha256")
    @classmethod
    def _hash(cls, v: str) -> str:
        if not _SHA.fullmatch(v):
            raise ValueError("bad hash")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if unicodedata.normalize("NFC", v) != v:
            raise ValueError("bad path")
        parts = v.split("/")
        if v.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\x00" in v:
            raise ValueError("bad path")
        return v


class DocumentEntry(_Strict):
    document_id: StrictStr
    version: StrictInt = Field(ge=1, le=999999)
    title: StrictStr = Field(min_length=1, max_length=200)
    origin: Literal["dive_professional", "published_document"]
    author_role: Literal["dive_professional", "published_source"]
    effective_date: date
    review_by_date: date
    licence: StrictStr = Field(min_length=1, max_length=500)
    licence_reference: StrictStr = Field(min_length=1, max_length=500)  # checked, then discarded
    attribution: StrictStr | None = Field(default=None, min_length=1, max_length=300)
    licence_requires_personal_name: StrictBool
    site_id: StrictStr | None = None
    all_sites: StrictBool
    display_permitted: StrictBool
    display_permission_reference: StrictStr = Field(min_length=1, max_length=500)  # discarded
    supersedes: StrictInt | None = Field(default=None, ge=1, le=999999)
    file: FileEntry

    @field_validator("document_id")
    @classmethod
    def _id(cls, v: str) -> str:
        if len(v) > 40 or not _ID.fullmatch(v) or kb_screen.document_id_reads_as_instruction(v):
            raise ValueError("bad id")
        return v

    @field_validator("title", "licence", "licence_reference", "attribution")
    @classmethod
    def _texts(cls, v: str | None) -> str | None:
        return None if v is None else _field_text(v)

    @field_validator("licence_requires_personal_name")
    @classmethod
    def _no_personal_name(cls, v: bool) -> bool:
        if v is not False:
            raise ValueError("refused")
        return v

    @field_validator("display_permitted")
    @classmethod
    def _displayable(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("refused")
        return v

    def check_consistency(self) -> None:
        if self.all_sites != (self.site_id is None):
            raise ValueError("site")
        if self.site_id is not None and self.site_id not in SITES:
            raise ValueError("site")
        if self.supersedes is not None and self.supersedes >= self.version:
            raise ValueError("supersedes")
        if not self.effective_date <= self.review_by_date <= add_months(self.effective_date, 12):
            raise ValueError("dates")


class Manifest(_Strict):
    format: StrictInt = Field(ge=1, le=1)
    attests_no_generated_text: StrictBool
    attests_no_personal_names: StrictBool
    documents: tuple[DocumentEntry, ...] = Field(
        min_length=1, max_length=kb_common.MAX_DOCUMENTS_PER_LOAD
    )

    @field_validator("attests_no_generated_text", "attests_no_personal_names")
    @classmethod
    def _attested(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("refused")
        return v


class LoadedChunk(_Strict):
    chunk: int
    section: str
    text: str
    sha256: StrictStr


class LoadedDocument(_Strict):
    entry: DocumentEntry
    chunks: tuple[LoadedChunk, ...]


class LoadedCorpus(_Strict):
    manifest_sha256: str
    documents: tuple[LoadedDocument, ...]


def parse_manifest(raw: bytes) -> tuple[Manifest, str]:
    """Strict parse of the bytes as read; returns the manifest and the SHA-256 of those bytes."""
    if len(raw) > kb_common.MAX_MANIFEST_TOTAL_BYTES:
        raise ManifestTooLarge("the manifest is over the size limit")
    try:
        # Duplicate keys: the strict validator keeps the last one silently, so detect them in a
        # first parse of the same bytes (the hash is of these bytes too), then validate below.
        json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        # JSON mode, strict: arrays become tuples and ISO date strings become dates, but nothing
        # else is coerced (python-mode strict would refuse arrays and date strings).
        manifest = Manifest.model_validate_json(raw)
        for doc in manifest.documents:
            doc.check_consistency()
    except (ValueError, ValidationError, RecursionError, TypeError):
        raise ManifestInvalid("the manifest is not valid") from None
    ids = [(d.document_id, d.version) for d in manifest.documents]
    paths = [d.file.path for d in manifest.documents]
    if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
        raise ManifestInvalid("the manifest is not valid")
    return manifest, hashlib.sha256(raw).hexdigest()


def _open_dir(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags)


def read_regular_file(
    root_fd: int, relative: str, budget: int, seen: set[tuple[int, int]] | None = None
) -> bytes:
    """Open each component from a directory handle without following links; the final handle must
    be a regular file with one hard link; read at most `budget` bytes (+1 to detect overflow).
    `seen` collects (st_dev, st_ino) of every file read; a repeat (two names, one file: case or
    normalisation aliasing on some filesystems) is refused."""
    parts = relative.split("/")
    fd = os.dup(root_fd)
    try:
        for name in parts[:-1]:
            nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
            dir_fd=fd,
        )
    finally:
        os.close(fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ManifestInvalid("a corpus file is not accepted")
        if seen is not None:
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                raise ManifestInvalid("a corpus file is not accepted")
            seen.add(identity)
        data = b""
        while len(data) <= budget:
            chunk = os.read(file_fd, budget + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(file_fd)
    if len(data) > budget:
        raise ManifestTooLarge("the corpus is over the size limit")
    return data


def _split_chunks(entry: DocumentEntry, data: bytes) -> tuple[LoadedChunk, ...]:
    if hashlib.sha256(data).hexdigest() != entry.file.sha256:
        raise FileHashMismatch("a file hash does not match")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestInvalid("a corpus file is not accepted") from None
    if not whole_file_ok(text):
        raise ManifestInvalid("a corpus file is not accepted")
    spans = sorted(
        [(c.start, c.end, c) for c in entry.file.chunks]
        + [(g.start, g.end, None) for g in entry.file.gaps],
        key=lambda s: s[0],
    )
    position = 0
    for start, end, _ in spans:  # whole file, in order, no gap, no overlap, none empty
        if start != position or end <= start:
            raise ManifestInvalid("a chunk span is not accepted")
        position = end
    if position != len(data):
        raise ManifestInvalid("a chunk span is not accepted")
    # Chunk numbers are checked in manifest order; they equal file order because the spans above
    # are also required to be in order (a sorted walk with no gap or overlap).
    numbers = [c.chunk for c in entry.file.chunks]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ManifestInvalid("a chunk span is not accepted")
    out: list[LoadedChunk] = []
    for c in entry.file.chunks:
        raw = data[c.start : c.end]
        try:
            piece = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ManifestInvalid("a chunk span is not accepted") from None
        if len(piece) > kb_common.MAX_CHUNK_CHARACTERS:
            raise kb_common.LimitExceeded("a chunk is over the length limit")
        if not kb_screen.allowed_text(piece, newlines=True) or kb_screen.contains_evidence_id(
            piece
        ):
            raise ManifestInvalid("a chunk is not accepted")
        if hashlib.sha256(raw).hexdigest() != c.sha256:
            raise ChunkHashMismatch("a chunk hash does not match")
        out.append(LoadedChunk(chunk=c.chunk, section=c.section, text=piece, sha256=c.sha256))
    return tuple(out)


def read_corpus(target: Path) -> LoadedCorpus:
    """Read, hash and validate the whole corpus under `target`. Raises fixed refusals only."""
    try:
        root_fd = _open_dir(target)
    except OSError:
        raise ManifestInvalid("the corpus target cannot be read") from None
    try:
        try:
            budget = kb_common.MAX_MANIFEST_TOTAL_BYTES
            seen: set[tuple[int, int]] = set()
            raw = read_regular_file(root_fd, MANIFEST_FILENAME, budget, seen)
            budget -= len(raw)
            manifest, digest = parse_manifest(raw)
            docs: list[LoadedDocument] = []
            total = 0
            for entry in manifest.documents:
                data = read_regular_file(root_fd, entry.file.path, budget, seen)
                budget -= len(data)
                chunks = _split_chunks(entry, data)
                total += len(chunks)
                if total > kb_common.MAX_CHUNKS_PER_LOAD:
                    raise kb_common.LimitExceeded("the load is over the chunk limit")
                docs.append(LoadedDocument(entry=entry, chunks=chunks))
        except OSError:
            raise ManifestInvalid("a corpus file cannot be read") from None
    finally:
        os.close(root_fd)
    if total == 0 or any(not d.chunks for d in docs):  # an empty version could never be approved
        raise ZeroChunks("the manifest has no chunks")
    return LoadedCorpus(manifest_sha256=digest, documents=tuple(docs))
