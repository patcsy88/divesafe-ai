"""Owner decision 2026-11-10 (reversing ADR 0022 condition 7 of 2026-11-06): the record refuses a
KNOWLEDGE item without a `kb:` id and a `kb:` id on any other kind, in `evidence` and in
`withheld_evidence`, so a stored record cannot carry knowledge the egress check would not see.
Failure conditions first. Every item below is SYNTHETIC test data.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import make_evidence
from tests.safety.test_record_egress import KB_ID, _doc, _knowledge

from divesafe.domain import AssessmentRecord, DataCategory, EvidenceItem
from divesafe.services import RepositoryIntegrityError
from divesafe.services.postgres import _storage_errors

pytestmark = pytest.mark.safety

LOCAL = {"egress_basis": "declared_local", "model_host": "127.0.0.1"}
NO_KB = {
    "evidence": "a knowledge evidence item must have a kb: id",
    "withheld": "a knowledge withheld item must have a kb: id",
}
KB_ON_OTHER = {
    "evidence": "a kb: id is reserved for knowledge evidence items",
    "withheld": "a kb: id is reserved for knowledge withheld items",
}
# Reserved ids in every case the prefix check folds; all follow the grammar except for case.
RESERVED = ["kb:site-guide:2:8", "KB:site-guide:2:8", "Kb:site-guide:2:8"]


def _live(evidence_id: str) -> EvidenceItem:
    return make_evidence(DataCategory.WAVES_SWELL).model_copy(update={"id": evidence_id})


def _refusal(doc: dict[str, Any]) -> str:
    try:
        AssessmentRecord.model_validate(doc)
    except ValidationError as exc:
        return str(exc)
    return ""


def _with_evidence(item: EvidenceItem) -> dict[str, Any]:
    doc = _doc(**LOCAL)
    doc["evidence"].append(item.model_dump(mode="json"))
    return doc


def _with_withheld(item: EvidenceItem) -> dict[str, Any]:
    """`item` withheld by an id conflict: two different items sharing its id (ADR 0008)."""
    other = item.model_copy(update={"source_version": "synthetic-other"})
    return _doc(
        **LOCAL,
        evidence_issues=["evidence.conflict: synthetic"],
        withheld_evidence=[item.model_dump(mode="json"), other.model_dump(mode="json")],
    )


PLACES = {"evidence": _with_evidence, "withheld": _with_withheld}


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize("where", PLACES)
@pytest.mark.parametrize("evidence_id", ["syn-site", "site-guide:2:7", "xkb:site-guide:2:7"])
def test_knowledge_without_a_kb_id_is_refused(where: str, evidence_id: str) -> None:
    assert NO_KB[where] in _refusal(PLACES[where](_knowledge(evidence_id)))


@pytest.mark.parametrize("where", PLACES)
@pytest.mark.parametrize("evidence_id", RESERVED)
def test_a_kb_id_on_a_non_knowledge_item_is_refused(where: str, evidence_id: str) -> None:
    assert KB_ON_OTHER[where] in _refusal(PLACES[where](_live(evidence_id)))


@pytest.mark.parametrize("where", PLACES)
def test_the_well_formed_cases_still_validate(where: str) -> None:
    """Positive controls: a KNOWLEDGE item with a kb: id, and a live item with an ordinary id."""
    for item in (_knowledge(KB_ID), _live("syn-wave-2")):
        assert _refusal(PLACES[where](item)) == ""


def test_the_messages_carry_no_id() -> None:
    for item, place in [(_knowledge("syn-secret-marker"), p) for p in PLACES.values()] + [
        (_live("kb:secretmarker:2:8"), p) for p in PLACES.values()
    ]:
        text = _refusal(place(item))
        message = text.splitlines()[1] if text else ""
        assert message and "secret" not in message.casefold(), message


def test_an_item_that_skipped_its_own_validation_is_still_refused_by_the_record() -> None:
    """`model_construct` items passed to the constructor (python mode) hit the same check."""
    base = AssessmentRecord.model_validate(_doc(**LOCAL))
    no_kb = EvidenceItem.model_construct(**{**dict(_knowledge()), "id": "syn-site"})
    kb_live = EvidenceItem.model_construct(**{**dict(_live("x")), "id": "KB:site-guide:2:8"})
    for item, message in ((no_kb, NO_KB["evidence"]), (kb_live, KB_ON_OTHER["evidence"])):
        with pytest.raises(ValidationError, match=message):
            AssessmentRecord(**{**dict(base), "evidence": (*base.evidence, item)})
    with pytest.raises(ValidationError, match=NO_KB["withheld"]):
        AssessmentRecord(
            **{
                **dict(base),
                "evidence_issues": ("evidence.conflict: synthetic",),
                "withheld_evidence": (
                    no_kb,
                    no_kb.model_copy(update={"source_version": "synthetic-other"}),
                ),
            }
        )


# --- a stored record ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("place", "item"),
    [
        ("evidence", _knowledge("syn-site")),
        ("evidence", _live("KB:site-guide:2:8")),
        ("withheld", _knowledge("syn-site")),
        ("withheld", _live("kb:site-guide:2:8")),
    ],
    ids=["evidence-no-kb", "evidence-kb-live", "withheld-no-kb", "withheld-kb-live"],
)
def test_a_stored_record_breaking_the_rule_raises_the_fixed_integrity_error(
    place: str, item: EvidenceItem
) -> None:
    """The Postgres repository's load path: `model_validate_json` inside `_storage_errors`.
    The error is the fixed text with no chain, so neither id nor content can reach a log. The
    same path against a real row: `tests/integration/test_postgres_guard.py`."""
    import json

    stored = json.dumps(PLACES[place](item))
    with pytest.raises(RepositoryIntegrityError) as caught, _storage_errors():
        AssessmentRecord.model_validate_json(stored)
    assert str(caught.value) == "a stored record failed validation"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
