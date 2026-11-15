"""ADR 0028 decision 6: the view says site document extracts went to the external model server
only when the basis is `remote_knowledge_opt_in`, unwithheld `kb:` evidence is on the record and
the Reason stage ran. The withheld sentence is reworded. No host is ever shown in either. Wording is
literal here. Mutations edit COPIES of `api.schemas`, never `src`."""

from __future__ import annotations

from typing import Any

import pytest
from tests.agent import scripted_llm as sl
from tests.agent.test_knowledge_egress import _run
from tests.safety.kb_fakes import mutated_module

from divesafe.api import schemas as schemas_module
from divesafe.api.schemas import AssessmentView, knowledge_note
from divesafe.domain import AssessmentRecord, RecordEgressBasis, StageName, StageOutcome

pytestmark = pytest.mark.safety

SENT = "Site document extracts were included in prompts to the external model server."
WITHHELD_TAIL = (
    "site document extract(s) were not sent to the language model because the model server is "
    "not declared local or named for site documents."
)
HOST = "llm.example.org"


@pytest.fixture(scope="module")
def sent_record() -> AssessmentRecord:
    record = _run(sl.fake(), "remote_knowledge_opt_in", HOST)
    assert record.withheld_knowledge == ()
    assert any(e.id.startswith("kb:") for e in record.evidence)
    return record


def _reason_as(record: AssessmentRecord, outcome: StageOutcome) -> AssessmentRecord:
    stages = tuple(
        t.model_copy(update={"outcome": outcome}) if t.name is StageName.REASON else t
        for t in record.stages
    )
    return record.model_copy(update={"stages": stages})


def _note(record: AssessmentRecord, module: Any = schemas_module) -> str:
    return str(module.knowledge_note(record))


def test_the_sentence_is_shown_when_all_three_conditions_hold(
    sent_record: AssessmentRecord,
) -> None:
    note = AssessmentView.of(sent_record).knowledge_note
    assert SENT in note
    assert HOST not in note and "example" not in note
    assert note.count(SENT) == 1


@pytest.mark.parametrize(
    "basis",
    [b for b in RecordEgressBasis if b.value != "remote_knowledge_opt_in"],
    ids=lambda b: b.value,
)
def test_not_under_another_basis(sent_record: AssessmentRecord, basis: RecordEgressBasis) -> None:
    record = sent_record.model_copy(update={"egress_basis": basis})
    assert SENT not in _note(record)


def test_not_when_the_basis_is_a_plain_string_or_absent(sent_record: AssessmentRecord) -> None:
    """Only the enum member counts; a smuggled near miss or an older record never shows it."""
    for basis in (None, "REMOTE_KNOWLEDGE_OPT_IN", "remote_knowledge_opt_in "):
        record = sent_record.model_copy(update={"egress_basis": basis})
        assert SENT not in _note(record), basis


def test_not_without_kb_evidence(sent_record: AssessmentRecord) -> None:
    evidence = tuple(e for e in sent_record.evidence if not e.id.startswith("kb:"))
    record = sent_record.model_copy(update={"evidence": evidence})
    assert SENT not in _note(record)


def test_not_when_every_kb_item_is_withheld(sent_record: AssessmentRecord) -> None:
    from divesafe.domain.models import knowledge_ref

    refs = tuple(ref for e in sent_record.evidence if (ref := knowledge_ref(e.id)) is not None)
    record = sent_record.model_copy(update={"withheld_knowledge": refs})
    assert SENT not in _note(record)
    # One unwithheld item among withheld ones is enough.
    partly = sent_record.model_copy(update={"withheld_knowledge": refs[:-1]})
    assert SENT in _note(partly)


@pytest.mark.parametrize("outcome", [StageOutcome.SKIPPED, StageOutcome.NOT_CONFIGURED])
def test_not_when_reason_did_not_run(sent_record: AssessmentRecord, outcome: StageOutcome) -> None:
    assert SENT not in _note(_reason_as(sent_record, outcome))


@pytest.mark.parametrize(
    "outcome", [StageOutcome.COMPLETED, StageOutcome.FAILED, StageOutcome.TIMED_OUT]
)
def test_shown_when_reason_ran_whatever_its_end(
    sent_record: AssessmentRecord, outcome: StageOutcome
) -> None:
    """A failed or timed-out Reason may already have sent a prompt."""
    assert SENT in _note(_reason_as(sent_record, outcome))


def test_not_without_a_reason_trace(sent_record: AssessmentRecord) -> None:
    stages = tuple(t for t in sent_record.stages if t.name is not StageName.REASON)
    assert SENT not in _note(sent_record.model_copy(update={"stages": stages}))


def test_the_withheld_sentence_is_reworded() -> None:
    record = _run(sl.fake(), "remote_external_opt_in", HOST)
    assert record.withheld_knowledge
    note = knowledge_note(record)
    assert f"{len(record.withheld_knowledge)} {WITHHELD_TAIL}" in note
    assert SENT not in note
    assert HOST not in note


# --- mutations ------------------------------------------------------------------------------


VIEW_MUTATIONS = [
    pytest.param(
        ("        parts.append(_KNOWLEDGE_SENT_EXTERNAL)\n", "        pass\n"),
        "shown",
        id="sentence-dropped",
    ),
    pytest.param(
        (
            "    if record.egress_basis is not RecordEgressBasis.REMOTE_KNOWLEDGE_OPT_IN:\n"
            "        return False\n",
            "",
        ),
        "basis",
        id="basis-condition-dropped",
    ),
    pytest.param(
        ("    return unwithheld and reason_ran\n", "    return reason_ran\n"),
        "withheld",
        id="evidence-condition-dropped",
    ),
    pytest.param(
        ("    return unwithheld and reason_ran\n", "    return unwithheld\n"),
        "reason",
        id="reason-condition-dropped",
    ),
    pytest.param(
        (
            "_REASON_NOT_RUN = frozenset({StageOutcome.SKIPPED, StageOutcome.NOT_CONFIGURED})",
            "_REASON_NOT_RUN = frozenset({StageOutcome.SKIPPED})",
        ),
        "not_configured",
        id="not-configured-counts-as-ran",
    ),
]


@pytest.mark.parametrize(("edit", "case"), VIEW_MUTATIONS)
def test_mutation_of_the_view_sentence_is_caught(
    sent_record: AssessmentRecord, edit: tuple[str, str], case: str
) -> None:
    from divesafe.domain.models import knowledge_ref

    refs = tuple(r for e in sent_record.evidence if (r := knowledge_ref(e.id)) is not None)
    record = {
        "shown": sent_record,
        "basis": sent_record.model_copy(
            update={"egress_basis": RecordEgressBasis.REMOTE_EXTERNAL_OPT_IN}
        ),
        "withheld": sent_record.model_copy(update={"withheld_knowledge": refs}),
        "reason": _reason_as(sent_record, StageOutcome.SKIPPED),
        "not_configured": _reason_as(sent_record, StageOutcome.NOT_CONFIGURED),
    }[case]
    expected = case == "shown"
    assert (SENT in _note(record)) is expected
    mutant = mutated_module(schemas_module, edit)
    assert (SENT in _note(record, mutant)) is not expected
