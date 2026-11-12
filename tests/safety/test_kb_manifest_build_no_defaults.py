"""The manifest builder never supplies or changes a human field (security review, 2026-11-12). The
loader re-checks every value in the built manifest, but it cannot tell whether `attests_*: true`,
`display_permitted` or a licence field was written by a human or defaulted by the builder, so the
builder itself must refuse a draft that lacks any of them, copy each one verbatim, and never
overwrite a refusable value with a permissive one. The key sets are pinned here as literals and
the helpers live in `tests/safety/kb_build_draft.py`, so both are under safety review.
Every text here is synthetic. Mutations run on COPIES of the module."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from tests.safety.kb_build_draft import (
    CRASHED,
    GUIDE,
    corpus,
    document,
    draft,
    manifest_of,
    mutated_module,
    outcome,
)

from divesafe.services import kb_manifest, kb_manifest_build
from divesafe.services.kb_manifest_build import (
    DOCUMENT_KEYS,
    FAILED_DRAFT,
    FAILED_LOADER,
    OK_BUILD,
    TOP_KEYS,
)

pytestmark = pytest.mark.safety

# Pinned here, not read from the module: a field dropped from the module's lists would otherwise
# silently drop out of every test below.
EXPECTED_TOP_KEYS = (
    "format",
    "attests_no_generated_text",
    "attests_no_personal_names",
    "documents",
)
EXPECTED_DOCUMENT_KEYS = (
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
TOP_CHECK = "    if not isinstance(draft, dict) or set(draft) != set(TOP_KEYS):\n"
DOC_CHECK = "        if not isinstance(doc, dict) or set(doc) != {*DOCUMENT_KEYS, PATH_KEY}:\n"
PARSED = "        draft = parse_draft(raw_draft)\n"


# --- shared assertions: one place, so loosening one loosens every test that uses it visibly ----


def assert_refused_as_draft(module: Any, path: Path) -> None:
    assert outcome(module, path) == FAILED_DRAFT


def assert_refused_by_the_loader(module: Any, path: Path) -> None:
    assert outcome(module, path) == FAILED_LOADER


def assert_builds(module: Any, path: Path) -> None:
    """A mutant prints a manifest: exactly what the real module must not do for this draft."""
    assert outcome(module, path) == OK_BUILD


def without(key: str) -> dict[str, Any]:
    """A valid draft with one top-level or document field removed."""
    if key in EXPECTED_TOP_KEYS:
        the_draft = draft()
        del the_draft[key]
        return the_draft
    doc = document()
    del doc[key]
    return draft(doc)


def valid_value(key: str) -> Any:
    """The value the valid draft carries. For the single-valued booleans (attests_* true,
    display_permitted true, licence_requires_personal_name false) and the scope pair (all_sites
    true with site_id null) it is the permissive one; for the rest (origin, author_role,
    supersedes, the dates, the texts) it is just a value the loader accepts. A builder forcing
    one of those is caught by the second verbatim case below, not by the default mutants."""
    return draft()[key] if key in EXPECTED_TOP_KEYS else document()[key]


# --- the pinned key sets ------------------------------------------------------------------------


def test_the_module_key_sets_are_the_pinned_ones() -> None:
    assert TOP_KEYS == EXPECTED_TOP_KEYS
    assert DOCUMENT_KEYS == EXPECTED_DOCUMENT_KEYS
    assert kb_manifest_build.PATH_KEY == PATH_KEY


# --- a missing field is refused, never filled in --------------------------------------------------


# The valid draft holds the permissive or more trusted value of every multi-valued field, so a
# builder FORCING that value would still match it. The second draft holds the other accepted
# value of each, so such a builder is caught there.
OTHER_VALUES: dict[str, Any] = {
    "document_id": "other-guide",
    "version": 2,
    "title": "Another synthetic guide",
    "origin": "published_document",
    "author_role": "published_source",
    "effective_date": "2026-03-15",
    "review_by_date": "2027-03-01",
    "licence": "Another synthetic licence",
    "licence_reference": "synthetic-licence-ref-2",
    "attribution": "Synthetic attribution",
    "site_id": next(iter(kb_manifest.SITES)),
    "all_sites": False,
    "display_permission_reference": "synthetic-permission-ref-2",
    "supersedes": 1,
}
VERBATIM = {"permissive": draft(), "other-values": draft(document(**OTHER_VALUES))}


def differing(raw: bytes, the_draft: dict[str, Any]) -> list[str]:
    """The human fields the built manifest does not carry verbatim, and any shape difference.
    Empty means verbatim; the real module and every mutant are judged by this one function."""
    built = manifest_of(raw)
    if list(built) != list(EXPECTED_TOP_KEYS) or len(built["documents"]) != 1:
        return ["<shape>"]
    out = [k for k in EXPECTED_TOP_KEYS[:-1] if built[k] != the_draft[k]]
    (entry,) = built["documents"]
    if list(entry) != [*EXPECTED_DOCUMENT_KEYS, "file"]:
        return [*out, "<document shape>"]
    source = the_draft["documents"][0]
    out += [k for k in EXPECTED_DOCUMENT_KEYS if entry[k] != source[k]]
    if set(entry["file"]) != {"path", "sha256", "chunks", "gaps"}:
        out.append("<file shape>")
    elif entry["file"]["sha256"] != hashlib.sha256(GUIDE.encode()).hexdigest():
        out.append("<file hash>")
    return out


@pytest.mark.parametrize("case", VERBATIM)
def test_human_fields_are_copied_verbatim_and_nothing_is_added(tmp_path: Path, case: str) -> None:
    the_draft = VERBATIM[case]
    raw = kb_manifest_build.build(corpus(tmp_path, {"guide.txt": GUIDE}, the_draft))
    assert differing(raw, the_draft) == []


def test_the_other_values_draft_differs_from_the_permissive_one_in_every_listed_field() -> None:
    """Guards the second case itself: each listed value really is the other one."""
    base = document()
    assert all(base[k] != v for k, v in OTHER_VALUES.items())


# Each mutant forces the permissive or more trusted value after parsing; the loader accepts it.
FORCED_TRUST: dict[str, dict[str, Any]] = {
    "origin": {"origin": "dive_professional"},
    "author_role": {"author_role": "dive_professional"},
    "supersedes": {"supersedes": None},
    "scope-widened": {"all_sites": True, "site_id": None},
    "attribution": {"attribution": None},
    "review_by_date": {"review_by_date": "2027-03-15"},
}


@pytest.mark.parametrize("case", FORCED_TRUST)
def test_mutation_a_forced_trusted_value_is_caught_by_the_verbatim_check(
    tmp_path: Path, case: str
) -> None:
    fields = FORCED_TRUST[case]
    the_draft = VERBATIM["other-values"]
    path = corpus(tmp_path, {"guide.txt": GUIDE}, the_draft)
    mutant = mutated_module(kb_manifest_build, _forced(**fields))
    assert outcome(mutant, path) == OK_BUILD  # the loader accepts the forced value
    assert set(differing(mutant.build(path), the_draft)) == set(fields), case
    # Control: on the permissive draft the same mutant is invisible (review_by_date excepted: the
    # forced date is out of range there), which is why the second case exists.
    if case != "review_by_date":
        permissive = VERBATIM["permissive"]
        plain = corpus(tmp_path / "permissive", {"guide.txt": GUIDE}, permissive)
        assert differing(mutant.build(plain), permissive) == []


@pytest.mark.parametrize("key", EXPECTED_TOP_KEYS)
def test_a_missing_top_level_field_is_refused(tmp_path: Path, key: str) -> None:
    assert_refused_as_draft(kb_manifest_build, corpus(tmp_path, {"guide.txt": GUIDE}, without(key)))


@pytest.mark.parametrize("key", [*EXPECTED_DOCUMENT_KEYS, PATH_KEY])
def test_a_missing_document_field_is_refused(tmp_path: Path, key: str) -> None:
    """Never defaulted: `null` fields too (attribution, site_id, supersedes) must be written."""
    assert_refused_as_draft(kb_manifest_build, corpus(tmp_path, {"guide.txt": GUIDE}, without(key)))


# --- a refusable value is passed to the loader's parser, never overwritten ------------------------

# (top-level fields, document fields, the field a mutant forces, the value it forces)
REFUSABLE = {
    "attests_no_generated_text": (
        {"attests_no_generated_text": False},
        {},
        "attests_no_generated_text",
        True,
    ),
    "attests_no_personal_names": (
        {"attests_no_personal_names": False},
        {},
        "attests_no_personal_names",
        True,
    ),
    "display_permitted": ({}, {"display_permitted": False}, "display_permitted", True),
    "licence_requires_personal_name": (
        {},
        {"licence_requires_personal_name": True},
        "licence_requires_personal_name",
        False,
    ),
    "all_sites": ({}, {"site_id": None, "all_sites": False}, "all_sites", True),
    "attests_no_generated_text-null": (
        {"attests_no_generated_text": None},
        {},
        "attests_no_generated_text",
        True,
    ),
}


def _refusable_path(tmp_path: Path, case: str) -> Path:
    top, doc, _, _ = REFUSABLE[case]
    # The file starts with a heading, so the title is not used as a section here.
    return corpus(tmp_path, {"guide.txt": "## Start\nBody.\n"}, draft(document(**doc), **top))


def _forced(**fields: Any) -> tuple[str, str]:
    """A builder that overwrites each field with its value after parsing, whatever the draft
    says."""
    lines = ""
    for key, value in fields.items():
        if key in EXPECTED_TOP_KEYS:
            lines += f"        draft[{key!r}] = {value!r}\n"
        else:
            lines += (
                f"        for _doc in draft['documents']:\n            _doc[{key!r}] = {value!r}\n"
            )
    return (PARSED, PARSED + lines)


@pytest.mark.parametrize("case", REFUSABLE)
def test_a_refusable_value_is_judged_by_the_loader_never_corrected(
    tmp_path: Path, case: str
) -> None:
    assert_refused_by_the_loader(kb_manifest_build, _refusable_path(tmp_path, case))


@pytest.mark.parametrize("case", REFUSABLE)
def test_mutation_an_overwritten_value_is_caught(tmp_path: Path, case: str) -> None:
    path = _refusable_path(tmp_path, case)
    _, _, key, value = REFUSABLE[case]
    assert_refused_by_the_loader(kb_manifest_build, path)
    assert_builds(mutated_module(kb_manifest_build, _forced(**{key: value})), path)


# --- mutations: a default for a missing field (on COPIES) --------------------------------------


def _defaulted(key: str) -> tuple[str, str]:
    """A builder that fills in a missing field, with the valid draft's value, before the key-set
    check."""
    value = valid_value(key)
    if key in EXPECTED_TOP_KEYS:
        guard = f"    if isinstance(draft, dict):\n        draft.setdefault({key!r}, {value!r})\n"
        return (TOP_CHECK, guard + TOP_CHECK)
    guard = f"        if isinstance(doc, dict):\n            doc.setdefault({key!r}, {value!r})\n"
    return (DOC_CHECK, guard + DOC_CHECK)


@pytest.mark.parametrize("key", [*EXPECTED_TOP_KEYS, *EXPECTED_DOCUMENT_KEYS, PATH_KEY])
def test_mutation_a_defaulted_field_is_built_and_caught(tmp_path: Path, key: str) -> None:
    """Every human field, including attests_*, display_permitted, the licence fields, site_id and
    all_sites, review_by_date, origin and author_role, and supersedes."""
    # `documents` defaulted to a list fabricates a whole document: that case tests the key-set
    # check itself, not a default for a human field.
    path = corpus(tmp_path, {"guide.txt": GUIDE}, without(key))
    assert_refused_as_draft(kb_manifest_build, path)
    assert_builds(mutated_module(kb_manifest_build, _defaulted(key)), path)


@pytest.mark.parametrize(
    ("edit", "missing"),
    [
        (
            (
                "set(doc) != {*DOCUMENT_KEYS, PATH_KEY}",
                "not set(doc) <= {*DOCUMENT_KEYS, PATH_KEY}",
            ),
            "attribution",
        ),
        (
            ("set(draft) != set(TOP_KEYS)", "not set(draft) <= set(TOP_KEYS)"),
            "attests_no_generated_text",
        ),
    ],
    ids=["document-subset", "top-level-subset"],
)
def test_mutation_the_exact_key_set_is_load_bearing(
    tmp_path: Path, edit: tuple[str, str], missing: str
) -> None:
    """A subset check lets the missing key through to a later `draft[key]` (a crash, not a
    refusal): the exact key-set comparison is what refuses it."""
    path = corpus(tmp_path, {"guide.txt": GUIDE}, without(missing))
    assert_refused_as_draft(kb_manifest_build, path)
    assert outcome(mutated_module(kb_manifest_build, edit), path) == CRASHED
