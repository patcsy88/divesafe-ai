"""Fail-closed engine behaviour and audit-record integrity (risk review B1-B3, M2, M3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.conftest import NOW, make_evidence

from divesafe.domain import (
    ActualConditions,
    AssessmentRecord,
    DataCategory,
    DivePlan,
    EvidenceItem,
    HumanDecision,
    Recommendation,
    RuleResult,
    Scenario,
    ScenarioKind,
)
from divesafe.risk import EvidencePolicy, RiskRulesEngine

pytestmark = pytest.mark.safety

R = Recommendation
WAVES = DataCategory.WAVES_SWELL


class _Rule:
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self, rule_id: str, outcome: Recommendation | None, boom: bool = False) -> None:
        self.rule_id = rule_id
        self._outcome = outcome
        self._boom = boom

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult | None:
        if self._boom:
            raise RuntimeError("boom")
        if self._outcome is None:
            return None
        return RuleResult(rule_id=self.rule_id, outcome=self._outcome, rationale="fixed")


def _engine(*rules: _Rule, required: frozenset[DataCategory] = frozenset()) -> RiskRulesEngine:
    return RiskRulesEngine(rules, EvidencePolicy(required, timedelta(hours=1)), "test")


# --- B1: never GO by absence -------------------------------------------------------------


def test_empty_engine_is_not_go(plan: DivePlan, now: datetime) -> None:
    assert _engine().assess(plan, [], now).recommendation == R.INSUFFICIENT_EVIDENCE


def test_rules_that_do_not_apply_are_not_go(plan: DivePlan, now: datetime) -> None:
    engine = _engine(_Rule("a", None), _Rule("b", None))
    assert engine.assess(plan, [], now).recommendation == R.INSUFFICIENT_EVIDENCE


def test_go_requires_an_explicit_go_rule(plan: DivePlan, now: datetime) -> None:
    assert _engine(_Rule("a", R.GO)).assess(plan, [], now).recommendation == R.GO


# --- M2: freshness -----------------------------------------------------------------------


def test_future_dated_evidence_is_not_fresh(plan: DivePlan, now: datetime) -> None:
    future = [make_evidence(WAVES, age=timedelta(minutes=-30))]
    engine = _engine(_Rule("a", R.GO), required=frozenset({WAVES}))
    assert engine.assess(plan, future, now).recommendation == R.INSUFFICIENT_EVIDENCE


def test_evidence_exactly_at_max_age_is_fresh_and_one_second_older_is_not(
    plan: DivePlan, now: datetime
) -> None:
    engine = _engine(_Rule("a", R.GO), required=frozenset({WAVES}))
    at_limit = [make_evidence(WAVES, age=timedelta(hours=1))]
    over = [make_evidence(WAVES, age=timedelta(hours=1, seconds=1))]
    assert engine.assess(plan, at_limit, now).recommendation == R.GO
    assert engine.assess(plan, over, now).recommendation == R.INSUFFICIENT_EVIDENCE


def test_evidence_in_wrong_category_does_not_satisfy_requirement(
    plan: DivePlan, now: datetime
) -> None:
    other = [make_evidence(DataCategory.WIND)]
    engine = _engine(_Rule("a", R.GO), required=frozenset({WAVES}))
    assert engine.assess(plan, other, now).recommendation == R.INSUFFICIENT_EVIDENCE


def test_non_positive_max_age_rejected() -> None:
    with pytest.raises(ValueError):
        EvidencePolicy(frozenset(), timedelta(0))


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValidationError):
        DivePlan(
            site_id="s",
            planned_start=datetime(2026, 1, 1, 12, 0),
            planned_duration_minutes=30,
            max_depth_m=10,
        )


# --- M3 / M4: rule failures and citations ------------------------------------------------


def test_raising_rule_fails_closed(plan: DivePlan, now: datetime) -> None:
    engine = _engine(_Rule("ok", R.GO), _Rule("bad", None, boom=True))
    assessment = engine.assess(plan, [], now)
    assert assessment.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id == "rule.error.bad" for r in assessment.rule_results)


def test_rule_without_citation_is_refused() -> None:
    rule = _Rule("a", R.GO)
    rule.citation = "  "
    with pytest.raises(ValueError):
        _engine(rule)


# --- B2 / B3: audit record integrity -----------------------------------------------------


def _record(**overrides: object) -> AssessmentRecord:
    evidence = make_evidence(WAVES)
    base: dict[str, object] = {
        "id": "rec-1",
        "created_at": NOW,
        "plan": DivePlan(
            site_id="s",
            planned_start=NOW + timedelta(hours=1),
            planned_duration_minutes=30,
            max_depth_m=10,
        ),
        "evidence": (evidence,),
        "rule_results": (
            RuleResult(rule_id="r", outcome=R.NO_GO, rationale="x", evidence_ids=(evidence.id,)),
        ),
        "deterministic_recommendation": R.NO_GO,
        "proposed_recommendation": None,
        "final_recommendation": R.NO_GO,
        "llm_attempted_downgrade": False,
        "confidence": 0.5,
        "ruleset_version": "test",
    }
    base.update(overrides)
    return AssessmentRecord(**base)  # type: ignore[arg-type]


def _human(decision: Recommendation, *, override: bool, rationale: str | None = None):  # type: ignore[no-untyped-def]
    return HumanDecision(
        decided_by="leader",
        decision=decision,
        decided_at=NOW,
        is_override=override,
        override_rationale=rationale,
    )


def test_consistent_record_is_pending_until_human_decides() -> None:
    assert _record().status == "PENDING_HUMAN"
    decided = _record(human_decision=_human(R.NO_GO, override=False))
    assert decided.status == "DECIDED"


def test_record_with_relaxed_final_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _record(final_recommendation=R.GO)


def test_record_with_wrong_deterministic_result_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _record(deterministic_recommendation=R.GO, final_recommendation=R.GO)


def test_record_with_wrong_downgrade_flag_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _record(proposed_recommendation=R.GO, llm_attempted_downgrade=False)


def test_record_without_rule_results_must_be_insufficient_evidence() -> None:
    with pytest.raises(ValidationError):
        _record(rule_results=(), deterministic_recommendation=R.GO, final_recommendation=R.GO)


def test_record_citing_unknown_evidence_is_rejected() -> None:
    bad = RuleResult(rule_id="r", outcome=R.NO_GO, rationale="x", evidence_ids=("nope",))
    with pytest.raises(ValidationError):
        _record(rule_results=(bad,))


def test_override_requires_rationale() -> None:
    for rationale in (None, "", "   "):
        with pytest.raises(ValidationError):
            _human(R.GO, override=True, rationale=rationale)


def test_override_flag_must_match_the_decision() -> None:
    with pytest.raises(ValidationError):
        _record(human_decision=_human(R.GO, override=False))
    ok = _record(human_decision=_human(R.GO, override=True, rationale="Site briefed by leader."))
    assert ok.human_decision is not None and ok.human_decision.is_override


def test_decision_maker_must_be_identified() -> None:
    with pytest.raises(ValidationError):
        HumanDecision(decided_by=" ", decision=R.NO_GO, decided_at=NOW, is_override=False)


def test_actual_conditions_require_a_human_decision() -> None:
    actual = ActualConditions(reported_at=NOW, reported_by="leader", observations={})
    with pytest.raises(ValidationError):
        _record(actual_conditions=actual)


def test_scenarios_must_cite_evidence() -> None:
    with pytest.raises(ValidationError):
        Scenario(kind=ScenarioKind.MARGINAL, summary="s", evidence_ids=(), confidence=0.5)
