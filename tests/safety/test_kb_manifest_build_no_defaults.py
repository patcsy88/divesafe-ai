"""The manifest builder never supplies a human field (security review, 2026-11-12). The loader
re-checks every value in the built manifest, but it cannot tell whether `attests_*: true`,
`display_permitted` or a licence field was written by a human or defaulted by the builder, so the
builder itself must refuse a draft that lacks any of them and copy each one verbatim.
Every text here is synthetic. Mutations run on COPIES of the module."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from tests.safety.kb_fakes import mutated_module
from tests.unit.test_kb_manifest_build import GUIDE, corpus, document, draft, manifest_of, outcome

from divesafe.services import kb_manifest_build
from divesafe.services.kb_manifest_build import DOCUMENT_KEYS, FAILED_DRAFT, OK_BUILD, TOP_KEYS

pytestmark = pytest.mark.safety

TOP_CHECK = "    if not isinstance(draft, dict) or set(draft) != set(TOP_KEYS):\n"
DOC_CHECK = "        if not isinstance(doc, dict) or set(doc) != {*DOCUMENT_KEYS, PATH_KEY}:\n"


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


# --- mutations (on COPIES) -----------------------------------------------------------------------


def _top_default(key: str, value: Any) -> tuple[str, str]:
    """A builder that fills in a missing top-level field before the key-set check."""
    return (
        TOP_CHECK,
        f"    if isinstance(draft, dict):\n        draft.setdefault({key!r}, {value!r})\n"
        + TOP_CHECK,
    )


def _doc_default(key: str, value: Any) -> tuple[str, str]:
    """A builder that fills in a missing document field before the key-set check."""
    return (
        DOC_CHECK,
        f"        if isinstance(doc, dict):\n            doc.setdefault({key!r}, {value!r})\n"
        + DOC_CHECK,
    )


# Each default is the value a careless builder would choose: the permissive one.
TOP_DEFAULTS = {"attests_no_generated_text": True, "attests_no_personal_names": True}
DOC_DEFAULTS = {
    "attribution": None,
    "display_permitted": True,
    "display_permission_reference": "synthetic-default-ref",
    "licence": "Synthetic default licence",
    "licence_reference": "synthetic-default-licence-ref",
    "licence_requires_personal_name": False,
}


@pytest.mark.parametrize(("key", "value"), TOP_DEFAULTS.items(), ids=list(TOP_DEFAULTS))
def test_mutation_a_defaulted_attestation_is_caught(tmp_path: Path, key: str, value: Any) -> None:
    the_draft = draft()
    del the_draft[key]
    path = corpus(tmp_path, {"guide.txt": GUIDE}, the_draft)
    assert outcome(kb_manifest_build, path) == FAILED_DRAFT
    # The mutant prints a manifest the human never attested: exactly what this file prevents.
    assert outcome(mutated_module(kb_manifest_build, _top_default(key, value)), path) == OK_BUILD


@pytest.mark.parametrize(("key", "value"), DOC_DEFAULTS.items(), ids=list(DOC_DEFAULTS))
def test_mutation_a_defaulted_document_field_is_caught(
    tmp_path: Path, key: str, value: Any
) -> None:
    doc = document()
    del doc[key]
    path = corpus(tmp_path, {"guide.txt": GUIDE}, draft(doc))
    assert outcome(kb_manifest_build, path) == FAILED_DRAFT
    assert outcome(mutated_module(kb_manifest_build, _doc_default(key, value)), path) == OK_BUILD


@pytest.mark.parametrize(
    ("label", "edit", "missing"),
    [
        (
            "exact key set becomes subset",
            (
                "set(doc) != {*DOCUMENT_KEYS, PATH_KEY}",
                "not set(doc) <= {*DOCUMENT_KEYS, PATH_KEY}",
            ),
            "attribution",
        ),
        (
            "top-level exact key set becomes subset",
            ("set(draft) != set(TOP_KEYS)", "not set(draft) <= set(TOP_KEYS)"),
            "attests_no_generated_text",
        ),
    ],
    ids=["document-subset", "top-level-subset"],
)
def test_mutation_a_defaulted_field_is_caught(
    tmp_path: Path, label: str, edit: tuple[str, str], missing: str
) -> None:
    if missing in TOP_KEYS:
        the_draft = draft()
        del the_draft[missing]
    else:
        doc = document()
        del doc[missing]
        the_draft = draft(doc)
    path = corpus(tmp_path, {"guide.txt": GUIDE}, the_draft)
    assert outcome(kb_manifest_build, path) == FAILED_DRAFT
    # A subset check lets the missing key through to a later `draft[key]`: no longer refused.
    assert outcome(mutated_module(kb_manifest_build, edit), path) != FAILED_DRAFT, label
