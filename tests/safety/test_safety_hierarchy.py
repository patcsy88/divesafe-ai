"""Protects the core principle: deterministic rules outrank agent and LLM output."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from tests.conftest import make_evidence

from divesafe.domain import DataCategory, DivePlan, EvidenceItem, Recommendation, RuleResult
from divesafe.risk import EvidencePolicy, RiskRulesEngine, reconcile, severity

pytestmark = pytest.mark.safety

R = Recommendation


class _FixedRule:
    def __init__(self, rule_id: str, outcome: Recommendation) -> None:
        self.rule_id = rule_id
        self.citation = "test fixture (synthetic, not a real safety source)"
        self._outcome = outcome

    def evaluate(self, plan: DivePlan, evidence: object) -> RuleResult:
        return RuleResult(rule_id=self.rule_id, outcome=self._outcome, rationale="fixed")


def _engine(*rules: _FixedRule, required: frozenset[DataCategory] = frozenset()) -> RiskRulesEngine:
    policy = EvidencePolicy(required_categories=required, max_age=timedelta(hours=1))
    return RiskRulesEngine(rules, policy, ruleset_version="test")


def test_severity_order() -> None:
    order = [R.GO, R.CAUTION, R.INSUFFICIENT_EVIDENCE, R.NO_GO]
    assert sorted(order, key=severity) == order


@pytest.mark.parametrize("proposed", [R.GO, R.CAUTION, R.INSUFFICIENT_EVIDENCE, None])
def test_proposal_can_never_relax_a_hard_no_go(proposed: Recommendation | None) -> None:
    result = reconcile(R.NO_GO, proposed)
    assert result.final == R.NO_GO


@pytest.mark.parametrize("deterministic", list(Recommendation))
@pytest.mark.parametrize("proposed", list(Recommendation))
def test_final_is_never_less_conservative_than_either_input(
    deterministic: Recommendation, proposed: Recommendation
) -> None:
    result = reconcile(deterministic, proposed)
    assert severity(result.final) >= severity(deterministic)
    assert severity(result.final) >= severity(proposed)


def test_attempted_downgrade_is_flagged() -> None:
    assert reconcile(R.CAUTION, R.GO).llm_attempted_downgrade is True
    assert reconcile(R.CAUTION, R.NO_GO).llm_attempted_downgrade is False
    assert reconcile(R.CAUTION, None).llm_attempted_downgrade is False


def test_engine_takes_most_severe_rule(plan: DivePlan, now: datetime) -> None:
    engine = _engine(_FixedRule("a", R.GO), _FixedRule("b", R.NO_GO), _FixedRule("c", R.CAUTION))
    assert engine.assess(plan, [], now).recommendation == R.NO_GO


def test_missing_required_evidence_yields_insufficient_evidence(
    plan: DivePlan, now: datetime
) -> None:
    engine = _engine(_FixedRule("a", R.GO), required=frozenset({DataCategory.WAVES_SWELL}))
    assessment = engine.assess(plan, [], now)
    assert assessment.recommendation == R.INSUFFICIENT_EVIDENCE


def test_stale_required_evidence_yields_insufficient_evidence(
    plan: DivePlan, now: datetime
) -> None:
    stale: list[EvidenceItem] = [make_evidence(DataCategory.WAVES_SWELL, age=timedelta(hours=3))]
    engine = _engine(required=frozenset({DataCategory.WAVES_SWELL}))
    assert engine.assess(plan, stale, now).recommendation == R.INSUFFICIENT_EVIDENCE


def test_no_go_rule_outranks_missing_evidence(plan: DivePlan, now: datetime) -> None:
    engine = _engine(_FixedRule("hard", R.NO_GO), required=frozenset({DataCategory.CURRENTS}))
    assert engine.assess(plan, [], now).recommendation == R.NO_GO
