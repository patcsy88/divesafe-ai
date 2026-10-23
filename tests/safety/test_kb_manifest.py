"""The manifest and corpus reader (ADR 0022 decisions 6, 7, 12, 13; ADR 0024 A3, Q6): limits,
hashes, file kinds, dates and the consistency rules. Offline, on a temporary directory.
Mutations run on COPIES of the module source, never on `src`."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from tests.safety.kb_corpus import SITE, doc_spec, sha, write_corpus
from tests.safety.kb_fakes import mutated_module

from divesafe.services import kb_common, kb_manifest
from divesafe.services.kb_common import LimitExceeded
from divesafe.services.kb_manifest import (
    ChunkHashMismatch,
    FileHashMismatch,
    ManifestInvalid,
    ManifestTooLarge,
    ZeroChunks,
    read_corpus,
)

pytestmark = pytest.mark.safety

CAUGHT = (AssertionError, Exception)


def read(root: Path, module: Any = kb_manifest) -> Any:
    return module.read_corpus(root)


def refused(root: Path, module: Any = kb_manifest) -> type[BaseException] | None:
    try:
        module.read_corpus(root)
    except kb_common.LoaderRefusal as exc:
        return type(exc).__name__  # type: ignore[return-value]
    return None


def build(tmp_path: Path, specs: list[dict[str, Any]] | None = None, **top: Any) -> Path:
    root = tmp_path / "corpus"
    write_corpus(root, specs, top=top or None)
    return root


def edit_manifest(root: Path, change: Any) -> None:
    path = root / "manifest.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


# --- the happy path ------------------------------------------------------------------------------


def test_a_valid_corpus_is_read_with_the_manifest_hash(tmp_path: Path) -> None:
    root = tmp_path / "c"
    digest = write_corpus(root, [doc_spec("alpha-guide"), doc_spec("beta-guide")])
    corpus = read_corpus(root)
    assert corpus.manifest_sha256 == digest == sha((root / "manifest.json").read_bytes())
    assert [d.entry.document_id for d in corpus.documents] == ["alpha-guide", "beta-guide"]
    assert [c.text for c in corpus.documents[0].chunks] == ["Alpha text.\n", "Beta text.\n"]


def test_the_licence_and_permission_references_are_checked_but_never_sent_to_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from pydantic import SecretStr
    from tests.safety.kb_fakes import LoadFakeConnection, Out

    from divesafe.config.loader_settings import LoaderSettings
    from divesafe.services import kb_load

    root = tmp_path / "c"
    digest = write_corpus(root)
    fake = LoadFakeConnection()
    monkeypatch.setattr(kb_common, "connect", lambda dsn: fake)
    monkeypatch.setattr(sys, "stdout", Out(fake))
    settings = LoaderSettings(
        environment="test",
        loader_database_url=SecretStr("postgresql://u:never-echoed@127.0.0.1:1/x_test"),
        corpus_target=root,
    )
    request = kb_load.parse_arguments(["load", "--approved-manifest-sha256", digest])
    assert kb_load.run(request, settings).status == 0
    sent = "\n".join(text for _, text in fake.log)
    assert "synthetic-permission-ref" not in sent
    assert "synthetic-licence-ref" not in sent
    assert "Synthetic test licence" in sent  # the licence NAME is stored; the reference is not


# --- size and count limits (each with a mutation) -------------------------------------------------


def test_a_manifest_over_one_mebibyte_is_too_large(tmp_path: Path) -> None:
    raw = b" " * (kb_common.MAX_MANIFEST_TOTAL_BYTES + 1)
    with pytest.raises(ManifestTooLarge):
        kb_manifest.parse_manifest(raw)
    root = build(tmp_path)
    (root / "manifest.json").write_bytes(raw)
    assert refused(root) == "ManifestTooLarge"


def test_a_manifest_of_exactly_one_mebibyte_is_not_refused_for_size() -> None:
    raw = b" " * kb_common.MAX_MANIFEST_TOTAL_BYTES
    with pytest.raises(ManifestInvalid):  # empty, not too large
        kb_manifest.parse_manifest(raw)


def test_the_total_corpus_bytes_share_the_one_mebibyte_budget(tmp_path: Path) -> None:
    root = build(tmp_path)
    big = b"x" * kb_common.MAX_MANIFEST_TOTAL_BYTES
    spec = doc_spec(chunks=("Alpha text.\n",))
    (root / spec["entry"]["file"]["path"]).write_bytes(big)
    assert refused(root) == "ManifestTooLarge"


def too_many_documents(tmp_path: Path, count: int) -> Path:
    specs = [doc_spec(f"guide-{n}") for n in range(count)]
    return build(tmp_path, specs)


def test_fifty_documents_are_allowed_and_fifty_one_are_refused(tmp_path: Path) -> None:
    assert refused(too_many_documents(tmp_path / "a", 50)) is None
    assert refused(too_many_documents(tmp_path / "b", 51)) == "ManifestInvalid"


def many_chunks(count: int) -> dict[str, Any]:
    return doc_spec("big-guide", chunks=tuple(f"Line {i}.\n" for i in range(count)))


def test_two_hundred_chunks_are_allowed_and_two_hundred_and_one_are_refused(
    tmp_path: Path,
) -> None:
    assert refused(build(tmp_path / "a", [many_chunks(200)])) is None
    assert refused(build(tmp_path / "b", [many_chunks(201)])) == "ManifestInvalid"


def test_the_chunk_limit_counts_across_documents(tmp_path: Path) -> None:
    half = [
        doc_spec("one-guide", chunks=tuple(f"A{i}.\n" for i in range(101))),
        doc_spec("two-guide", chunks=tuple(f"B{i}.\n" for i in range(100))),
    ]
    assert refused(build(tmp_path, half)) == "LimitExceeded"


def test_a_chunk_of_1500_characters_is_allowed_and_1501_is_refused(tmp_path: Path) -> None:
    ok = doc_spec("long-guide", chunks=("a" * 1499 + "\n",))
    assert refused(build(tmp_path / "a", [ok])) is None
    over = doc_spec("long-guide", chunks=("a" * 1500 + "\n",))
    assert refused(build(tmp_path / "b", [over])) == "LimitExceeded"


def test_the_character_limit_counts_characters_not_bytes(tmp_path: Path) -> None:
    text = "é" * 1000 + "\n"  # 2001 bytes, 1001 characters
    assert refused(build(tmp_path, [doc_spec("accent-guide", chunks=(text,))])) is None


LIMIT_MUTATIONS = [
    ("if total > kb_common.MAX_CHUNKS_PER_LOAD:", "if False:", "chunks"),
    ("if len(piece) > kb_common.MAX_CHUNK_CHARACTERS:", "if False:", "characters"),
    ("if len(piece) > kb_common.MAX_CHUNK_CHARACTERS:", "if len(piece) > 100000:", "characters"),
    ("if len(data) > budget:", "if False:", "budget"),
]


def scenario(kind: str, base: Path) -> Path:
    if kind == "size":
        root = build(base)
        (root / "manifest.json").write_bytes(b" " * (kb_common.MAX_MANIFEST_TOTAL_BYTES + 1))
        return root
    if kind == "chunks":
        half = [
            doc_spec("one-guide", chunks=tuple(f"A{i}.\n" for i in range(101))),
            doc_spec("two-guide", chunks=tuple(f"B{i}.\n" for i in range(100))),
        ]
        return build(base, half)
    if kind == "characters":
        return build(base, [doc_spec("long-guide", chunks=("a" * 1500 + "\n",))])
    root = build(base)
    spec = doc_spec(chunks=("Alpha text.\n",))
    (root / spec["entry"]["file"]["path"]).write_bytes(b"x" * kb_common.MAX_MANIFEST_TOTAL_BYTES)
    return root


@pytest.mark.parametrize(("old", "new", "kind"), LIMIT_MUTATIONS)
def test_mutation_a_removed_limit_is_caught(old: str, new: str, kind: str, tmp_path: Path) -> None:
    mutant = mutated_module(kb_manifest, (old, new))
    root = scenario(kind, tmp_path)
    assert refused(root) is not None  # the real module refuses
    assert refused(root, mutant) != refused(root)  # the mutant does not, or refuses differently


def test_mutation_the_parse_size_check_removed_is_caught() -> None:
    mutant = mutated_module(
        kb_manifest, ("if len(raw) > kb_common.MAX_MANIFEST_TOTAL_BYTES:", "if False:")
    )
    raw = b" " * (kb_common.MAX_MANIFEST_TOTAL_BYTES + 1)
    with pytest.raises(ManifestTooLarge):
        kb_manifest.parse_manifest(raw)
    with pytest.raises(kb_common.LoaderRefusal) as caught:
        mutant.parse_manifest(raw)
    assert type(caught.value).__name__ == "ManifestInvalid"  # without the check: fails as not JSON


def test_mutation_the_read_budget_removed_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(kb_manifest, ("    if len(data) > budget:", "    if False:"))
    root = build(tmp_path)
    spec = doc_spec(chunks=("Alpha text.\n",))
    (root / spec["entry"]["file"]["path"]).write_bytes(b"x" * kb_common.MAX_MANIFEST_TOTAL_BYTES)
    assert refused(root) == "ManifestTooLarge"
    assert refused(root, mutant) == "FileHashMismatch"  # it read the oversized file in full


def test_mutation_a_document_count_limit_removed_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        (
            "documents: tuple[DocumentEntry, ...] = Field(\n        min_length=1, max_length=kb_common.MAX_DOCUMENTS_PER_LOAD\n    )",  # noqa: E501
            "documents: tuple[DocumentEntry, ...] = Field(min_length=1)",
        ),
    )
    root = too_many_documents(tmp_path, 51)
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) is None


# --- zero chunks ---------------------------------------------------------------------------------


def test_a_corpus_with_no_chunks_is_refused(tmp_path: Path) -> None:
    root = build(tmp_path, [doc_spec("only-guide", chunks=(), gap="Heading\n")])
    assert refused(root) == "ZeroChunks"


def test_one_document_without_chunks_is_refused_even_when_others_have_some(
    tmp_path: Path,
) -> None:
    specs = [doc_spec("alpha-guide"), doc_spec("beta-guide", chunks=(), gap="Heading\n")]
    assert refused(build(tmp_path, specs)) == "ZeroChunks"


def test_mutation_checking_only_the_total_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest, ("if total == 0 or any(not d.chunks for d in docs):", "if total == 0:")
    )
    specs = [doc_spec("alpha-guide"), doc_spec("beta-guide", chunks=(), gap="Heading\n")]
    root = build(tmp_path, specs)
    assert refused(root) == "ZeroChunks"
    assert refused(root, mutant) is None


# --- unknown fields and shapes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(extra=1),
        lambda d: d["documents"][0].update(unknown="x"),
        lambda d: d["documents"][0]["file"].update(unknown="x"),
        lambda d: d["documents"][0]["file"]["chunks"][0].update(unknown="x"),
        lambda d: d.update(format=2),
        lambda d: d.update(attests_no_generated_text=False),
        lambda d: d.update(attests_no_personal_names=False),
        lambda d: d.pop("attests_no_generated_text"),
        lambda d: d.update(documents=[]),
        lambda d: d["documents"][0].update(licence_requires_personal_name=True),
        lambda d: d["documents"][0].update(display_permitted=False),
        lambda d: d["documents"][0].update(display_permitted="true"),
        lambda d: d["documents"][0].update(origin="llm"),
        lambda d: d["documents"][0].update(author_role="llm"),
        lambda d: d["documents"][0].update(version=0),
    ],
    ids=lambda f: "",
)
def test_an_unknown_field_or_a_wrong_attestation_is_refused(tmp_path: Path, change: Any) -> None:
    root = build(tmp_path)
    edit_manifest(root, change)
    assert refused(root) == "ManifestInvalid"


@pytest.mark.parametrize("field", ["licence_reference", "display_permission_reference"])
def test_the_references_are_required_and_not_empty(tmp_path: Path, field: str) -> None:
    root = build(tmp_path / "missing")
    edit_manifest(root, lambda d: d["documents"][0].pop(field))
    assert refused(root) == "ManifestInvalid"
    root = build(tmp_path / "empty")
    edit_manifest(root, lambda d: d["documents"][0].update({field: ""}))
    assert refused(root) == "ManifestInvalid"


def test_nan_and_infinity_are_refused(tmp_path: Path) -> None:
    for constant in ("NaN", "Infinity", "-Infinity"):
        root = build(tmp_path / constant)
        path = root / "manifest.json"
        path.write_text(path.read_text().replace('"version": 1', f'"version": {constant}'))
        assert refused(root) == "ManifestInvalid"


def test_not_json_not_utf8_and_deep_nesting_are_refused(tmp_path: Path) -> None:
    for raw in (b"not json", b"\xff\xfe", b"[" * 100000, b"null", b"[]", b'"x"'):
        root = build(tmp_path / str(abs(hash(raw))))
        (root / "manifest.json").write_bytes(raw)
        assert refused(root) == "ManifestInvalid"


def test_duplicate_ids_and_duplicate_paths_are_refused(tmp_path: Path) -> None:
    root = build(tmp_path / "ids", [doc_spec("alpha-guide"), doc_spec("beta-guide")])
    edit_manifest(root, lambda d: d["documents"][1].update(document_id="alpha-guide"))
    assert refused(root) == "ManifestInvalid"
    root = build(tmp_path / "paths", [doc_spec("alpha-guide"), doc_spec("beta-guide")])
    edit_manifest(
        root, lambda d: d["documents"][1]["file"].update(path=d["documents"][0]["file"]["path"])
    )
    assert refused(root) == "ManifestInvalid"


def test_mutation_allowing_duplicates_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        ("    if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):", "    if False:"),
    )
    root = build(tmp_path, [doc_spec("alpha-guide"), doc_spec("beta-guide")])
    edit_manifest(root, lambda d: d["documents"][1].update(document_id="alpha-guide"))
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) != "ManifestInvalid"


@pytest.mark.parametrize(
    "bad_path", ["/etc/passwd", "../x.txt", "a/../b.txt", "./x.txt", "a//b.txt", "", "x\x00y"]
)
def test_unsafe_paths_are_refused(tmp_path: Path, bad_path: str) -> None:
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0]["file"].update(path=bad_path))
    assert refused(root) == "ManifestInvalid"


@pytest.mark.parametrize(
    "bad_id",
    [
        "Alpha",
        "a_b",
        "a--b",
        "a-b-c-d",
        "-a",
        "a-",
        "x" * 41,
        "ignore-me",
        "go",
        "system-notes",
        "a b",
    ],
)
def test_bad_document_ids_are_refused(tmp_path: Path, bad_id: str) -> None:
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0].update(document_id=bad_id))
    assert refused(root) == "ManifestInvalid"


# --- consistency: site, supersedes, dates --------------------------------------------------------


def test_all_sites_must_be_the_opposite_of_a_site_id(tmp_path: Path) -> None:
    for site, all_sites in ((SITE, True), (None, False)):
        root = build(tmp_path / f"{site}{all_sites}")
        edit_manifest(
            root,
            lambda d, s=site, a=all_sites: d["documents"][0].update(site_id=s, all_sites=a),
        )
        assert refused(root) == "ManifestInvalid"
    root = build(tmp_path / "ok")
    edit_manifest(root, lambda d: d["documents"][0].update(site_id=SITE, all_sites=False))
    assert refused(root) is None


def test_a_site_id_must_be_a_known_site(tmp_path: Path) -> None:
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0].update(site_id="no-such-site", all_sites=False))
    assert refused(root) == "ManifestInvalid"


def test_supersedes_must_be_lower_than_the_version(tmp_path: Path) -> None:
    for version, supersedes, ok in ((2, 1, True), (2, 2, False), (2, 3, False), (1, 1, False)):
        root = build(tmp_path / f"{version}{supersedes}", [doc_spec(version=version)])
        edit_manifest(root, lambda d, v=supersedes: d["documents"][0].update(supersedes=v))
        assert (refused(root) is None) is ok


def test_review_by_must_be_within_twelve_months_and_not_before_the_effective_date(
    tmp_path: Path,
) -> None:
    cases = [
        ("2026-01-31", "2026-01-31", True),
        ("2026-01-31", "2027-01-31", True),
        ("2026-01-31", "2027-02-01", False),
        ("2026-01-31", "2026-01-30", False),
    ]
    for index, (effective, review, ok) in enumerate(cases):
        root = build(tmp_path / str(index))
        edit_manifest(
            root,
            lambda d, e=effective, r=review: d["documents"][0].update(
                effective_date=e, review_by_date=r
            ),
        )
        assert (refused(root) is None) is ok, (effective, review)


def test_add_months_clamps_to_the_end_of_the_target_month() -> None:
    assert kb_manifest.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert kb_manifest.add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert kb_manifest.add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)
    assert kb_manifest.add_months(date(2026, 1, 31), 12) == date(2027, 1, 31)
    assert kb_manifest.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert kb_manifest.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert kb_manifest.add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("min(value.day, calendar.monthrange(year, month)[1])", "value.day"),
        (
            "index = value.year * 12 + (value.month - 1) + months",
            "index = value.year * 12 + value.month + months",
        ),
        ("    month += 1\n", "    month += 0\n"),
    ],
)
def test_mutation_a_wrong_month_calculation_is_caught(old: str, new: str) -> None:
    mutant = mutated_module(kb_manifest, (old, new))
    with pytest.raises(CAUGHT):
        assert mutant.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert mutant.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_mutation_a_looser_review_window_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest, ("add_months(self.effective_date, 12)", "add_months(self.effective_date, 120)")
    )
    root = build(tmp_path)
    edit_manifest(
        root,
        lambda d: d["documents"][0].update(
            effective_date="2026-01-31", review_by_date="2030-01-01"
        ),
    )
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) is None


def test_mutation_dropping_the_site_rule_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        (
            '        if self.all_sites != (self.site_id is None):\n            raise ValueError("site")\n',  # noqa: E501
            "",
        ),
    )
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0].update(site_id=SITE, all_sites=True))
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) is None


# --- hashes and the whole file --------------------------------------------------------------------


def test_a_changed_file_is_a_file_hash_mismatch(tmp_path: Path) -> None:
    root = build(tmp_path)
    (root / "site-guide-v1.txt").write_bytes(b"Alpha text.\nBeta text!\n")
    assert refused(root) == "FileHashMismatch"


def test_a_changed_chunk_hash_with_the_right_file_hash_is_a_chunk_hash_mismatch(
    tmp_path: Path,
) -> None:
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0]["file"]["chunks"][0].update(sha256="0" * 64))
    assert refused(root) == "ChunkHashMismatch"


def test_the_file_hash_is_checked_before_anything_else_is_read_from_the_file(
    tmp_path: Path,
) -> None:
    root = build(tmp_path)
    (root / "site-guide-v1.txt").write_bytes(b"\xff\xfe not utf-8")
    assert refused(root) == "FileHashMismatch"


def test_mutation_skipping_either_hash_is_caught(tmp_path: Path) -> None:
    file_mutant = mutated_module(
        kb_manifest,
        ("    if hashlib.sha256(data).hexdigest() != entry.file.sha256:", "    if False:"),
    )
    chunk_mutant = mutated_module(
        kb_manifest,
        ("        if hashlib.sha256(raw).hexdigest() != c.sha256:", "        if False:"),
    )
    root = build(tmp_path / "file")
    (root / "site-guide-v1.txt").write_bytes(b"Alpha text.\nBeta text!\n")
    assert refused(root) == "FileHashMismatch"
    assert refused(root, file_mutant) != "FileHashMismatch"
    root = build(tmp_path / "chunk")
    edit_manifest(root, lambda d: d["documents"][0]["file"]["chunks"][0].update(sha256="0" * 64))
    assert refused(root) == "ChunkHashMismatch"
    assert refused(root, chunk_mutant) is None


def test_the_whole_file_must_be_nfc_and_free_of_carriage_returns(tmp_path: Path) -> None:
    assert kb_manifest.whole_file_ok("plain text\n")
    assert kb_manifest.whole_file_ok("caf\u00e9\n")
    assert not kb_manifest.whole_file_ok("cafe\u0301\n")  # NFD
    assert not kb_manifest.whole_file_ok("a\rb")
    for index, text in enumerate(("a\r\nb\n", "cafe\u0301\n")):
        spec = doc_spec("raw-guide", chunks=(text,))
        assert refused(build(tmp_path / str(index), [spec])) == "ManifestInvalid"


def test_mutation_whole_file_checks_removed_are_caught() -> None:
    no_cr = mutated_module(kb_manifest, ('and "\\r" not in value', "and True"))
    no_nfc = mutated_module(
        kb_manifest, ('return unicodedata.normalize("NFC", value) == value and', "return True and")
    )
    assert no_cr.whole_file_ok("a\rb") and not kb_manifest.whole_file_ok("a\rb")
    assert no_nfc.whole_file_ok("cafe\u0301") and not kb_manifest.whole_file_ok("cafe\u0301")


def test_spans_must_cover_the_whole_file_in_order_without_gaps_or_overlap(tmp_path: Path) -> None:
    def shift(d: dict[str, Any], key: str, delta: int, index: int = 0) -> None:
        d["documents"][0]["file"]["chunks"][index][key] += delta

    cases = {
        "gap": lambda d: shift(d, "start", 1, 1),
        "overlap": lambda d: shift(d, "start", -1, 1),
        "short": lambda d: shift(d, "end", -1, 1),
        "long": lambda d: shift(d, "end", 1, 1),
        "numbering": lambda d: d["documents"][0]["file"]["chunks"][1].update(chunk=3),
        "empty": lambda d: shift(d, "end", -d["documents"][0]["file"]["chunks"][0]["end"], 0),
    }
    for name, change in cases.items():
        root = build(tmp_path / name)
        edit_manifest(root, change)
        assert refused(root) == "ManifestInvalid", name


def test_a_chunk_with_a_forbidden_character_or_an_evidence_id_is_refused(tmp_path: Path) -> None:
    for index, text in enumerate(
        (
            "zero\u200bwidth\n",
            "kb:other-guide\n",
            "see open-meteo-marine:grid\n",
            "emoji \U0001f600\n",
        )
    ):
        spec = doc_spec("risky-guide", chunks=(text,))
        assert refused(build(tmp_path / str(index), [spec])) == "ManifestInvalid", index


def test_the_manifest_text_fields_are_screened_too(tmp_path: Path) -> None:
    for index, (field, value) in enumerate(
        (("title", "zero\u200bwidth"), ("licence", "kb:abc"), ("title", "line\nbreak"))
    ):
        root = build(tmp_path / str(index))
        edit_manifest(root, lambda d, f=field, v=value: d["documents"][0].update({f: v}))
        assert refused(root) == "ManifestInvalid", (field, index)


# --- file kinds ----------------------------------------------------------------------------------


def test_a_missing_corpus_directory_or_manifest_is_refused(tmp_path: Path) -> None:
    assert refused(tmp_path / "absent") == "ManifestInvalid"
    root = build(tmp_path / "c")
    (root / "manifest.json").unlink()
    assert refused(root) == "ManifestInvalid"
    file_as_root = tmp_path / "plain"
    file_as_root.write_text("x")
    assert refused(file_as_root) == "ManifestInvalid"


def test_a_symlinked_manifest_or_document_is_refused(tmp_path: Path) -> None:
    root = build(tmp_path / "doc")
    target = tmp_path / "elsewhere.txt"
    target.write_bytes((root / "site-guide-v1.txt").read_bytes())
    (root / "site-guide-v1.txt").unlink()
    os.symlink(target, root / "site-guide-v1.txt")
    assert refused(root) == "ManifestInvalid"
    root = build(tmp_path / "man")
    moved = tmp_path / "manifest-elsewhere.json"
    moved.write_bytes((root / "manifest.json").read_bytes())
    (root / "manifest.json").unlink()
    os.symlink(moved, root / "manifest.json")
    assert refused(root) == "ManifestInvalid"


def test_a_symlinked_directory_in_a_path_is_refused(tmp_path: Path) -> None:
    spec = doc_spec("nested-guide", file_path="x") if False else doc_spec("nested-guide")
    spec["entry"]["file"]["path"] = "sub/nested-guide-v1.txt"
    root = tmp_path / "c"
    write_corpus(root, [])  # a placeholder manifest; replaced below
    real = tmp_path / "real-sub"
    real.mkdir()
    (real / "nested-guide-v1.txt").write_bytes(spec["data"])
    os.symlink(real, root / "sub")
    manifest = {
        "format": 1,
        "attests_no_generated_text": True,
        "attests_no_personal_names": True,
        "documents": [spec["entry"]],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert refused(root) == "ManifestInvalid"


def test_a_file_with_more_than_one_hard_link_is_refused(tmp_path: Path) -> None:
    root = build(tmp_path)
    os.link(root / "site-guide-v1.txt", tmp_path / "second-name.txt")
    assert refused(root) == "ManifestInvalid"


def test_a_directory_or_fifo_in_place_of_a_file_is_refused(tmp_path: Path) -> None:
    root = build(tmp_path / "dir")
    (root / "site-guide-v1.txt").unlink()
    (root / "site-guide-v1.txt").mkdir()
    assert refused(root) == "ManifestInvalid"
    root = build(tmp_path / "fifo")
    (root / "site-guide-v1.txt").unlink()
    os.mkfifo(root / "site-guide-v1.txt")
    assert refused(root) == "ManifestInvalid"


def test_mutation_accepting_links_or_special_files_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        ("        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:", "        if False:"),
    )
    root = build(tmp_path)
    os.link(root / "site-guide-v1.txt", tmp_path / "second-name.txt")
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) is None


def test_mutation_following_symlinks_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        (
            "os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,",
            "os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY,",
        ),
    )
    root = build(tmp_path)
    target = tmp_path / "elsewhere.txt"
    target.write_bytes((root / "site-guide-v1.txt").read_bytes())
    (root / "site-guide-v1.txt").unlink()
    os.symlink(target, root / "site-guide-v1.txt")
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) is None


def test_every_refusal_is_a_fixed_message_without_the_path_or_the_text(tmp_path: Path) -> None:
    root = build(tmp_path)
    (root / "site-guide-v1.txt").write_bytes(b"leaked-secret-content\n")
    with pytest.raises(FileHashMismatch) as caught:
        read_corpus(root)
    for needle in ("leaked-secret-content", str(root), "site-guide"):
        assert needle not in str(caught.value)
    assert caught.value.__cause__ is None


def test_refusal_classes_are_distinct_and_all_loader_refusals() -> None:
    classes = (ManifestInvalid, ManifestTooLarge, ZeroChunks, FileHashMismatch, ChunkHashMismatch)
    for cls in classes:
        assert issubclass(cls, kb_common.LoaderRefusal)
    assert issubclass(LimitExceeded, kb_common.LoaderRefusal)
    assert len(set(classes)) == 5
    assert not any(issubclass(a, b) for a in classes for b in classes if a is not b)


# --- strict typing: no value is coerced from another JSON type -------------------------------------  # noqa: E501
# History: pydantic's lax mode used to read JSON `true` as the integer 1 for `version` (AGENTS.md
# section 10: reject malformed data, do not coerce). The model is now strict (StrictInt/StrictStr/
# StrictBool and strict=True); these tests keep it so.


def _doc(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = d["documents"][0]
    return out


def _set(key: str, value: Any, *, top: bool = False, file: bool = False) -> Any:
    def change(d: dict[str, Any]) -> None:
        target = d if top else (_doc(d)["file"] if file else _doc(d))
        target[key] = value

    return change


def _chunk(key: str, value: Any) -> Any:
    def change(d: dict[str, Any]) -> None:
        _doc(d)["file"]["chunks"][0][key] = value

    return change


# (id, change, spec version, lax mode would accept it)
CROSS_TYPE: list[tuple[str, Any, int, bool]] = [
    ("version-true", _set("version", True), 1, True),
    ("version-string", _set("version", "1"), 1, True),
    ("version-float", _set("version", 1.0), 1, True),
    ("format-true", _set("format", True, top=True), 1, True),
    ("format-string", _set("format", "1", top=True), 1, True),
    ("format-two", _set("format", 2, top=True), 1, False),
    ("attests-generated-int", _set("attests_no_generated_text", 1, top=True), 1, True),
    ("attests-generated-false", _set("attests_no_generated_text", False, top=True), 1, False),
    ("attests-names-int", _set("attests_no_personal_names", 1, top=True), 1, True),
    ("attests-names-false", _set("attests_no_personal_names", False, top=True), 1, False),
    ("display-int", _set("display_permitted", 1), 1, True),
    ("display-false", _set("display_permitted", False), 1, False),
    ("personal-name-zero", _set("licence_requires_personal_name", 0), 1, True),
    ("personal-name-true", _set("licence_requires_personal_name", True), 1, False),
    ("title-int", _set("title", 5), 1, False),
    ("document-id-int", _set("document_id", 5), 1, False),
    ("all-sites-string", _set("all_sites", "true"), 1, True),
    ("chunk-true", _chunk("chunk", True), 1, True),
    ("start-string", _chunk("start", "0"), 1, True),
    ("date-int", _set("effective_date", 1767225600), 1, True),
    ("supersedes-true", _set("supersedes", True), 2, True),
]
CASE_IDS = [c[0] for c in CROSS_TYPE]

LAX = [
    (": StrictInt", ": int"),
    (": StrictStr", ": str"),
    (": StrictBool", ": bool"),
]


def _lax_module() -> Any:
    return mutated_module(
        kb_manifest,
        *LAX,
        (
            'model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True, strict=True)',  # noqa: E501
            'model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)',
        ),
        many=True,
    )


def test_a_good_manifest_is_accepted_by_the_strict_model(tmp_path: Path) -> None:
    root = build(tmp_path, [doc_spec(version=2)])
    edit_manifest(root, _set("supersedes", 1))
    assert refused(root) is None


@pytest.mark.parametrize(("name", "change", "version", "lax"), CROSS_TYPE, ids=CASE_IDS)
def test_a_value_of_another_json_type_is_refused(
    name: str, change: Any, version: int, lax: bool, tmp_path: Path
) -> None:
    root = build(tmp_path, [doc_spec(version=version)])
    edit_manifest(root, change)
    assert refused(root) == "ManifestInvalid", name


LAX_CASES = [c for c in CROSS_TYPE if c[3]]


@pytest.mark.parametrize(
    ("name", "change", "version", "lax"), LAX_CASES, ids=[c[0] for c in LAX_CASES]
)
def test_mutation_lax_typing_accepts_what_strict_typing_refuses(
    name: str, change: Any, version: int, lax: bool, tmp_path: Path
) -> None:
    mutant = _lax_module()
    root = build(tmp_path, [doc_spec(version=version)])
    edit_manifest(root, change)
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) is None, name  # the mutant coerces it, so the test has teeth


@pytest.mark.parametrize(
    ("old", "new", "name"),
    [
        (
            '        if v is not False:\n            raise ValueError("refused")',
            '        if False:\n            raise ValueError("refused")',
            "personal-name-true",
        ),
        (
            '        if v is not True:\n            raise ValueError("refused")\n        return v\n\n    def check_consistency',  # noqa: E501
            "        return v\n\n    def check_consistency",
            "display-false",
        ),
    ],
)
def test_mutation_dropping_a_boolean_value_check_is_caught(
    old: str, new: str, name: str, tmp_path: Path
) -> None:
    mutant = mutated_module(kb_manifest, (old, new))
    change = next(c[1] for c in CROSS_TYPE if c[0] == name)
    root = build(tmp_path)
    edit_manifest(root, change)
    assert refused(root) == "ManifestInvalid"
    assert refused(root, mutant) is None


# --- round 8: duplicate keys, O_NOCTTY, aliases of one file, NFC paths ---------------------------


def _with_duplicate(root: Path, old: str, new: str) -> None:
    path = root / "manifest.json"
    text = path.read_text()
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new))


DUPLICATES = [
    pytest.param('"format": 1,', '"format": 1, "format": 1,', id="top-level-same-value"),
    pytest.param(
        '"attests_no_generated_text": true,',
        '"attests_no_generated_text": false, "attests_no_generated_text": true,',
        id="top-level-attestation-overridden",
    ),
    pytest.param(
        '"title": "Synthetic guide",', '"title": "x", "title": "Synthetic guide",', id="doc"
    ),
    pytest.param('"chunk": 1,', '"chunk": 7, "chunk": 1,', id="nested-chunk"),
    pytest.param(
        '"licence_reference": "synthetic-licence-ref",',
        '"licence_reference": "", "licence_reference": "synthetic-licence-ref",',
        id="reference-emptied-then-restored",
    ),
]


@pytest.mark.parametrize(("old", "new"), DUPLICATES)
def test_a_duplicate_json_key_is_refused_even_when_the_last_value_is_valid(
    old: str, new: str, tmp_path: Path
) -> None:
    root = build(tmp_path)
    assert refused(root) is None
    _with_duplicate(root, old, new)
    result = refused(root)
    assert result == "ManifestInvalid"


def test_mutation_without_the_duplicate_key_hook_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        ('raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys', 'raw.decode("utf-8")'),
    )
    root = build(tmp_path)
    _with_duplicate(root, '"chunk": 1,', '"chunk": 7, "chunk": 1,')
    real = refused(root)
    mutated = refused(root, mutant)
    assert real == "ManifestInvalid"
    assert mutated is None  # the strict model keeps the last value silently


def test_mutation_a_hook_that_never_refuses_is_caught_on_a_nested_key(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        ("    if len(set(keys)) != len(keys):", "    if False:"),
    )
    root = build(tmp_path)
    _with_duplicate(
        root, '"title": "Synthetic guide",', '"title": "x", "title": "Synthetic guide",'
    )
    real = refused(root)
    mutated = refused(root, mutant)
    assert real == "ManifestInvalid"
    assert mutated is None


def test_files_are_opened_with_no_follow_non_block_and_no_controlling_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build(tmp_path)
    real_open = os.open
    flags: list[int] = []

    def spy(path: Any, flag: int, *args: Any, **kwargs: Any) -> int:
        if not flag & os.O_DIRECTORY:
            flags.append(flag)
        return real_open(path, flag, *args, **kwargs)

    monkeypatch.setattr(kb_manifest.os, "open", spy)
    assert refused(root) is None
    assert len(flags) >= 2  # the manifest and one document
    for flag in flags:
        assert flag & os.O_NOFOLLOW
        assert flag & os.O_NONBLOCK
        assert flag & os.O_NOCTTY


def test_mutation_without_o_noctty_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = mutated_module(
        kb_manifest,
        (
            "os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,",
            "os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,",
        ),
    )
    root = build(tmp_path)
    real_open = os.open
    flags: list[int] = []

    def spy(path: Any, flag: int, *args: Any, **kwargs: Any) -> int:
        if not flag & os.O_DIRECTORY:
            flags.append(flag)
        return real_open(path, flag, *args, **kwargs)

    monkeypatch.setattr(mutant.os, "open", spy)
    assert refused(root, mutant) is None
    assert flags
    assert all(not f & os.O_NOCTTY for f in flags)  # the real module's test above would fail


def test_a_second_read_of_the_same_file_is_refused_when_names_are_tracked(tmp_path: Path) -> None:
    root = build(tmp_path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        seen: set[tuple[int, int]] = set()
        first = kb_manifest.read_regular_file(fd, "site-guide-v1.txt", 10_000, seen)
        assert first
        with pytest.raises(ManifestInvalid):
            kb_manifest.read_regular_file(fd, "site-guide-v1.txt", 10_000, seen)
        # without a tracker (the older call) nothing is remembered
        assert kb_manifest.read_regular_file(fd, "site-guide-v1.txt", 10_000)
    finally:
        os.close(fd)


def test_the_manifest_and_a_document_cannot_be_the_same_file(tmp_path: Path) -> None:
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0]["file"].update(path="manifest.json"))
    result = refused(root)
    assert result == "ManifestInvalid"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("            if identity in seen:", "            if False:"),
        ("            seen.add(identity)\n", ""),
    ],
    ids=["no-repeat-check", "never-recorded"],
)
def test_mutation_without_the_same_file_check_is_caught(old: str, new: str, tmp_path: Path) -> None:
    mutant = mutated_module(kb_manifest, (old, new))
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0]["file"].update(path="manifest.json"))
    real = refused(root)
    mutated = refused(root, mutant)
    assert real == "ManifestInvalid"
    assert mutated != "ManifestInvalid"  # the mutant reads it twice and fails on the hash instead


def test_mutation_not_sharing_the_tracker_between_reads_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        (
            "read_regular_file(root_fd, MANIFEST_FILENAME, budget, seen)",
            "read_regular_file(root_fd, MANIFEST_FILENAME, budget)",
        ),
    )
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0]["file"].update(path="manifest.json"))
    real = refused(root)
    mutated = refused(root, mutant)
    assert real == "ManifestInvalid"
    assert mutated != "ManifestInvalid"


@pytest.mark.parametrize("name", ["e\u0301.txt", "sub/e\u0301.txt", "\u212b.txt", "a\u0308.txt"])
def test_a_path_that_is_not_nfc_is_refused(name: str, tmp_path: Path) -> None:
    root = build(tmp_path)
    edit_manifest(root, lambda d: d["documents"][0]["file"].update(path=name))
    result = refused(root)
    assert result == "ManifestInvalid"


def _rename_document(root: Path, name: str) -> None:
    (root / "site-guide-v1.txt").rename(root / name)
    edit_manifest(root, lambda d: d["documents"][0]["file"].update(path=name))


def test_an_nfc_non_ascii_path_to_an_existing_file_is_accepted(tmp_path: Path) -> None:
    root = build(tmp_path)
    _rename_document(root, "\u00e9.txt")
    assert refused(root) is None


def test_mutation_without_the_nfc_path_check_is_caught(tmp_path: Path) -> None:
    mutant = mutated_module(
        kb_manifest,
        (
            '        if unicodedata.normalize("NFC", v) != v:\n'
            '            raise ValueError("bad path")\n',
            "",
        ),
    )
    root = build(tmp_path)
    _rename_document(root, "e\u0301.txt")  # the file exists under the decomposed name
    real = refused(root)
    mutated = refused(root, mutant)
    assert real == "ManifestInvalid"
    assert mutated is None


# --- every screened field (round 10): allow-list, id screen, accepted case, validator removal -----

DOCUMENT_FIELDS = (
    "title",
    "licence",
    "licence_reference",
    "attribution",
    "display_permission_reference",
)
SCREENED_FIELDS = (*DOCUMENT_FIELDS, "section", "chunk text")
TEXTS_LINE = (
    '        "title", "licence", "licence_reference", "attribution", '
    '"display_permission_reference"\n'
)
SECTION_VALIDATOR = '    _section = field_validator("section")(_field_text)\n'


def _set_field(root: Path, field: str, value: str) -> None:
    def change(d: dict[str, Any]) -> None:
        if field == "section":
            d["documents"][0]["file"]["chunks"][0]["section"] = value
        else:
            d["documents"][0][field] = value

    edit_manifest(root, change)


def _field_corpus(tmp_path: Path, field: str, value: str) -> Path:
    if field == "chunk text":
        return build(tmp_path, [doc_spec("risky-guide", chunks=(f"{value}\n",))])
    root = build(tmp_path)
    _set_field(root, field, value)
    return root


@pytest.mark.parametrize("field", SCREENED_FIELDS)
def test_every_screened_field_refuses_a_character_off_the_allow_list(
    tmp_path: Path, field: str
) -> None:
    outcome = refused(_field_corpus(tmp_path, field, "it\u2019s"))
    assert outcome == "ManifestInvalid"


@pytest.mark.parametrize("field", SCREENED_FIELDS)
def test_every_screened_field_refuses_an_evidence_id(tmp_path: Path, field: str) -> None:
    outcome = refused(_field_corpus(tmp_path, field, "kb:other-guide"))
    assert outcome == "ManifestInvalid"


@pytest.mark.parametrize("field", SCREENED_FIELDS)
def test_every_screened_field_accepts_ordinary_text(tmp_path: Path, field: str) -> None:
    outcome = refused(_field_corpus(tmp_path, field, "Caf\u00e9 (v2): 100% ok"))
    assert outcome is None


def test_a_lookalike_colon_in_a_chunk_is_refused_at_manifest_level(tmp_path: Path) -> None:
    root = build(tmp_path, [doc_spec("risky-guide", chunks=("kb\u2236abc\n",))])
    outcome = refused(root)
    assert outcome == "ManifestInvalid"


@pytest.mark.parametrize(
    ("field", "old"),
    [
        (
            "attribution",
            TEXTS_LINE.replace('"attribution", ', ""),
        ),
        ("licence_reference", TEXTS_LINE.replace('"licence_reference", ', "")),
        (
            "display_permission_reference",
            TEXTS_LINE.replace(', "display_permission_reference"', ""),
        ),
        ("title", TEXTS_LINE.replace('"title", ', "")),
        ("licence", TEXTS_LINE.replace('"licence", ', "", 1)),
        ("section", None),
    ],
)
@pytest.mark.parametrize("bad", ["it\u2019s", "kb:other-guide"])
def test_mutation_removing_a_field_validator_is_caught(
    tmp_path: Path, field: str, old: str | None, bad: str
) -> None:
    edit = (SECTION_VALIDATOR, "") if old is None else (TEXTS_LINE, old)
    mutant = mutated_module(kb_manifest, edit)
    real_root = _field_corpus(tmp_path / "real", field, bad)
    mutant_root = _field_corpus(tmp_path / "mut", field, bad)
    real = refused(real_root)
    mutated = refused(mutant_root, mutant)
    assert real == "ManifestInvalid"
    assert mutated is None  # the unvalidated field gets through: the test above would fail


def test_the_path_is_not_screened_by_the_allow_list_only_by_nfc_and_shape(
    tmp_path: Path,
) -> None:
    """PINNED CURRENT BEHAVIOUR: a path may hold ASCII punctuation outside the allow-list (`_`,
    `@`); `FileEntry._path` checks NFC and shape only. A non-NFC path is still refused. If the
    path is ever screened by the allow-list, revisit the first half of this test."""
    root = build(tmp_path / "a")
    _rename_document(root, "a_b@c.txt")
    allowed_by_path_rules = refused(root)
    assert allowed_by_path_rules is None
    assert not kb_screen_allowed("a_b@c.txt")
    other = build(tmp_path / "b")
    _rename_document(other, "e\u0301.txt")
    non_nfc = refused(other)
    assert non_nfc == "ManifestInvalid"


def kb_screen_allowed(value: str) -> bool:
    from divesafe.services import kb_screen

    return kb_screen.allowed_text(value, newlines=False)
