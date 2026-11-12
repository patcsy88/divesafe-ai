"""The manifest builder (owner request 2026-11-12): a convenience tool, not a gate. What it prints
must be accepted by the loader's own `read_corpus`; every refusal is a fixed outcome; it never
writes a file. Every text here is synthetic. Mutations run on COPIES of the module."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_common, kb_manifest, kb_manifest_build
from divesafe.services.kb_manifest_build import (
    DOCUMENT_KEYS,
    FAILED_ARGUMENTS,
    FAILED_CHECK,
    FAILED_DRAFT,
    FAILED_FILE,
    FAILED_HEADING,
    FAILED_LIMIT,
    FAILED_LOADER,
    FAILED_PARAGRAPH,
    FAILED_TEXT,
    FAILED_ZERO_CHUNKS,
    OK_BUILD,
    OK_CHECK,
    TOP_KEYS,
    Outcome,
)

LIMIT = kb_common.MAX_CHUNK_CHARACTERS
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


def accepted_by_the_loader(root: Path, raw: bytes) -> kb_manifest.LoadedCorpus:
    (root / "manifest.json").write_bytes(raw)
    loaded = kb_manifest.read_corpus(root)
    assert loaded.manifest_sha256 == hashlib.sha256(raw).hexdigest()
    return loaded


def paragraph(n: int, letter: str = "a") -> str:
    """A one-line paragraph of exactly `n` characters including its newline."""
    return letter * (n - 1) + "\n"


# --- round trip and the chunking rule ------------------------------------------------------------


def test_round_trip_the_loader_accepts_what_is_built(tmp_path: Path) -> None:
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}))
    loaded = accepted_by_the_loader(tmp_path, raw)
    (doc,) = loaded.documents
    assert [(c.chunk, c.section, c.text) for c in doc.chunks] == [
        (1, "Synthetic guide", "Intro line.\n\n"),
        (2, "Entry and exit", "Enter at the steps.\nSecond line.\n\nAnother paragraph.\n"),
        (3, "Hazards", "Boat traffic.\n"),
    ]


def test_human_fields_are_copied_verbatim_and_nothing_is_added(tmp_path: Path) -> None:
    the_draft = draft(document(attribution="Synthetic attribution", supersedes=None))
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}, the_draft))
    built = manifest_of(raw)
    assert list(built) == list(TOP_KEYS)
    for key in TOP_KEYS[:-1]:
        assert built[key] == the_draft[key]
    (entry,) = built["documents"]
    assert list(entry) == [*DOCUMENT_KEYS, "file"]
    for key in DOCUMENT_KEYS:
        assert entry[key] == the_draft["documents"][0][key]
    assert set(entry["file"]) == {"path", "sha256", "chunks", "gaps"}
    assert entry["file"]["sha256"] == hashlib.sha256(GUIDE.encode()).hexdigest()


def test_heading_lines_and_their_blank_lines_are_heading_gaps(tmp_path: Path) -> None:
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}))
    data = GUIDE.encode()
    gaps = manifest_of(raw)["documents"][0]["file"]["gaps"]
    assert [(data[g["start"] : g["end"]], g["reason"]) for g in gaps] == [
        (b"## Entry and exit\n\n", "heading"),
        (b"## Hazards\n", "heading"),
    ]
    accepted_by_the_loader(tmp_path, raw)


def test_multi_chunk_packing_up_to_the_limit(tmp_path: Path) -> None:
    # Two paragraphs filling the limit exactly pack into one chunk; the third starts a new one.
    text = paragraph(799, "a") + "\n" + paragraph(699, "b") + "\n" + paragraph(10, "c")
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": text}))
    doc = accepted_by_the_loader(tmp_path, raw).documents[0]
    assert [len(c.text) for c in doc.chunks] == [LIMIT, 10]
    assert manifest_of(raw)["documents"][0]["file"]["gaps"] == []


def test_many_chunks_and_two_documents(tmp_path: Path) -> None:
    long = "".join(paragraph(900, letter) + "\n" for letter in "abcdef")
    the_draft = draft(document("one.txt"), document("two.txt", document_id="other-guide"))
    raw = kb_manifest_build.build(
        corpus(tmp_path, {"one.txt": long, "two.txt": "## Only\nShort.\n"}, the_draft)
    )
    loaded = accepted_by_the_loader(tmp_path, raw)
    assert [len(d.chunks) for d in loaded.documents] == [6, 1]
    assert [c.chunk for c in loaded.documents[0].chunks] == [1, 2, 3, 4, 5, 6]


def test_a_paragraph_of_exactly_the_limit_is_accepted(tmp_path: Path) -> None:
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": paragraph(LIMIT)}))
    assert len(accepted_by_the_loader(tmp_path, raw).documents[0].chunks[0].text) == LIMIT


def test_characters_not_bytes_count_and_offsets_are_bytes(tmp_path: Path) -> None:
    text = "## Caf\u00e9\n" + "\u00e9" * (LIMIT - 1) + "\n"
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": text}))
    doc = accepted_by_the_loader(tmp_path, raw).documents[0]
    assert doc.chunks[0].section == "Caf\u00e9" and len(doc.chunks[0].text) == LIMIT


def test_no_final_newline(tmp_path: Path) -> None:
    text = "## Start\nfirst line\nlast line"
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": text}))
    doc = accepted_by_the_loader(tmp_path, raw).documents[0]
    assert [c.text for c in doc.chunks] == ["first line\nlast line"]


@pytest.mark.parametrize("line", ["# One hash\n", "###Three\n", "##No space\n", " ## Indented\n"])
def test_other_hash_lines_are_body_text_which_the_loader_screen_refuses(
    tmp_path: Path, line: str
) -> None:
    path = corpus(tmp_path, {"guide.txt": "## Start\n" + line})
    assert outcome(kb_manifest_build, path) == FAILED_TEXT


def test_determinism_same_input_same_bytes(tmp_path: Path) -> None:
    text = GUIDE + "".join(paragraph(400, letter) + "\n" for letter in "xyz")
    first = kb_manifest_build.build(corpus(tmp_path / "a", {"guide.txt": text}))
    second = kb_manifest_build.build(corpus(tmp_path / "b", {"guide.txt": text}))
    assert first == second and first == kb_manifest_build.build(tmp_path / "a" / "draft.json")
    assert first.endswith(b"}\n") and not first.endswith(b"\n\n")


# --- refusals ------------------------------------------------------------------------------------


def test_an_oversize_paragraph_is_refused_never_split(tmp_path: Path) -> None:
    path = corpus(tmp_path, {"guide.txt": "Short.\n\n" + paragraph(LIMIT + 1)})
    assert outcome(kb_manifest_build, path) == FAILED_PARAGRAPH


def test_an_oversize_paragraph_of_many_lines_is_refused(tmp_path: Path) -> None:
    text = paragraph(LIMIT // 2) + paragraph(LIMIT // 2) + paragraph(5)
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": text})) == FAILED_PARAGRAPH


@pytest.mark.parametrize("key", [*TOP_KEYS])
def test_a_missing_top_level_field_is_refused(tmp_path: Path, key: str) -> None:
    the_draft = draft()
    del the_draft[key]
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": GUIDE}, the_draft)) == (
        FAILED_DRAFT
    )


@pytest.mark.parametrize("key", [*DOCUMENT_KEYS, "path"])
def test_a_missing_document_field_is_refused(tmp_path: Path, key: str) -> None:
    """Never defaulted: `null` fields too (attribution, site_id, supersedes) must be written."""
    doc = document()
    del doc[key]
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": GUIDE}, draft(doc))) == (
        FAILED_DRAFT
    )


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({**draft(), "extra": 1}).encode(),
        json.dumps(draft(document(file={"path": "guide.txt"}))).encode(),
        json.dumps(draft(document(chunks=[]))).encode(),
        json.dumps(draft()).replace('"format": 1,', '"format": 1, "format": 1,').encode(),
        json.dumps(draft()).replace('"version": 1,', '"version": 1, "version": 2,').encode(),
        json.dumps({**draft(), "documents": []}).encode(),
        json.dumps({**draft(), "documents": {}}).encode(),
        json.dumps(draft(document(path=7))).encode(),
        b"[]",
        b"{",
        b"\xff",
    ],
)
def test_an_extra_repeated_or_malformed_draft_is_refused(tmp_path: Path, raw: bytes) -> None:
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": GUIDE}, raw)) == FAILED_DRAFT


@pytest.mark.parametrize(
    ("top", "doc"),
    [
        ({"attests_no_generated_text": False}, {}),
        ({"attests_no_personal_names": False}, {}),
        ({"attests_no_generated_text": None}, {}),
        ({"format": 2}, {}),
        ({}, {"licence_requires_personal_name": True}),
        ({}, {"display_permitted": False}),
        ({}, {"licence": ""}),
        ({}, {"site_id": None, "all_sites": False}),
        ({}, {"supersedes": 1}),
        ({}, {"review_by_date": "2027-02-01"}),
        ({}, {"title": "Guide kb:x"}),
        ({}, {"document_id": "Bad Id"}),
    ],
)
def test_values_are_judged_by_the_loader_parser_never_corrected(
    tmp_path: Path, top: dict[str, Any], doc: dict[str, Any]
) -> None:
    """The file starts with a heading, so the title is not used as a section here."""
    text = "## Start\nBody.\n"
    path = corpus(tmp_path, {"guide.txt": text}, draft(document(**doc), **top))
    assert outcome(kb_manifest_build, path) == FAILED_LOADER


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Body \u201cquoted\u201d.\n", FAILED_TEXT),
        ("Body with a dash \u2014 here.\n", FAILED_TEXT),
        ("Body with a zero\u200bwidth space.\n", FAILED_TEXT),
        ("Body citing kb:site-guide:1:1 here.\n", FAILED_TEXT),
        ("## Hazards \u201cnow\u201d\nBody.\n", FAILED_HEADING),
        ("## \nBody.\n", FAILED_HEADING),
        ("## " + "h" * 201 + "\nBody.\n", FAILED_HEADING),
        ("Body.\r\n", FAILED_FILE),
        ("Cafe\u0301 decomposed.\n", FAILED_FILE),
        ("\n\n", FAILED_ZERO_CHUNKS),
        ("## Only a heading\n\n", FAILED_ZERO_CHUNKS),
    ],
)
def test_text_the_loader_refuses_is_refused(tmp_path: Path, text: str, expected: Outcome) -> None:
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": text})) == expected


def test_a_title_the_section_rules_refuse_is_refused_before_the_first_heading(
    tmp_path: Path,
) -> None:
    path = corpus(tmp_path, {"guide.txt": GUIDE}, draft(document(title="Guide kb:x")))
    assert outcome(kb_manifest_build, path) == FAILED_HEADING


def test_bytes_that_are_not_utf8_are_refused(tmp_path: Path) -> None:
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": b"ab\xff\n"})) == FAILED_FILE


@pytest.mark.parametrize("path", ["../guide.txt", "/etc/hosts", "./guide.txt", "a//b", "missing"])
def test_a_path_outside_or_missing_is_refused(tmp_path: Path, path: str) -> None:
    path_file = corpus(tmp_path / "c", {"guide.txt": GUIDE}, draft(document(path)))
    (tmp_path / "guide.txt").write_text(GUIDE)
    assert outcome(kb_manifest_build, path_file) == FAILED_FILE


def test_a_symlink_a_hard_link_and_the_draft_itself_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "c"
    corpus(root, {"guide.txt": GUIDE})
    os.symlink(root / "guide.txt", root / "link.txt")
    os.link(root / "guide.txt", root / "hard.txt")
    for name in ("link.txt", "hard.txt", "draft.json"):
        (root / "draft.json").write_text(json.dumps(draft(document(name))))
        assert outcome(kb_manifest_build, root / "draft.json") == FAILED_FILE, name


def test_the_draft_cannot_be_named_manifest_json(tmp_path: Path) -> None:
    corpus(tmp_path, {"guide.txt": GUIDE})
    (tmp_path / "draft.json").rename(tmp_path / "manifest.json")
    assert outcome(kb_manifest_build, tmp_path / "manifest.json") == FAILED_ARGUMENTS


def test_over_the_chunk_limit_is_refused(tmp_path: Path) -> None:
    text = "".join(paragraph(LIMIT - 1) + "\n" for _ in range(kb_common.MAX_CHUNKS_PER_LOAD + 1))
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": text})) == FAILED_LIMIT


def test_over_the_size_limit_is_refused(tmp_path: Path) -> None:
    text = (paragraph(999) + "\n") * (kb_common.MAX_MANIFEST_TOTAL_BYTES // 1000 + 1)
    assert outcome(kb_manifest_build, corpus(tmp_path, {"guide.txt": text})) == FAILED_LIMIT


def _validate_outcome(raw: bytes, files: list[bytes]) -> Outcome:
    try:
        kb_manifest_build.validate(raw, files)
    except kb_manifest_build.BuildRefused as refused:
        return refused.outcome
    return OK_BUILD


def test_validate_alone_refuses_over_the_chunk_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each layer on its own: the final validation, without the earlier count in `build`."""
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}))
    assert _validate_outcome(raw, [GUIDE.encode()]) == OK_BUILD
    monkeypatch.setattr(kb_common, "MAX_CHUNKS_PER_LOAD", 2)
    assert _validate_outcome(raw, [GUIDE.encode()]) == FAILED_LIMIT


