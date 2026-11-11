"""Contract every AssessmentRepository adapter must satisfy (run against each adapter)."""

from __future__ import annotations

import os
import threading
import warnings
from collections.abc import Callable
from datetime import timedelta

import psycopg
import psycopg.conninfo
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
    PostgresAssessmentRepository,
    RecordExistsError,
    RecordNotFoundError,
)
from divesafe.services.migrations import MIGRATIONS
from divesafe.services.postgres import GUARD_TRIGGERS, install_schema

R = Recommendation
TEST_DSN = os.environ.get("DIVESAFE_TEST_DATABASE_URL")


def _forget_knowledge_store_without_history() -> None:
    """Some integration tests drop `schema_migrations` to look like a fresh database, then call
    reset_test_database(), which replays every migration. The knowledge tables can never be
    emptied (ADR 0022), so a replay of migration 4 would collide with them. A database with no
    history (missing, or not 1..N) is treated as one with no knowledge objects, so they are
    removed here, and only then, in the *_test database this module already insists on."""
    assert TEST_DSN is not None
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        if conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone() != (None,):
            rows = conn.execute("SELECT version FROM public.schema_migrations").fetchall()
            if sorted(r[0] for r in rows) == list(range(1, len(MIGRATIONS) + 1)):
                return
            # An incomplete or edited history is replayed from scratch, like a fresh database.
            conn.execute("DROP TABLE public.schema_migrations")
    drop_knowledge_objects()


def drop_knowledge_objects() -> None:
    """Remove the migration-4 objects (test databases only; see the function above)."""
    assert TEST_DSN is not None
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS public.kb_status_events, public.kb_chunks, public.kb_documents"
        )
        conn.execute("DROP SEQUENCE IF EXISTS public.kb_status_event_order_seq")
        for function in (
            "kb_no_update_delete",
            "kb_no_truncate",
            "kb_documents_insert",
            "kb_chunks_insert",
            "kb_status_events_insert",
        ):
            conn.execute(f"DROP FUNCTION IF EXISTS public.{function}()")  # type: ignore[call-overload]


def _repair_guard_triggers_left_disabled() -> None:
    """A test that disables a guard and is killed (or fails) before re-enabling it would leave the
    test database refused by `migrate()`'s post-check for every later run. Re-enable before the
    install, and warn when something was actually repaired so the earlier run is noticed. The
    post-check still judges `migrate()`'s own statements afterwards."""
    assert TEST_DSN is not None
    with psycopg.connect(TEST_DSN) as conn:
        if conn.execute("SELECT to_regclass('public.assessments')").fetchone() == (None,):
            return
        disabled = [
            row[0]
            for row in conn.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.assessments'::regclass "
                "AND tgname = ANY(%s) AND tgenabled <> 'A'",
                (list(GUARD_TRIGGERS),),
            ).fetchall()
        ]
        for trigger in disabled:
            conn.execute(f"ALTER TABLE assessments ENABLE ALWAYS TRIGGER {trigger}")  # type: ignore[call-overload]
    if disabled:
        warnings.warn(
            f"re-enabled guard triggers left disabled by an earlier run: {sorted(disabled)}",
            stacklevel=2,
        )


def reset_test_database() -> None:
    """Install the guard as the owner and empty the table. Refuses any database whose name does
    not end in `_test`, so a copied production DSN can never be wiped."""
    assert TEST_DSN is not None
    name = psycopg.conninfo.conninfo_to_dict(TEST_DSN).get("dbname", "")
    if not str(name).endswith("_test"):
        raise RuntimeError("DIVESAFE_TEST_DATABASE_URL must point at a database named *_test")
    _forget_knowledge_store_without_history()
    _repair_guard_triggers_left_disabled()
    install_schema(TEST_DSN)
    with psycopg.connect(TEST_DSN) as conn:
        for trigger in GUARD_TRIGGERS:
            conn.execute(f"ALTER TABLE assessments DISABLE TRIGGER {trigger}")
        conn.execute("TRUNCATE assessments")
        conn.execute("TRUNCATE pending_runs, run_failure_counts")
        for trigger in GUARD_TRIGGERS:
            conn.execute(f"ALTER TABLE assessments ENABLE ALWAYS TRIGGER {trigger}")


def _postgres() -> AssessmentRepository:
    assert TEST_DSN is not None
    reset_test_database()
    return PostgresAssessmentRepository(TEST_DSN)


ADAPTERS: dict[str, Callable[[], AssessmentRepository]] = {
    "in-memory": InMemoryAssessmentRepository,
    "postgres": pytest.param(
        _postgres,
        marks=[
            pytest.mark.integration,
            pytest.mark.skipif(TEST_DSN is None, reason="DIVESAFE_TEST_DATABASE_URL not set"),
        ],
    ),  # type: ignore[dict-item]
}


@pytest.fixture(params=[v for v in ADAPTERS.values()], ids=list(ADAPTERS))
def repo(request: pytest.FixtureRequest) -> AssessmentRepository:
    return request.param()


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


