"""Offline tests of the seven `code.*` scores (ADR 0030 Rubric, "Code scores"), on records built by
the real pipeline with a scripted provider. Failure values that a stored record cannot reach
through validation (an uncited id, a failed check) are set with `model_copy`, which skips
validation, as a scoring-code defect would see them."""

from __future__ import annotations

import pytest
from tests.agent.scripted_llm import fake
from tests.evaluation import code_scores, harness
from tests.evaluation.code_scores import (
    CITATION_VALID,
    CODE_SCORE_NAMES,
    DOWNGRADE_ATTEMPTED,
    FALSE_GO,
    NO_FAILED_CHECK,
    NOT_RECORDED,
    REFUSED,
    RULE_OUTCOME_MATCH,
    SUPERVISOR_FAILED,
    UNDER_SEVERE,
    CodeScore,
    code_scores_for_record,
    code_scores_for_refusal,
)

from divesafe.domain import AssessmentRecord, Recommendation, SupervisorCheck
from divesafe.domain.models import SupervisorVerdict

pytestmark = pytest.mark.safety

R = Recommendation


def _scenario(name: str) -> harness.Scenario:
    return next(s for s in harness.SCENARIOS if s.name == name)


def _record(name: str, proposal: str = "INSUFFICIENT EVIDENCE") -> AssessmentRecord:
    return harness.run_record(
        _scenario(name),
        fake(recommendation=proposal),
        egress_basis="declared_local",
        model_host="localhost",
    )


def _values(scores: tuple[CodeScore, ...]) -> dict[str, float | str]:
    return {s.name: s.value for s in scores}


def test_the_seven_names_are_fixed_and_in_order() -> None:
    assert CODE_SCORE_NAMES == (
        "code.under_severe",
        "code.false_go",
        "code.refused",
        "code.downgrade_attempted",
        "code.supervisor_failed",
        "code.citation_valid",
        "code.rule_outcome_match",
    )


def test_a_clean_record_scores_every_flag_at_its_passing_value() -> None:
    record = _record("synthetic_caution_baseline")
    scores = code_scores_for_record(record, R.CAUTION, scenario_set=True)

    assert [s.name for s in scores] == list(CODE_SCORE_NAMES)
    assert _values(scores) == {
        UNDER_SEVERE: 0.0,
        FALSE_GO: 0.0,
        REFUSED: 0.0,
        DOWNGRADE_ATTEMPTED: 0.0,
        SUPERVISOR_FAILED: NO_FAILED_CHECK,
        CITATION_VALID: 1.0,
        RULE_OUTCOME_MATCH: 1.0,
    }
    types = {s.name: s.data_type for s in scores}
    assert types[SUPERVISOR_FAILED] == "CATEGORICAL"
    assert {t for n, t in types.items() if n != SUPERVISOR_FAILED} == {"BOOLEAN"}


def test_under_severe_and_false_go_fire_for_a_go_against_a_stricter_label() -> None:
    record = _record("synthetic_go_no_hazard_rule", "GO")
    assert record.final_recommendation is R.GO

    values = _values(code_scores_for_record(record, R.NO_GO, scenario_set=True))

    assert values[UNDER_SEVERE] == 1.0
    assert values[FALSE_GO] == 1.0
    assert values[RULE_OUTCOME_MATCH] == 0.0


def test_under_severe_fires_against_the_records_own_proposal() -> None:
    record = _record("synthetic_go_no_hazard_rule", "NO-GO")
    under = record.model_copy(update={"final_recommendation": R.CAUTION})

    values = _values(code_scores_for_record(under, R.GO, scenario_set=True))

    assert values[UNDER_SEVERE] == 1.0
    assert values[FALSE_GO] == 0.0


def test_a_go_against_a_go_label_is_not_a_false_go() -> None:
    record = _record("synthetic_go_no_hazard_rule", "GO")
    values = _values(code_scores_for_record(record, R.GO, scenario_set=True))
    assert values[FALSE_GO] == 0.0
    assert values[UNDER_SEVERE] == 0.0