def test_validate_alone_refuses_over_the_total_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest and the files together, as `read_corpus` budgets them."""
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}))
    total = len(raw) + len(GUIDE.encode())
    monkeypatch.setattr(kb_common, "MAX_MANIFEST_TOTAL_BYTES", total)
    assert _validate_outcome(raw, [GUIDE.encode()]) == OK_BUILD
    monkeypatch.setattr(kb_common, "MAX_MANIFEST_TOTAL_BYTES", total - 1)
    assert _validate_outcome(raw, [GUIDE.encode()]) == FAILED_LIMIT


@pytest.mark.parametrize("data", [b"## Only a heading\n", b"## One\n\n## Two\n"])
def test_chunk_file_alone_refuses_zero_chunks(data: bytes) -> None:
    with pytest.raises(kb_manifest_build.BuildRefused) as refused:
        kb_manifest_build.chunk_file(data, "Synthetic guide")
    assert refused.value.outcome == FAILED_ZERO_CHUNKS


# --- the command line: stdout once, fixed stderr, never a file written ---------------------------


def _tree(root: Path) -> dict[str, tuple[int, int, bytes]]:
    out = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        out[str(path)] = (info.st_mode, info.st_mtime_ns, content)
    return out


def guard_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fails any open for writing, creation or truncation, through os.open and builtins.open."""
    attempts: list[str] = []
    real_open = os.open
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guarded(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & write_flags:
            attempts.append("os.open")
            raise AssertionError("opened for writing")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    real_builtin = open

    def guarded_builtin(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(ch in mode for ch in "wax+"):
            attempts.append("open")
            raise AssertionError("opened for writing")
        return real_builtin(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_builtin)
    monkeypatch.setattr("io.open", guarded_builtin)
    return attempts


@pytest.mark.parametrize("text", [GUIDE, "Body \u201cquoted\u201d.\n"])
def test_main_never_writes_a_file(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes], text: str
) -> None:
    """Build and --check, accepted and refused: no open for writing, the tree unchanged."""
    path = corpus(tmp_path, {"guide.txt": text})
    before = _tree(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        attempts = guard_writes(patch)
        kb_manifest_build.main([str(path)])
        kb_manifest_build.main(["--check", str(tmp_path)])
    assert attempts == []
    assert _tree(tmp_path) == before
    capsysbinary.readouterr()


def test_the_write_guard_has_teeth(tmp_path: Path) -> None:
    with pytest.MonkeyPatch.context() as patch:
        attempts = guard_writes(patch)
        with pytest.raises(AssertionError):
            (tmp_path / "x").write_bytes(b"x")
        with pytest.raises(AssertionError):
            os.open(tmp_path / "y", os.O_WRONLY | os.O_CREAT)
    assert attempts == ["open", "os.open"]


def test_main_prints_the_validated_bytes_once_and_a_fixed_message(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    path = corpus(tmp_path, {"guide.txt": GUIDE})
    assert kb_manifest_build.main([str(path)]) == 0
    out, err = capsysbinary.readouterr()
    assert out == kb_manifest_build.build(path)
    assert err == (OK_BUILD.message + "\n").encode()


@pytest.mark.parametrize(
    "text",
    ["Body \u201cquoted\u201d.\n", "## Bad\u2014 heading\nx\n", paragraph(LIMIT + 1), "x\r\n"],
)
def test_a_refusal_prints_nothing_on_stdout_and_echoes_nothing(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes], text: str
) -> None:
    path = corpus(tmp_path / "secret-dir-name", {"guide.txt": text})
    status = kb_manifest_build.main([str(path)])
    out, err = capsysbinary.readouterr()
    assert status != 0 and out == b""
    messages = {o.message for o in vars(kb_manifest_build).values() if isinstance(o, Outcome)}
    assert err.decode().removesuffix("\n") in messages
    assert b"secret-dir-name" not in err and b"guide" not in err


@pytest.mark.parametrize("argv", [[], ["a", "b"], ["--check"], ["--other", "x"], ["-x"]])
def test_bad_arguments(argv: list[str], capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert kb_manifest_build.main(argv) == FAILED_ARGUMENTS.status
    assert capsysbinary.readouterr().out == b""


def test_check_prints_ids_versions_chunk_counts_and_the_manifest_hash(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}))
    (tmp_path / "manifest.json").write_bytes(raw)
    assert kb_manifest_build.main(["--check", str(tmp_path)]) == 0
    out, err = capsysbinary.readouterr()
    assert out.decode() == (
        f"manifest_sha256 {hashlib.sha256(raw).hexdigest()}\n"
        "document site-guide version 1 chunks 3\n"
    )
    assert err == (OK_CHECK.message + "\n").encode()


def test_check_refuses_what_the_loader_refuses(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}))
    (tmp_path / "manifest.json").write_bytes(raw)
    (tmp_path / "guide.txt").write_text(GUIDE + "x\n")  # the file changed after the build
    assert kb_manifest_build.main(["--check", str(tmp_path)]) == FAILED_CHECK.status
    assert kb_manifest_build.main(["--check", str(tmp_path / "missing")]) == FAILED_CHECK.status
    out, err = capsysbinary.readouterr()
    assert out == b"" and err == (FAILED_CHECK.message + "\n").encode() * 2