# --- ADR 0026: a pending assessment may be reassigned, append-only ------------------------------


def _assigned(record: AssessmentRecord, who: str = "ann") -> AssessmentRecord:
    return AssessmentRecord.model_validate({**record.model_dump(), "assigned_decider": who})


def _moved(record: AssessmentRecord, to: str = "bob", *, at: int = 1) -> AssessmentRecord:
    from datetime import timedelta as _td

    from divesafe.domain import Reassignment

    move = Reassignment(
        from_decider=str(record.assigned_decider),
        to_decider=to,
        reassigned_by="root",
        reassigned_at=NOW + _td(minutes=at),
        key_id="0123456789ab",
    )
    return AssessmentRecord.model_validate(
        {
            **record.model_dump(),
            "assigned_decider": to,
            "reassignments": (*record.reassignments, move),
        }
    )


def test_a_pending_record_can_be_reassigned_and_then_again(repo: AssessmentRepository) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    first = _moved(pending, "bob")
    repo.replace(pending, first)
    assert repo.get("r1") == first
    second = _moved(first, "cara", at=2)
    repo.replace(first, second)
    stored = repo.get("r1")
    assert stored is not None and stored.assigned_decider == "cara"
    assert [m.to_decider for m in stored.reassignments] == ["bob", "cara"]


def test_a_reassigned_record_can_still_be_decided_by_the_new_assignee(
    repo: AssessmentRepository,
) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    moved = _moved(pending, "bob")
    repo.replace(pending, moved)
    later = HumanDecision(
        decided_by="bob",
        decision=R.INSUFFICIENT_EVIDENCE,
        decided_at=NOW + timedelta(minutes=10),
        is_override=False,
    )
    decided = AssessmentRecord.model_validate(
        {**moved.model_dump(), "human_decision": later.model_dump()}
    )
    repo.replace(moved, decided)
    assert repo.get("r1") == decided


def test_a_decided_record_cannot_be_reassigned(repo: AssessmentRepository) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    decided = _decided(pending, "ann")
    repo.replace(pending, decided)
    with pytest.raises(InvalidSuccessorError):
        repo.replace(decided, _moved(decided, "bob", at=0))
    assert repo.get("r1") == decided


def test_the_assignee_cannot_change_without_a_recorded_move(repo: AssessmentRepository) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    sneaky = AssessmentRecord.model_validate({**pending.model_dump(), "assigned_decider": "bob"})
    with pytest.raises(InvalidSuccessorError):
        repo.replace(pending, sneaky)
    assert repo.get("r1") == pending


def test_a_reassignment_cannot_be_removed_rewritten_or_stacked(repo: AssessmentRepository) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    first = _moved(pending, "bob")
    repo.replace(pending, first)
    two_at_once = _moved(_moved(first, "cara", at=2), "dan", at=3)
    with pytest.raises(InvalidSuccessorError):
        repo.replace(first, two_at_once)
    with pytest.raises(InvalidSuccessorError):
        repo.replace(first, pending)  # undoing a move
    rewritten = _moved(pending, "cara")  # a different history ending elsewhere
    with pytest.raises(InvalidSuccessorError):
        repo.replace(first, rewritten)
    assert repo.get("r1") == first


def test_a_reassignment_cannot_carry_another_change(repo: AssessmentRepository) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    moved = _moved(pending, "bob")
    altered = AssessmentRecord.model_validate({**moved.model_dump(), "ruleset_version": "other"})
    with pytest.raises(InvalidSuccessorError):
        repo.replace(pending, altered)
    assert repo.get("r1") == pending


def test_a_move_must_start_from_the_current_assignee(repo: AssessmentRepository) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    from divesafe.domain import Reassignment

    forged = Reassignment(
        from_decider="zed",
        to_decider="bob",
        reassigned_by="root",
        reassigned_at=NOW + timedelta(minutes=1),
        key_id="0123456789ab",
    )
    wrong = AssessmentRecord.model_validate(
        {**pending.model_dump(), "assigned_decider": "bob", "reassignments": (forged,)}
    )
    with pytest.raises(InvalidSuccessorError):
        repo.replace(pending, wrong)
    assert repo.get("r1") == pending


def test_an_earlier_move_cannot_be_rewritten_in_place(repo: AssessmentRepository) -> None:
    pending = _assigned(_record())
    repo.create(pending)
    first = _moved(pending, "bob")
    repo.replace(pending, first)
    second = _moved(first, "cara", at=2)
    rewritten_first = first.reassignments[0].model_copy(update={"reassigned_by": "mallory"})
    forged = AssessmentRecord.model_validate(
        {
            **second.model_dump(),
            "reassignments": (rewritten_first, second.reassignments[1]),
        }
    )
    with pytest.raises(InvalidSuccessorError):
        repo.replace(first, forged)
    assert repo.get("r1") == first
