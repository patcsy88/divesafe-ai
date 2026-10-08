"""Contract every AssessmentRepository adapter must satisfy (run against each adapter)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta

import pytest
from tests.conftest import NOW, make_evidence

from divesafe.domain import (
    ActualConditions,
    AssessmentRecord,
    DataCategory,
    DivePlan,
    HumanDecision,
    Recommendation,
    RuleResult,
)
from divesafe.services import (
    AssessmentRepository,
    ConflictError,
    InMemoryAssessmentRepository,
    InvalidSuccessorError,
    RecordExistsError,
    RecordNotFoundError,
)

R = Recommendation
ADAPTERS: dict[str, Callable[[], AssessmentRepository]] = {
    "in-memory": InMemoryAssessmentRepository
}


@pytest.fixture(params=list(ADAPTERS))
def repo(request: pytest.FixtureRequest) -> AssessmentRepository:
    return ADAPTERS[request.param]()


def _record(record_id: str = "r1") -> AssessmentRecord:
    evidence = make_evidence(DataCategory.WAVES_SWELL)
    rule = RuleResult(
        rule_id="r", outcome=R.INSUFFICIENT_EVIDENCE, rationale="x", evidence_ids=(evidence.id,)
    )
    return AssessmentRecord(
        id=record_id,
        created_at=NOW,
        plan=DivePlan(
            site_id="s",
            planned_start=NOW + timedelta(hours=1),
            planned_duration_minutes=30,
            max_depth_m=10,
        ),
        evidence=(evidence,),
        rule_results=(rule,),
        deterministic_recommendation=R.INSUFFICIENT_EVIDENCE,
        proposed_recommendation=None,
        final_recommendation=R.INSUFFICIENT_EVIDENCE,
        llm_attempted_downgrade=False,
        ruleset_version="test",
    )


def _decided(record: AssessmentRecord, who: str = "a") -> AssessmentRecord:
    decision = HumanDecision(
        decided_by=who, decision=R.INSUFFICIENT_EVIDENCE, decided_at=NOW, is_override=False
    )
    return AssessmentRecord.model_validate(
        {**record.model_dump(), "human_decision": decision.model_dump()}
    )


def test_create_get_and_duplicate_id(repo: AssessmentRepository) -> None:
    record = _record()
    repo.create(record)
    assert repo.get("r1") == record
    assert repo.get("missing") is None
    with pytest.raises(RecordExistsError):
        repo.create(record)


def test_decision_can_be_appended_once(repo: AssessmentRepository) -> None:
    pending = _record()
    repo.create(pending)
    decided = _decided(pending)
    repo.replace(pending, decided)
    assert repo.get("r1") == decided
    with pytest.raises(ConflictError):  # stale `expected`
        repo.replace(pending, _decided(pending, "b"))
    with pytest.raises(InvalidSuccessorError):  # replacing the existing decision
        repo.replace(decided, _decided(pending, "b"))


def test_second_writer_with_the_same_read_loses(repo: AssessmentRepository) -> None:
    pending = _record()
    repo.create(pending)
    results: list[str] = []

    def write(who: str) -> None:
        try:
            repo.replace(pending, _decided(pending, who))
            results.append("ok")
        except ConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=write, args=(w,)) for w in ("a", "b", "c", "d")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict", "conflict", "conflict", "ok"]


def test_nothing_but_the_append_only_fields_may_change(repo: AssessmentRepository) -> None:
    pending = _record()
    repo.create(pending)
    tampered = AssessmentRecord.model_validate(
        {**pending.model_dump(), "explanation": "rewritten history"}
    )
    with pytest.raises(InvalidSuccessorError):
        repo.replace(pending, tampered)
    assert repo.get("r1") == pending


def test_actual_conditions_need_a_decision_and_are_write_once(repo: AssessmentRepository) -> None:
    pending = _record()
    repo.create(pending)
    decided = _decided(pending)
    repo.replace(pending, decided)
    actual = ActualConditions(
        reported_at=NOW + timedelta(hours=2), reported_by="a", observations={}
    )
    done = AssessmentRecord.model_validate(
        {**decided.model_dump(), "actual_conditions": actual.model_dump()}
    )
    repo.replace(decided, done)
    other = ActualConditions(
        reported_at=NOW + timedelta(hours=3), reported_by="a", observations={"x": 1}
    )
    again = AssessmentRecord.model_validate(
        {**done.model_dump(), "actual_conditions": other.model_dump()}
    )
    with pytest.raises(InvalidSuccessorError):
        repo.replace(done, again)


def test_replace_unknown_or_id_changing_record_is_refused(repo: AssessmentRepository) -> None:
    with pytest.raises(RecordNotFoundError):
        repo.replace(_record("ghost"), _decided(_record("ghost")))
    pending = _record()
    repo.create(pending)
    renamed = AssessmentRecord.model_validate({**_decided(pending).model_dump(), "id": "other"})
    with pytest.raises(InvalidSuccessorError):
        repo.replace(pending, renamed)


def test_unvalidated_successor_is_rejected(repo: AssessmentRepository) -> None:
    pending = _record()
    repo.create(pending)
    forged = AssessmentRecord.model_construct(
        **{**_decided(pending).__dict__, "final_recommendation": R.GO}
    )
    with pytest.raises(InvalidSuccessorError):
        repo.replace(pending, forged)