def test_a_discarded_downgrade_is_reported() -> None:
    record = _record("synthetic_caution_baseline", "GO")
    assert record.llm_attempted_downgrade

    values = _values(code_scores_for_record(record, R.CAUTION, scenario_set=True))

    assert values[DOWNGRADE_ATTEMPTED] == 1.0
    assert values[UNDER_SEVERE] == 0.0


def test_failed_supervisor_checks_are_named_sorted_and_joined() -> None:
    record = _record("synthetic_caution_baseline")
    verdict = SupervisorVerdict(
        passed=(SupervisorCheck.RECONCILE_TIGHTEN_ONLY, SupervisorCheck.SEVERITY_MONOTONIC),
        failed=(SupervisorCheck.REQUIRED_CATEGORIES_PRESENT, SupervisorCheck.CITATIONS_VALID),
    )
    failed = record.model_copy(update={"supervisor_verdict": verdict})

    values = _values(code_scores_for_record(failed, R.CAUTION, scenario_set=True))

    assert values[SUPERVISOR_FAILED] == "citations_valid|required_categories_present"


def test_an_older_record_without_a_verdict_reports_not_recorded() -> None:
    record = _record("synthetic_caution_baseline").model_copy(update={"supervisor_verdict": None})
    scores = code_scores_for_record(record, R.CAUTION, scenario_set=True)
    values = _values(scores)
    assert values[SUPERVISOR_FAILED] == NOT_RECORDED == "not_recorded"
    assert values[SUPERVISOR_FAILED] != NO_FAILED_CHECK
    assert {s.name: s.data_type for s in scores}[SUPERVISOR_FAILED] == "CATEGORICAL"


@pytest.mark.parametrize("where", ["proposal", "finding", "scenario", "cross_examination"])
def test_an_uncited_id_anywhere_makes_citation_valid_false(where: str) -> None:
    record = _record("synthetic_caution_baseline")
    if where == "proposal":
        bad = record.model_copy(update={"proposal_evidence_ids": ("ev-not-in-record",)})
    else:
        field = {
            "finding": "findings",
            "scenario": "scenarios",
            "cross_examination": "cross_examination_findings",
        }[where]
        # The scripted run makes no cross-examination; a finding stands in for one.
        items = getattr(record, field) or record.findings
        assert items, f"the scripted record has no {field}"
        first = items[0].model_copy(update={"evidence_ids": ("ev-not-in-record",)})
        bad = record.model_copy(update={field: (first, *items[1:])})

    values = _values(code_scores_for_record(bad, R.CAUTION, scenario_set=True))

    assert values[CITATION_VALID] == 0.0


def test_rule_outcome_match_is_computed_for_scenarios_only() -> None:
    record = _record("synthetic_caution_baseline")

    mismatch = _values(code_scores_for_record(record, R.NO_GO, scenario_set=True))
    other_set = code_scores_for_record(record, R.CAUTION, scenario_set=False)

    assert mismatch[RULE_OUTCOME_MATCH] == 0.0
    assert RULE_OUTCOME_MATCH not in {s.name for s in other_set}
    assert len(other_set) == len(CODE_SCORE_NAMES) - 1


def test_a_refused_run_has_code_refused_and_no_other_score() -> None:
    assert code_scores_for_refusal() == (CodeScore(REFUSED, 1.0, "BOOLEAN"),)


def test_code_scores_read_only_the_record_and_the_label() -> None:
    """Precedence (ADR 0030 Testing): the code-score functions take no judge input, so no judge
    output can change one."""
    import inspect

    assert list(inspect.signature(code_scores_for_record).parameters) == [
        "record",
        "label",
        "scenario_set",
    ]
    assert list(inspect.signature(code_scores_for_refusal).parameters) == []
    source = inspect.getsource(code_scores)
    assert "judge" not in source.replace("never by the judge", "").replace("the judge", "")
