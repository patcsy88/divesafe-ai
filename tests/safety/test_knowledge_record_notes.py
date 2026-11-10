"""ADR 0022 A1 and decision 14: the Observe/Retrieve trace split, the knowledge notes channel and
superseded versions on `AssessmentRecord`, how they reach the human, and that they never reach a
prompt. Failure conditions first. Every item below is SYNTHETIC test data.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.agent import scripted_llm as sl
from tests.conftest import NOW
from tests.safety.test_api_human_gate import _build, _create
from tests.safety.test_record_egress import _doc, _knowledge

from divesafe.agents import prompts as prompts_module
from divesafe.api.schemas import AssessmentView, knowledge_note
from divesafe.domain import (
    AssessmentRecord,
    DataCategory,
    DataKind,
    EvidenceItem,
    KnowledgeNote,
    KnowledgeNoteCode,
    KnowledgeVersionRef,
    StageName,
    StageOutcome,
    StageTrace,
)
from divesafe.domain.stages import MAX_EVIDENCE_ITEMS
from divesafe.models import FakeProvider

pytestmark = pytest.mark.safety

K = KnowledgeNoteCode
N = StageName
SRC = Path(__file__).resolve().parents[2] / "src" / "divesafe"
LOCAL = {"egress_basis": "declared_local", "model_host": "127.0.0.1"}
ABSENCE = "Absence of site documents is not evidence that the site has no hazards."


def _note(code: K, count: int = 0) -> dict[str, Any]:
    return KnowledgeNote(code=code, count=count).model_dump(mode="json")


def _version(document_id: str = "site-guide", version: int = 2) -> dict[str, Any]:
    return KnowledgeVersionRef(document_id=document_id, version=version).model_dump(mode="json")


def _refused(doc: dict[str, Any], message: str) -> bool:
    try:
        AssessmentRecord.model_validate(doc)
    except ValidationError as exc:
        return message in str(exc)
    return False


# --- notes and superseded versions on the record ------------------------------------------------


def test_a_note_code_appears_at_most_once() -> None:
    doc = _doc(notes=[_note(K.WITHDRAWN, 1), _note(K.WITHDRAWN, 2)])
    assert _refused(doc, "each knowledge note code appears at most once")
    AssessmentRecord.model_validate(_doc(notes=[_note(K.WITHDRAWN, 1), _note(K.EXPIRED, 2)]))


@pytest.mark.parametrize(
    "other",
    [_note(K.WITHDRAWN, 1), _note(K.NOT_IN_FORCE, 1), _note(K.EXPIRED, 1), _note(K.NONE_RETURNED)],
    ids=lambda n: n["code"],
)
def test_not_configured_stands_alone(other: dict[str, Any]) -> None:
    alone = _note(K.NOT_CONFIGURED)
    message = "knowledge_not_configured cannot appear with other knowledge notes"
    assert _refused(_doc(notes=[alone, other]), message)
    assert _refused(_doc(notes=[other, alone]), message)
    AssessmentRecord.model_validate(_doc(notes=[alone]))
    AssessmentRecord.model_validate(_doc(notes=[other]))


def test_not_configured_cannot_list_superseded_versions() -> None:
    doc = _doc(notes=[_note(K.NOT_CONFIGURED)], superseded_knowledge=[_version()])
    assert _refused(doc, "knowledge_not_configured cannot appear with other knowledge notes")
    AssessmentRecord.model_validate(_doc(superseded_knowledge=[_version()]))


@pytest.mark.parametrize("code", [K.NOT_CONFIGURED, K.NONE_RETURNED])
def test_a_no_knowledge_note_contradicts_kb_evidence(code: K) -> None:
    message = "a note saying no knowledge was returned contradicts kb: evidence"
    assert _refused(_doc(_knowledge(), notes=[_note(code)], **LOCAL), message)
    AssessmentRecord.model_validate(_doc(_knowledge(), **LOCAL))
    # Knowledge without a kb: id is not knowledge for this check (the owner's rule, 2026-11-06).
    AssessmentRecord.model_validate(_doc(_knowledge("syn-site"), notes=[_note(code)]))


@pytest.mark.parametrize("code", [K.NOT_CONFIGURED, K.NONE_RETURNED])
def test_a_no_knowledge_note_contradicts_withheld_kb_evidence(code: K) -> None:
    """kb: items dropped by an id conflict (`withheld_evidence`, ADR 0008) were still returned."""
    first, second = _knowledge(), _knowledge().model_copy(update={"source_version": "other"})
    base = _doc(
        evidence_issues=["evidence.conflict: synthetic"],
        withheld_evidence=[first.model_dump(mode="json"), second.model_dump(mode="json")],
    )
    AssessmentRecord.model_validate(base)
    message = "a note saying no knowledge was returned contradicts kb: evidence"
    assert _refused({**base, "notes": [_note(code)]}, message)


@pytest.mark.parametrize("code", [K.WITHDRAWN, K.NOT_IN_FORCE, K.EXPIRED])
def test_family_notes_may_sit_beside_kb_evidence(code: K) -> None:
    AssessmentRecord.model_validate(_doc(_knowledge(), notes=[_note(code, 3)], **LOCAL))


def test_a_superseded_version_cannot_be_in_the_evidence() -> None:
    message = "a superseded knowledge version cannot be in the evidence"
    used = _doc(_knowledge(), superseded_knowledge=[_version("site-guide", 2)], **LOCAL)
    assert _refused(used, message)
    # Another version of the same document, or another document, is fine.
    AssessmentRecord.model_validate(
        _doc(_knowledge(), superseded_knowledge=[_version("site-guide", 1)], **LOCAL)
    )
    AssessmentRecord.model_validate(
        _doc(_knowledge(), superseded_knowledge=[_version("other-guide", 2)], **LOCAL)
    )


def test_a_superseded_version_appears_at_most_once() -> None:
    doc = _doc(superseded_knowledge=[_version(), _version()])
    assert _refused(doc, "each superseded knowledge version appears at most once")


@pytest.mark.parametrize(
    ("code", "count"),
    [(K.WITHDRAWN, 0), (K.NOT_IN_FORCE, 0), (K.EXPIRED, 0), (K.NOT_CONFIGURED, 1)]
    + [(K.NONE_RETURNED, 1), (K.WITHDRAWN, -1), (K.WITHDRAWN, 1_000_000)],
)
def test_a_note_count_must_match_its_code(code: K, count: int) -> None:
    with pytest.raises(ValidationError):
        KnowledgeNote(code=code, count=count)


@pytest.mark.parametrize(
    "bad",
    [
        {"code": "knowledge_withdrawn", "count": 1, "text": "Site guide v2 withdrawn"},
        {"code": "Withdrawn because the licence ended", "count": 1},
        {"code": "knowledge_withdrawn", "count": "1"},
        {"document_id": "Site Guide", "version": 1},
        {"document_id": "site-guide", "version": 0},
        {"document_id": "site-guide", "version": 1, "title": "Site guide"},
    ],
)
def test_notes_and_versions_carry_no_free_text(bad: dict[str, Any]) -> None:
    key = "superseded_knowledge" if "document_id" in bad else "notes"
    with pytest.raises(ValidationError):
        AssessmentRecord.model_validate(_doc(**{key: [bad]}))


def test_older_records_without_the_new_fields_still_validate() -> None:
    """ADR 0015: a record stored before `notes` and `superseded_knowledge` existed."""
    for doc in (_doc(), sl.run_pipeline(sl.fake()).model_dump(mode="json")):
        for key in ("notes", "superseded_knowledge"):
            doc.pop(key)
        record = AssessmentRecord.model_validate(doc)
        assert (record.notes, record.superseded_knowledge) == ((), ())
        assert knowledge_note(record) == ""
        assert AssessmentView.of(record).knowledge_note == ""


def test_an_older_record_with_the_placeholder_retrieve_code_still_validates() -> None:
    doc = sl.run_pipeline(sl.fake()).model_dump(mode="json")
    retrieve = next(t for t in doc["stages"] if t["name"] == "retrieve")
    assert retrieve["issues"] == ["retrieve.not_configured"]
    doc.pop("notes")
    AssessmentRecord.model_validate(doc)


@pytest.mark.parametrize(
    "kind",
    ["not_configured", "knowledge_not_configured", "knowledge_failed"]
    + ["knowledge_timed_out", "knowledge_issue"],
)
def test_the_retrieve_codes_are_fixed(kind: str) -> None:
    StageTrace(
        name=N.RETRIEVE,
        outcome=StageOutcome.COMPLETED,
        next_stage=None,
        started_at=NOW,
        finished_at=NOW,
        issues=(f"retrieve.{kind}",),
    )
    with pytest.raises(ValidationError):
        StageTrace(
            name=N.RETRIEVE,
            outcome=StageOutcome.COMPLETED,
            next_stage=None,
            started_at=NOW,
            finished_at=NOW,
            issues=(f"retrieve.{kind}_extra",),
        )


# --- the Observe / Retrieve split -----------------------------------------------------------------


def _live(n: int) -> EvidenceItem:
    return EvidenceItem(
        id=f"syn-live-{n:03d}",
        category=DataCategory.WIND,
        source="synthetic",
        retrieved_at=NOW,
        valid_at=NOW,
        valid_until=NOW + timedelta(hours=3),
        is_forecast=True,
        data_kind=DataKind.FORECAST,
        value={},
    )


KB_IDS = ("kb:site-guide:2:7", "kb:site-guide:2:8")


def _split_doc(live: int, *, observe: Any = None, retrieve: Any = "kb") -> dict[str, Any]:
    """A record with `kb:` items FIRST then `live` live items, and an Observe -> Retrieve trace.
    `retrieve=None` leaves the Retrieve entry out."""
    live_ids = tuple(_live(n).id for n in range(live))
    doc = _doc(*(_knowledge(i) for i in KB_IDS), **LOCAL)
    wave = doc["evidence"][0]
    doc["evidence"] = [
        *doc["evidence"][1:],
        wave,
        *(_live(n).model_dump(mode="json") for n in range(live)),
    ]
    all_live = (wave["id"], *live_ids)
    names = [N.OBSERVE] + ([] if retrieve is None else [N.RETRIEVE])
    ids = {
        N.OBSERVE: all_live[:MAX_EVIDENCE_ITEMS] if observe is None else observe,
        N.RETRIEVE: KB_IDS if retrieve == "kb" else retrieve,
    }
    stages = [
        StageTrace(
            name=name,
            outcome=StageOutcome.COMPLETED,
            next_stage=names[i + 1] if i + 1 < len(names) else None,
            started_at=NOW,
            finished_at=NOW,
            evidence_ids=tuple(ids[name]),
        ).model_dump(mode="json")
        for i, name in enumerate(names)
    ]
    return {**doc, "stages": stages}


def test_observe_lists_the_live_ids_and_retrieve_the_kb_ids() -> None:
    record = AssessmentRecord.model_validate(_split_doc(3))
    observe, retrieve = record.stages
    assert not any(i.startswith("kb:") for i in observe.evidence_ids)
    assert retrieve.evidence_ids == KB_IDS


def test_observe_may_not_list_kb_ids() -> None:
    doc = _split_doc(3)
    live = tuple(doc["stages"][0]["evidence_ids"])
    message = "the observe trace must list the record's kept evidence ids"
    assert _refused(_split_doc(3, observe=(*KB_IDS, *live)), message)
    assert _refused(_split_doc(3, observe=live[:-1]), message)


def test_observe_is_cut_at_the_bound_counting_live_ids_only() -> None:
    """With 2 kb: ids first and 125 live ids, Observe lists the first 120 LIVE ids. A cut over all
    kept ids would drop two live ids and fail here."""
    live = MAX_EVIDENCE_ITEMS + 5
    doc = _split_doc(live)
    record = AssessmentRecord.model_validate(doc)
    assert len(record.stages[0].evidence_ids) == MAX_EVIDENCE_ITEMS
    assert record.stages[0].evidence_ids[-1] == _live(MAX_EVIDENCE_ITEMS - 2).id
    all_ids = tuple(e["id"] for e in doc["evidence"])
    message = "the observe trace must list the record's kept evidence ids"
    assert _refused(_split_doc(live, observe=all_ids[:MAX_EVIDENCE_ITEMS]), message)
    # The last 120 live ids (a cut from the wrong end) are refused too.
    live_ids = all_ids[len(KB_IDS) :]
    assert _refused(_split_doc(live, observe=live_ids[-MAX_EVIDENCE_ITEMS:]), message)


@pytest.mark.parametrize(
    "ids",
    [(), KB_IDS[:1], KB_IDS[::-1], (*KB_IDS, "syn-live-000"), ("syn-live-000",)],
    ids=["empty", "subset", "reordered", "plus-live", "live-only"],
)
def test_retrieve_must_list_exactly_the_kb_ids(ids: tuple[str, ...]) -> None:
    message = "the retrieve trace must list exactly the record's kb: ids"
    assert _refused(_split_doc(3, retrieve=ids), message)


def test_kb_evidence_with_a_trace_but_no_retrieve_entry_is_refused() -> None:
    assert _refused(_split_doc(3, retrieve=None), "kb: evidence needs a retrieve trace")
    # No trace at all (an older record) and no kb: evidence are both fine.
    AssessmentRecord.model_validate({**_split_doc(3), "stages": []})
    no_kb = _split_doc(3, retrieve=())
    no_kb["evidence"] = [e for e in no_kb["evidence"] if not e["id"].startswith("kb:")]
    AssessmentRecord.model_validate(no_kb)


def test_a_record_without_kb_evidence_has_an_empty_retrieve_trace() -> None:
    record = sl.run_pipeline(sl.fake())
    retrieve = next(t for t in record.stages if t.name is N.RETRIEVE)
    assert retrieve.evidence_ids == ()
    doc = record.model_dump(mode="json")
    doc["stages"] = [
        {**t, "evidence_ids": [record.evidence[0].id]} if t["name"] == "retrieve" else t
        for t in doc["stages"]
    ]
    assert _refused(doc, "the retrieve trace must list exactly the record's kb: ids")


# --- notes reach the human, in fixed wording ------------------------------------------------------


def test_the_pipeline_records_not_configured_and_the_view_says_so_with_the_absence_line() -> None:
    record = sl.run_pipeline(sl.fake())
    assert record.notes == (KnowledgeNote(code=K.NOT_CONFIGURED),)
    note = AssessmentView.of(record).knowledge_note
    assert note.startswith("No curated site knowledge source is configured")
    assert ABSENCE in note
    assert "knowledge_not_configured" not in note  # wording, not the code


def test_the_note_reaches_the_user_over_http() -> None:
    client, _, _ = _build()
    body = _create(client)
    assert body["record"]["notes"] == [{"code": "knowledge_not_configured", "count": 0}]
    assert "No curated site knowledge source is configured" in body["knowledge_note"]
    assert ABSENCE in body["knowledge_note"]


@pytest.mark.parametrize(
    ("note", "wording"),
    [
        (KnowledgeNote(code=K.WITHDRAWN, count=2), "2 site document(s) were withdrawn"),
        (KnowledgeNote(code=K.NOT_IN_FORCE, count=1), "1 site document(s) were not yet in force"),
        (KnowledgeNote(code=K.EXPIRED, count=3), "3 site document(s) were past their review"),
        (KnowledgeNote(code=K.NONE_RETURNED), "No site documents were found for this dive."),
    ],
    ids=lambda v: v.code.value if isinstance(v, KnowledgeNote) else "",
)
def test_every_note_code_has_fixed_wording_and_the_absence_line(
    note: KnowledgeNote, wording: str
) -> None:
    record = AssessmentRecord.model_validate(_doc(notes=[note.model_dump(mode="json")]))
    text = AssessmentView.of(record).knowledge_note
    assert wording in text
    assert text.endswith(ABSENCE + " Site knowledge is context only and never live conditions.")
    assert note.code.value not in text


def test_superseded_versions_are_shown_by_id_only() -> None:
    record = AssessmentRecord.model_validate(
        _doc(superseded_knowledge=[_version("site-guide", 1), _version("reef-notes", 4)])
    )
    text = AssessmentView.of(record).knowledge_note
    assert "Superseded by a newer version and not used: site-guide v1, reef-notes v4." in text


# --- notes never reach a prompt -------------------------------------------------------------------


def _note_strings() -> list[str]:
    from divesafe.api.schemas import _KNOWLEDGE_NOTE_WORDING

    fixed = [w.split("{count}")[-1].strip() for w in _KNOWLEDGE_NOTE_WORDING.values()]
    return [*(c.value for c in K), *(w for w in fixed if w), ABSENCE.split(".")[0]]


def _prompt_texts(provider: FakeProvider) -> list[str]:
    return [m.content for r in provider.requests for m in r.messages]


def _leaks(texts: list[str]) -> list[str]:
    return [m for t in texts for m in _note_strings() if m in t]


def test_no_note_code_or_wording_appears_in_any_prompt() -> None:
    provider = FakeProvider(sl.scripted())
    record = sl.run_pipeline(provider)
    assert record.notes  # the run did record a note
    texts = _prompt_texts(provider)
    assert len(texts) > 1
    assert _leaks(texts) == []


def test_the_prompt_check_has_teeth() -> None:
    """Positive control: every code and every fixed wording is caught when planted."""
    for marker in _note_strings():
        assert _leaks([f"synthetic prompt {marker} text"]) != [], marker


def test_no_note_code_or_wording_appears_in_the_prompt_layer_source() -> None:
    """Static scan of `agents/` (where every prompt is built): no reference to the notes channel
    and none of its codes or wording."""
    forbidden = [
        *_note_strings(),
        "KnowledgeNote",
        "superseded_knowledge",
        "knowledge_note",
        "NOT_CONFIGURED",
        "NONE_RETURNED",
    ]
    files = sorted((SRC / "agents").rglob("*.py"))
    assert Path(prompts_module.__file__).resolve() in {f.resolve() for f in files}
    for path in files:
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{path.name}: {marker}"
        assert not re.search(r"\.notes\b", source), path.name