# --- mutations (on COPIES) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "old", "new", "text", "expected"),
    [
        (
            "no paragraph limit",
            "            if len(unit.text) > kb_common.MAX_CHUNK_CHARACTERS:\n"
            "                raise BuildRefused(FAILED_PARAGRAPH)\n",
            "",
            paragraph(LIMIT + 1),
            FAILED_PARAGRAPH,
        ),
        (
            "no chunk text screen",
            "            raise BuildRefused(FAILED_TEXT)\n",
            "            pass\n",
            "Body \u201cquoted\u201d.\n",
            FAILED_TEXT,
        ),
        (
            "no heading screen",
            "            if not _section_ok(section):\n"
            "                raise BuildRefused(FAILED_HEADING)\n",
            "",
            "## Hazards \u201cnow\u201d\nBody.\n",
            FAILED_HEADING,
        ),
        (
            "no whole-file check",
            "    if not kb_manifest.whole_file_ok(text):\n"
            "        raise BuildRefused(FAILED_FILE)\n",
            "",
            "Body.\r\n",
            FAILED_FILE,
        ),
    ],
)
def test_mutation_each_builder_refusal_is_caught(
    tmp_path: Path, label: str, old: str, new: str, text: str, expected: Outcome
) -> None:
    path = corpus(tmp_path, {"guide.txt": text})
    assert outcome(kb_manifest_build, path) == expected
    assert outcome(mutated_module(kb_manifest_build, (old, new)), path) != expected, label


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        (
            "exact key set becomes subset",
            "set(doc) != {*DOCUMENT_KEYS, PATH_KEY}",
            "not set(doc) <= {*DOCUMENT_KEYS, PATH_KEY}",
        ),
        (
            "a default for a missing field",
            "        if not isinstance(doc, dict) or",
            "        if isinstance(doc, dict):\n            doc.setdefault('attribution', None)\n"
            "        if not isinstance(doc, dict) or",
        ),
    ],
)
def test_mutation_a_defaulted_field_is_caught(
    tmp_path: Path, label: str, old: str, new: str
) -> None:
    doc = document()
    del doc["attribution"]
    path = corpus(tmp_path, {"guide.txt": GUIDE}, draft(doc))
    assert outcome(kb_manifest_build, path) == FAILED_DRAFT
    assert outcome(mutated_module(kb_manifest_build, (old, new)), path) != FAILED_DRAFT, label


