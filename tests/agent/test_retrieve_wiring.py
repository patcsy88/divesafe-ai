"""ADR 0022 Retrieve wiring conditions 1-8 and the risk-reviewer points of 2026-11-10: the one
knowledge connector runs in Retrieve, its items, notes and issues reach the record in record form
with fixed codes, the absence line follows the Retrieve result, and a forced `none` egress basis
leaves a fixed code a human can see. Mutation checks edit copies of `orchestration.pipeline`,
`agents.stage`, `api.schemas` and `api.state`."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest
from tests.agent import scripted_llm as sl
from tests.agent.test_knowledge_connector import SITE, FakeStore, chunk, ver
from tests.safety.kb_fakes import mutated_module

from divesafe.agents import stage as stage_module
from divesafe.agents.prompts import ABSENCE_LINE
from divesafe.api import schemas as schemas_module
from divesafe.api import state as state_module
from divesafe.config import Settings
from divesafe.data import DataGovMyWarningConnector, FetchResult, OpenMeteoMarineConnector
from divesafe.domain import (
    AssessmentRecord,
    KnowledgeNote,
    KnowledgeNoteCode,
    Recommendation,
    StageName,
)
from divesafe.models import FakeProvider
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.rag.connector import KnowledgeConnector, KnowledgeResult
from divesafe.rag.items import RECORD_VALUE_KEYS
from divesafe.rag.store import STORE_UNAVAILABLE, KnowledgeStoreError, Selection

pytestmark = pytest.mark.safety

REVIEW_BY = date(2027, 1, 1)  # after the scripted run's window
SITE_DOC, GUIDE = "kb:site-doc:1:1", "kb:guide:1:1"
SITE_TEXT = "Text of site-doc v1 chunk 1."


def store(*, extra_version: Any = None, empty: bool = False) -> FakeStore:
    if empty:
        return FakeStore(Selection((), (), 0))
    rows = [chunk("site-doc", site=SITE.id, rev=REVIEW_BY), chunk("guide", rev=REVIEW_BY)]
    versions = [ver("site-doc", site=SITE.id, rev=REVIEW_BY), ver("guide", rev=REVIEW_BY)]
    if extra_version is not None:
        versions.append(extra_version)
    return FakeStore(Selection(tuple(versions), tuple(rows), len(rows)))


class Stub:
    """A knowledge connector stand-in whose one `retrieve` gives a fixed result, raises, or
    stalls. Counts its calls."""

    name = "knowledge"

    def __init__(self, result: Any = None, *, stall: bool = False) -> None:
        self.result, self.stall, self.calls = result, stall, 0

    async def retrieve(self, *_a: object) -> KnowledgeResult:
        self.calls += 1
        if self.stall:
            await asyncio.sleep(30)
        if isinstance(self.result, Exception):
            raise self.result
        assert isinstance(self.result, KnowledgeResult)
        return self.result


def _connectors(*extra: Any) -> list[Any]:
    return [
        OpenMeteoMarineConnector(sl._Getter(sl.MARINE)),
        DataGovMyWarningConnector(sl._Getter(sl.WARNINGS)),
        *extra,
    ]


def assess(
    knowledge: Any,
    *,
    provider: Any = None,
    basis: Any = "declared_local",
    host: str | None = "localhost",
    module: Any = pipeline_module,
    connectors: list[Any] | None = None,
    timeout: float | None = None,
) -> AssessmentRecord:
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["connector_timeout_seconds"] = timeout
    record: AssessmentRecord = asyncio.run(
        module.assess_dive(
            assessment_id="wiring-1",
            plan=sl.PLAN,
            site=SITE,
            connectors=connectors if connectors is not None else _connectors(),
            engine=sl.engine(with_warning_rule=True),
            now=sl.NOW,
            provider=provider,
            clock=lambda: sl.NOW,
            egress_basis=basis if provider is not None else None,
            model_host=host if provider is not None else None,
            knowledge_connector=knowledge,
            **kwargs,
        )
    )
    return record


def codes(record: AssessmentRecord, name: StageName) -> list[str]:
    return [c for t in record.stages if t.name is name for c in t.issues]


def texts(provider: FakeProvider) -> str:
    return "\n".join(m.content for r in provider.requests for m in r.messages)


def caught(check: Any, module: Any) -> bool:
    """A mutant is caught when the check finds a miss or the run is refused on the way."""
    try:
        return bool(check(module))
    except (ValueError, AssertionError):
        return True


# --- condition 7: real kb: items through assess_dive ------------------------------------------


def test_real_kb_items_reach_the_record_and_the_prompts_under_declared_local() -> None:
    provider = sl.fake()
    record = assess(KnowledgeConnector(store()), provider=provider)
    assert {SITE_DOC, GUIDE} <= {e.id for e in record.evidence}
    assert (record.notes, record.withheld_knowledge) == ((), ())
    assert codes(record, StageName.RETRIEVE) == []
    (retrieve,) = [t for t in record.stages if t.name is StageName.RETRIEVE]
    assert set(retrieve.evidence_ids) == {SITE_DOC, GUIDE}
    sent = texts(provider)
    assert SITE_DOC in sent and SITE_TEXT in sent
    # Items, no note, no issue: complete, so no absence line.
    assert ABSENCE_LINE.strip() not in sent


# --- condition 2: record form only ------------------------------------------------------------


def _chunk_text_in_record(module: Any) -> bool:
    record = assess(KnowledgeConnector(store()), module=module)
    kb = [e for e in record.evidence if e.id.startswith("kb:")]
    assert kb
    return SITE_TEXT in record.model_dump_json() or any(
        set(e.value) != set(RECORD_VALUE_KEYS) for e in kb
    )


def test_the_record_holds_knowledge_in_record_form_only() -> None:
    assert not _chunk_text_in_record(pipeline_module)


def test_mutation_skipping_record_form_is_caught() -> None:
    mutant = mutated_module(
        pipeline_module,
        ("    evidence = _for_record(run.evidence_set)\n", "    evidence = run.evidence_set\n"),
    )
    assert caught(_chunk_text_in_record, mutant)


# --- condition 3: the app role's connection string only ---------------------------------------

APP_DSN = "postgresql://app:pw@db.example/divesafe"
OWNER_DSN = "postgresql://owner:pw@db.example/divesafe"


def _dsn(module: Any, **settings: Any) -> str | None:
    built = module.build_knowledge_connector(Settings(_env_file=None, **settings))  # type: ignore[call-arg]
    return None if built is None else built._store._dsn  # type: ignore[no-any-return]


def test_the_connector_reads_with_the_app_dsn_and_is_absent_without_one() -> None:
    assert _dsn(state_module, database_url=APP_DSN, admin_database_url=OWNER_DSN) == APP_DSN
    assert _dsn(state_module, admin_database_url=OWNER_DSN) is None
    assert _dsn(state_module) is None


def test_mutation_using_the_owner_dsn_is_caught() -> None:
    mutant = mutated_module(
        state_module,
        (
            "PostgresKnowledgeStore(settings.database_url.get_secret_value())",
            "PostgresKnowledgeStore((settings.admin_database_url or settings.database_url)"
            ".get_secret_value())",
        ),
    )
    assert _dsn(mutant, database_url=APP_DSN, admin_database_url=OWNER_DSN) != APP_DSN


# --- condition 4: the same instance goes to gather_evidence -----------------------------------


def _gather_gets_the_instance(module: Any) -> bool:
    knowledge = KnowledgeConnector(store())
    seen: list[Any] = []
    original = module.gather_evidence

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("knowledge_connector"))
        return await original(*args, **kwargs)

    module.gather_evidence = spy
    try:
        assess(knowledge, module=module)
    finally:
        module.gather_evidence = original
    return seen == [knowledge]


def test_observe_screens_with_the_same_knowledge_connector() -> None:
    assert _gather_gets_the_instance(pipeline_module)


def test_mutation_passing_no_instance_to_observe_is_caught() -> None:
    mutant = mutated_module(
        pipeline_module,
        ("            knowledge_connector=knowledge,\n", "            knowledge_connector=None,\n"),
    )
    assert not _gather_gets_the_instance(mutant)


def test_kb_items_from_an_observe_connector_are_dropped_with_the_fixed_issue() -> None:
    other = KnowledgeConnector(store())  # not the Retrieve instance
    record = assess(None, connectors=_connectors(other))
    assert not any(e.id.startswith("kb:") for e in record.evidence)
    assert any("evidence.knowledge_refused" in i for i in record.evidence_issues)


def test_the_knowledge_connector_is_refused_as_an_observe_connector() -> None:
    knowledge = KnowledgeConnector(store())
    with pytest.raises(ValueError, match="runs in Retrieve"):
        assess(knowledge, connectors=_connectors(knowledge))


# --- condition 5: one retrieve call per run ---------------------------------------------------


def _store_calls(module: Any) -> int:
    fake = store()
    assess(KnowledgeConnector(fake), module=module)
    return len(fake.calls)


def test_one_run_makes_exactly_one_store_call() -> None:
    assert _store_calls(pipeline_module) == 1


def test_mutation_retrieving_twice_is_caught() -> None:
    mutant = mutated_module(
        pipeline_module,
        (
            "    fetch = screen_knowledge(connector, connector, result.fetch)\n",
            "    result = await connector.retrieve(\n"
            "        run.site, run.plan.planned_start, _window_end(run.plan), run.now\n"
            "    )\n"
            "    fetch = screen_knowledge(connector, connector, result.fetch)\n",
        ),
    )
    assert _store_calls(mutant) != 1


# --- conditions 1, 6 and 8: notes and the not-configured code ---------------------------------


def test_no_connector_writes_knowledge_not_configured() -> None:
    record = assess(None)
    assert codes(record, StageName.RETRIEVE) == ["retrieve.knowledge_not_configured"]
    assert record.notes == (KnowledgeNote(code=KnowledgeNoteCode.NOT_CONFIGURED),)


def test_mutation_writing_the_placeholder_code_is_caught() -> None:
    mutant = mutated_module(
        pipeline_module,
        (
            '            codes.append(issue_code(name, "knowledge_not_configured"))\n',
            '            codes.append(issue_code(name, "not_configured"))\n',
        ),
    )
    assert codes(assess(None, module=mutant), StageName.RETRIEVE) != [
        "retrieve.knowledge_not_configured"
    ]


def test_no_documents_is_none_returned_with_count_zero() -> None:
    record = assess(KnowledgeConnector(store(empty=True)))
    assert record.notes == (KnowledgeNote(code=KnowledgeNoteCode.NONE_RETURNED, count=0),)
    assert not any(e.id.startswith("kb:") for e in record.evidence)


def _silent_empty_has_a_note(module: Any) -> bool:
    record = assess(Stub(KnowledgeResult(FetchResult(items=()), ())), module=module)
    return record.notes == (KnowledgeNote(code=KnowledgeNoteCode.NONE_RETURNED, count=0),)


def test_zero_items_with_no_issue_and_no_note_still_gets_none_returned() -> None:
    assert _silent_empty_has_a_note(pipeline_module)


def test_mutation_dropping_the_catch_all_note_is_caught() -> None:
    mutant = mutated_module(
        pipeline_module,
        (
            "        notes = (KnowledgeNote(code=KnowledgeNoteCode.NONE_RETURNED),)\n",
            "        notes = ()\n",
        ),
    )
    assert caught(lambda m: not _silent_empty_has_a_note(m), mutant)


# --- failure, timeout and issue: codes, evidence issues and the view sentence -----------------

FAILED = schemas_module._KNOWLEDGE_FAILED


def _failed_record(module: Any = pipeline_module) -> AssessmentRecord:
    return assess(
        KnowledgeConnector(FakeStore(KnowledgeStoreError(STORE_UNAVAILABLE))), module=module
    )


def test_a_failed_store_is_an_issue_a_code_and_a_sentence_never_go() -> None:
    record = _failed_record()
    assert codes(record, StageName.RETRIEVE) == ["retrieve.knowledge_failed"]
    assert any(i.startswith("knowledge: ") for i in record.evidence_issues)
    assert record.final_recommendation is not Recommendation.GO
    assert FAILED in schemas_module.knowledge_note(record)


def test_a_stalled_connector_times_out_and_proceeds() -> None:
    stub = Stub(stall=True)
    record = assess(stub, timeout=0.05)
    assert codes(record, StageName.RETRIEVE) == ["retrieve.knowledge_timed_out"]
    assert any("TimeoutError" in i for i in record.evidence_issues)
    assert stub.calls == 1
    assert FAILED in schemas_module.knowledge_note(record)


def test_an_issue_without_a_note_gets_the_code_and_the_sentence() -> None:
    issue = "knowledge.store_malformed"
    record = assess(Stub(KnowledgeResult(FetchResult(items=(), issues=(issue,)), ())))
    assert record.notes == ()
    assert codes(record, StageName.RETRIEVE) == ["retrieve.knowledge_issue"]
    assert f"knowledge: {issue}" in record.evidence_issues
    assert FAILED in schemas_module.knowledge_note(record)


def test_mutation_dropping_the_failed_code_is_caught() -> None:
    mutant = mutated_module(
        pipeline_module,
        ('            codes.append(issue_code(name, "knowledge_failed"))\n', "            pass\n"),
    )
    assert codes(_failed_record(mutant), StageName.RETRIEVE) != ["retrieve.knowledge_failed"]


def test_mutation_dropping_the_failure_sentence_is_caught() -> None:
    mutant = mutated_module(
        schemas_module,
        ("        parts.append(_KNOWLEDGE_FAILED)\n", "        pass\n"),
    )
    assert FAILED not in mutant.knowledge_note(_failed_record())


# --- reviewer point 2: the absence line follows the Retrieve result ---------------------------

WITHDRAWN = ver("old-doc", site=SITE.id, withdrawn=True, rev=REVIEW_BY)


def _absence_sent_with_items_and_a_note(module: Any) -> bool:
    provider = sl.fake()
    record = assess(
        KnowledgeConnector(store(extra_version=WITHDRAWN)), provider=provider, module=module
    )
    assert {SITE_DOC, GUIDE} <= {e.id for e in record.evidence}
    assert record.notes, "the fixture needs a note beside the items"
    return ABSENCE_LINE.strip() in texts(provider)


def test_a_note_beside_items_sends_the_absence_line() -> None:
    assert _absence_sent_with_items_and_a_note(pipeline_module)


def test_mutation_ignoring_notes_for_absence_is_caught() -> None:
    mutant = mutated_module(
        pipeline_module,
        (
            "    run.knowledge_incomplete = not fetch.items or bool(notes) or bool(fetch.issues)\n",
            "    run.knowledge_incomplete = not fetch.items\n",
        ),
    )
    assert not _absence_sent_with_items_and_a_note(mutant)


def _stage_absence(module: Any) -> bool:
    provider = sl.fake()
    record = assess(KnowledgeConnector(store()), provider=sl.fake())
    items = [e for e in record.evidence]
    asyncio.run(
        module.run_agent_stage(
            provider,
            sl.PLAN,
            items,
            list(record.rule_results),
            record.deterministic_recommendation,
            egress_basis="declared_local",
            knowledge_incomplete=True,
        )
    )
    return ABSENCE_LINE.strip() in texts(provider)


def test_the_stage_follows_the_retrieve_result_over_the_evidence() -> None:
    assert _stage_absence(stage_module)


def test_mutation_of_the_stage_falling_back_to_evidence_is_caught() -> None:
    mutant = mutated_module(
        stage_module,
        (
            "        else knowledge_incomplete\n",
            "        else not any(is_knowledge(e) for e in evidence)\n",
        ),
    )
    assert not _stage_absence(mutant)


# --- reviewer point 3: a forced `none` basis leaves a visible fixed code ----------------------

CASES = [
    ("not-a-basis", "localhost", pipeline_module.EGRESS_BASIS_UNRECOGNISED),
    (None, "localhost", pipeline_module.EGRESS_BASIS_UNRECOGNISED),
    ("loopback_external_opt_in", None, pipeline_module.EGRESS_HOST_MISSING),
]


def _egress_code(module: Any, basis: Any, host: str | None) -> tuple[str, ...]:
    record = assess(
        KnowledgeConnector(store()), provider=sl.fake(), basis=basis, host=host, module=module
    )
    assert record.egress_basis.value == "none"
    return tuple(i for i in record.agent_issues if i.startswith("egress."))


@pytest.mark.parametrize(("basis", "host", "code"), CASES)
def test_a_forced_none_basis_records_its_fixed_code_and_wording(
    basis: Any, host: str | None, code: str
) -> None:
    record = assess(KnowledgeConnector(store()), provider=sl.fake(), basis=basis, host=host)
    assert record.agent_issues[0] == code
    note = schemas_module._agent_note(record)
    assert schemas_module._EGRESS_WORDING[code] in note
    assert "agent call(s) failed" not in note  # the code is not a failed call
    assert str(basis) not in note or basis is None


def test_a_real_none_basis_records_no_egress_code() -> None:
    assert _egress_code(pipeline_module, "none", None) == ()


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            (
                "        return RecordEgressBasis.NONE, None, EGRESS_BASIS_UNRECOGNISED\n",
                "        return RecordEgressBasis.NONE, None, None\n",
            ),
            id="unrecognised-code-dropped",
        ),
        pytest.param(
            (
                "        return RecordEgressBasis.NONE, None, EGRESS_HOST_MISSING\n",
                "        return RecordEgressBasis.NONE, None, None\n",
            ),
            id="host-missing-code-dropped",
        ),
        pytest.param(
            (
                "        agent_issues=(*((run.egress_code,) if run.egress_code else ()), "
                "*run.agent_issues),\n",
                "        agent_issues=run.agent_issues,\n",
            ),
            id="code-not-recorded",
        ),
    ],
)
def test_mutation_of_the_egress_code_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(pipeline_module, edit)
    missed = [code for basis, host, code in CASES if _egress_code(mutant, basis, host) != (code,)]
    assert missed


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            (
                '    note += "".join(w for code, w in _EGRESS_WORDING.items() '
                "if code in record.agent_issues)\n",
                "",
            ),
            id="wording-dropped",
        ),
        pytest.param(
            (
                "    calls = [i for i in record.agent_issues if i not in _EGRESS_WORDING]\n",
                "    calls = list(record.agent_issues)\n",
            ),
            id="code-counted-as-a-failed-call",
        ),
    ],
)
def test_mutation_of_the_egress_wording_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(schemas_module, edit)
    record = assess(
        KnowledgeConnector(store()), provider=sl.fake(), basis="not-a-basis", host="localhost"
    )
    note = mutant._agent_note(record)
    assert (
        mutant._EGRESS_WORDING[record.agent_issues[0]] not in note or "agent call(s) failed" in note
    )


def test_the_egress_codes_are_fixed_strings() -> None:
    assert json.dumps(pipeline_module.EGRESS_CODES) == json.dumps(
        ["egress.basis_unrecognised", "egress.host_missing"]
    )