def test_mutation_duplicate_keys_allowed_is_caught(tmp_path: Path) -> None:
    raw = json.dumps(draft()).replace('"format": 1,', '"format": 1, "format": 1,').encode()
    path = corpus(tmp_path, {"guide.txt": GUIDE}, raw)
    mutant = mutated_module(
        kb_manifest_build, ("object_pairs_hook=_no_duplicate_keys", "strict=True")
    )
    assert outcome(mutant, path) != FAILED_DRAFT


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("an end offset one short", '"end": start + len(raw),', '"end": start + len(raw) - 1,'),
        (
            "a wrong chunk hash",
            "hashlib.sha256(raw).hexdigest(),\n            }",
            "hashlib.sha256(raw + b'x').hexdigest(),\n            }",
        ),
        ("numbers from zero", '"chunk": len(chunks) + 1,', '"chunk": len(chunks),'),
    ],
)
def test_mutation_a_wrong_computation_is_caught_by_the_loader_checks(
    tmp_path: Path, label: str, old: str, new: str
) -> None:
    """The final validation runs the loader's own parser and `_split_chunks`."""
    path = corpus(tmp_path, {"guide.txt": GUIDE})
    assert outcome(mutated_module(kb_manifest_build, (old, new)), path) == FAILED_LOADER, label


def test_mutation_without_the_final_validation_a_wrong_computation_is_printed(
    tmp_path: Path,
) -> None:
    """Shows the validation above has teeth: without it the broken manifest is returned, and
    the loader refuses it."""
    path = corpus(tmp_path, {"guide.txt": GUIDE})
    mutant = mutated_module(
        kb_manifest_build,
        ('"end": start + len(raw),', '"end": start + len(raw) - 1,'),
        ("    validate(raw, files)\n    return raw\n", "    return raw\n"),
    )
    raw = mutant.build(path)
    with pytest.raises(kb_manifest.ManifestInvalid):
        accepted_by_the_loader(tmp_path, raw)


def test_mutation_the_validated_bytes_changed_before_printing_is_caught(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    path = corpus(tmp_path, {"guide.txt": GUIDE})
    mutant = mutated_module(
        kb_manifest_build, ("sys.stdout.buffer.write(out)", "sys.stdout.buffer.write(out + b'\\n')")
    )
    mutant.main([str(path)])
    out, _ = capsysbinary.readouterr()
    assert out != kb_manifest_build.build(path)
