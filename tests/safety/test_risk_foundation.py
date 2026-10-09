"""Risk-engine foundation. Required-test mapping: (3) stale data, (5) risk assessment structure,
(8) deterministic precedence, (9) LLM cannot bypass rules, (10) INSUFFICIENT EVIDENCE handling."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.agent.scripted_llm import fake, run_pipeline
from tests.conftest import NOW, make_evidence

from divesafe.domain import (
    THRESHOLD_FACTORS,
    DataCategory,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskAssessment,
    RiskFactorKind,
    RuleResult,
    ThresholdStatus,
)
from divesafe.risk import (
    EvidencePolicy,
    RiskEngine,
    RiskRulesEngine,
    placeholder_rules,
    reconcile,
    severity,
)

pytestmark = pytest.mark.safety
R = Recommendation
K = RiskFactorKind
WAVES = DataCategory.WAVES_SWELL


class _Fixed:
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(
        self,
        rule_id: str,
        outcome: Recommendation,
        factor: RiskFactorKind | None = None,
        status: ThresholdStatus | None = None,
    ) -> None:
        self.rule_id = rule_id
        self._outcome, self._factor, self._status = outcome, factor, status
        self.expires_at = NOW + timedelta(days=3650)  # synthetic: a validated rule must expire

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        return RuleResult(
            rule_id=self.rule_id,
            outcome=self._outcome,
            rationale="synthetic",
            factor=self._factor,
            threshold_status=self._status,
        )


def _production_engine() -> RiskRulesEngine:
    from divesafe.api.state import build_engine
    from divesafe.config import Settings

    engine = build_engine(Settings(evidence_max_age_minutes=60))
    assert engine is not None
    return engine


def _engine(*rules: object, required: set[DataCategory] | None = None) -> RiskRulesEngine:
    policy = EvidencePolicy(
        frozenset(required or set()), timedelta(hours=1), degraded_may_support_go=True
    )
    return RiskRulesEngine(
        rules,  # type: ignore[arg-type]
        policy,
        "test",
        required_factors=frozenset(),  # synthetic GO rules; real rulesets keep the default
    )


# --- (3) stale data -------------------------------------------------------------------------


def test_stale_required_evidence_is_detected(plan: DivePlan) -> None:
    stale = [make_evidence(WAVES, age=timedelta(hours=1, seconds=1))]
    fresh = [make_evidence(WAVES, age=timedelta(minutes=59))]
    engine = _engine(_Fixed("go", R.GO), required={WAVES})
    assert engine.assess(plan, stale, NOW).recommendation == R.INSUFFICIENT_EVIDENCE
    assert engine.assess(plan, fresh, NOW).recommendation == R.GO
    stale_result = engine.assess(plan, stale, NOW).rule_results[0]
    assert stale_result.factor == K.DATA_FRESHNESS


# --- (5) risk assessment structure ----------------------------------------------------------


def test_assessment_has_the_documented_structure(plan: DivePlan) -> None:
    engine: RiskEngine = _engine(_Fixed("a", R.CAUTION), required=set())
    result = engine.assess(plan, [], NOW)
    assert isinstance(result, RiskAssessment)
    assert result.recommendation == R.CAUTION
    assert [r.rule_id for r in result.rule_results] == ["a"]
    assert result.ruleset_version == "test" and result.evaluated_at == NOW


def test_an_assessment_cannot_be_built_with_a_relaxed_recommendation() -> None:
    results = (RuleResult(rule_id="r", outcome=R.NO_GO, rationale="x"),)
    for bad in (R.GO, R.CAUTION, R.INSUFFICIENT_EVIDENCE):
        with pytest.raises(ValidationError):
            RiskAssessment(
                recommendation=bad, rule_results=results, ruleset_version="v", evaluated_at=NOW
            )
    with pytest.raises(ValidationError):  # no results means INSUFFICIENT EVIDENCE, never GO
        RiskAssessment(recommendation=R.GO, rule_results=(), ruleset_version="v", evaluated_at=NOW)


def test_every_threshold_factor_is_reported_unevaluated_until_a_validated_rule_exists(
    plan: DivePlan,
) -> None:
    engine = _strict_engine(*placeholder_rules())
    result = engine.assess(plan, [], NOW)
    assert set(result.unevaluated_factors) == set(THRESHOLD_FACTORS)
    validated = _Fixed("w", R.GO, factor=K.WIND, status=ThresholdStatus.VALIDATED)
    covered = _strict_engine(*placeholder_rules(), validated).assess(plan, [], NOW)
    assert K.WIND not in covered.unevaluated_factors
    assert set(covered.unevaluated_factors) == set(THRESHOLD_FACTORS) - {K.WIND}


def test_the_record_keeps_the_unevaluated_factors(plan: DivePlan) -> None:
    record = run_pipeline(None, risk_engine=_production_engine())
    assert set(record.unevaluated_factors) == set(THRESHOLD_FACTORS)


# --- (8) deterministic precedence ----------------------------------------------------------


def test_a_hard_no_go_outranks_every_other_result(plan: DivePlan) -> None:
    engine = _engine(
        _Fixed("go", R.GO),
        _Fixed("caution", R.CAUTION),
        _Fixed("nogo", R.NO_GO),
        *placeholder_rules(),
    )
    assert engine.assess(plan, [], NOW).recommendation == R.NO_GO


def test_no_other_result_can_outrank_a_no_go() -> None:
    for other in (R.GO, R.CAUTION, R.INSUFFICIENT_EVIDENCE):
        assert severity(R.NO_GO) > severity(other)


def test_placeholder_rules_never_support_go_or_caution(plan: DivePlan) -> None:
    results = _engine(*placeholder_rules()).assess(plan, [], NOW)
    assert all(r.outcome == R.INSUFFICIENT_EVIDENCE for r in results.rule_results)
    assert results.recommendation == R.INSUFFICIENT_EVIDENCE


# --- (9) an LLM cannot bypass the rules ----------------------------------------------------


@pytest.mark.parametrize("proposal", [R.GO, R.CAUTION])
def test_a_proposal_cannot_relax_any_non_go_result(proposal: Recommendation) -> None:
    for deterministic in (R.CAUTION, R.INSUFFICIENT_EVIDENCE, R.NO_GO):
        if severity(proposal) < severity(deterministic):
            outcome = reconcile(deterministic, proposal)
            assert outcome.final == deterministic and outcome.llm_attempted_downgrade


@pytest.mark.parametrize("hostile", ["GO", "CAUTION"])
def test_through_the_pipeline_a_hostile_llm_cannot_beat_the_placeholder_ruleset(
    hostile: str,
) -> None:
    live = {
        WAVES,
        DataCategory.CURRENTS,
        DataCategory.SEA_TEMPERATURE,
        DataCategory.MARINE_WARNINGS,
    }
    placeholders_only = _engine(*placeholder_rules(), required=live)
    record = run_pipeline(fake(recommendation=hostile), risk_engine=placeholders_only)
    assert record.deterministic_recommendation == R.INSUFFICIENT_EVIDENCE
    assert record.proposed_recommendation == R(hostile)
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert record.llm_attempted_downgrade is True


# --- (10) INSUFFICIENT EVIDENCE handling ---------------------------------------------------


def test_perfect_fresh_evidence_still_cannot_produce_go_while_thresholds_are_tbd(
    plan: DivePlan,
) -> None:
    evidence = [make_evidence(c) for c in (WAVES, DataCategory.WIND, DataCategory.CURRENTS)]
    engine = _engine(*placeholder_rules(), required={WAVES, DataCategory.WIND})
    assert engine.assess(plan, evidence, NOW).recommendation == R.INSUFFICIENT_EVIDENCE


def test_missing_required_evidence_is_insufficient_evidence_not_go(plan: DivePlan) -> None:
    engine = _engine(_Fixed("go", R.GO), required={WAVES})
    assert engine.assess(plan, [], NOW).recommendation == R.INSUFFICIENT_EVIDENCE


def test_an_empty_ruleset_is_insufficient_evidence(plan: DivePlan) -> None:
    assert _engine().assess(plan, [], NOW).recommendation == R.INSUFFICIENT_EVIDENCE


def test_the_api_presents_insufficient_evidence_with_what_was_not_evaluated() -> None:
    from divesafe.api.schemas import AssessmentView

    note = AssessmentView.of(run_pipeline(None, risk_engine=_production_engine())).outcome_note
    assert "not a green light" in note
    assert "Not evaluated (no validated threshold):" in note and "wave_height" in note


# --- placeholders are honest ---------------------------------------------------------------


def test_every_placeholder_is_marked_tbd_and_requires_domain_validation() -> None:
    rules = placeholder_rules()
    assert {r.factor for r in rules} == set(THRESHOLD_FACTORS)
    for rule in rules:
        result = rule.evaluate(
            DivePlan(site_id="s", planned_start=NOW, planned_duration_minutes=30, max_depth_m=10),
            [],
        )
        assert result.threshold_status == ThresholdStatus.TBD
        assert "TBD" in result.rationale and "REQUIRES DOMAIN VALIDATION" in result.rationale
        assert "NOT evaluated" in result.rationale
        assert "TBD" in rule.citation


@pytest.mark.parametrize(
    "status", [ThresholdStatus.TBD, ThresholdStatus.REQUIRES_DOMAIN_VALIDATION]
)
@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION])
def test_an_unvalidated_threshold_cannot_yield_go_or_caution(
    status: ThresholdStatus, outcome: Recommendation
) -> None:
    with pytest.raises(ValidationError):
        RuleResult(rule_id="r", outcome=outcome, rationale="x", threshold_status=status)
    for safe in (R.NO_GO, R.INSUFFICIENT_EVIDENCE):
        RuleResult(rule_id="r", outcome=safe, rationale="x", threshold_status=status)


def find_numeric_literals(source: str) -> list[str]:
    """Numbers that could be invented limits: every float, any int outside the severity ranks
    0-3, and numeric-looking strings. Walks every `risk/` module including subpackages."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        value = node.value
        if isinstance(value, float):
            offenders.append(f"line {node.lineno}: float {value}")
        elif isinstance(value, int) and value not in {0, 1, 2, 3}:
            offenders.append(f"line {node.lineno}: int {value}")
        elif isinstance(value, str):
            try:
                float(value.strip())
            except ValueError:
                continue
            offenders.append(f"line {node.lineno}: numeric string {value!r}")
    return offenders


def test_no_numeric_threshold_is_hard_coded_in_the_risk_package() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "divesafe" / "risk"
    found = {
        str(path.relative_to(root)): hits
        for path in root.rglob("*.py")
        if (hits := find_numeric_literals(path.read_text()))
    }
    assert found == {}


@pytest.mark.parametrize(
    "planted",
    [
        "MAX_WAVE = 2.5",
        "LIMIT = 40",
        'LIMIT = "2.5"',
        "x = float('1.5') if False else 1.0",
        "timedelta(hours=6)",
        "y = 3.0",
    ],
)
def test_the_literal_scanner_actually_trips_on_a_planted_limit(planted: str) -> None:
    assert find_numeric_literals(planted) != []
    assert find_numeric_literals("rank = 2\nflag = True\nname = 'wave_height'") == []


# --- review follow-ups: GO needs every threshold factor covered ------------------------------


def _validated(factor: RiskFactorKind, outcome: Recommendation = R.GO) -> _Fixed:
    return _Fixed(f"validated.{factor.value}", outcome, factor, ThresholdStatus.VALIDATED)


def _strict_engine(*rules: object) -> RiskRulesEngine:
    policy = EvidencePolicy(frozenset(), timedelta(hours=1))
    return RiskRulesEngine(rules, policy, "test")  # type: ignore[arg-type]  # default factors


def test_a_validated_rule_for_one_factor_cannot_produce_go_while_others_are_unevaluated(
    plan: DivePlan,
) -> None:
    engine = _strict_engine(_validated(K.WIND))  # no placeholders, nothing else covered
    result = engine.assess(plan, [], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    uncovered = next(
        r for r in result.rule_results if r.rule_id == "ruleset.threshold_factors_uncovered"
    )
    assert "wave_height" in uncovered.rationale and "wind" not in uncovered.rationale


def test_caution_is_held_to_the_same_coverage_rule(plan: DivePlan) -> None:
    engine = _strict_engine(_validated(K.WIND, R.CAUTION))
    assert engine.assess(plan, [], NOW).recommendation == R.INSUFFICIENT_EVIDENCE


def test_go_is_possible_once_every_threshold_factor_has_a_validated_rule(plan: DivePlan) -> None:
    rules = [_validated(f) for f in sorted(THRESHOLD_FACTORS, key=lambda k: k.value)]
    result = _strict_engine(*rules).assess(plan, [], NOW)
    assert result.unevaluated_factors == ()
    assert result.recommendation == R.GO


def test_a_no_go_is_never_softened_by_the_coverage_rule(plan: DivePlan) -> None:
    result = _strict_engine(_validated(K.WIND, R.NO_GO)).assess(plan, [], NOW)
    assert result.recommendation == R.NO_GO


def test_a_go_from_a_rule_that_declares_no_factor_covers_nothing(plan: DivePlan) -> None:
    result = _strict_engine(_Fixed("anonymous", R.GO)).assess(plan, [], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("factor", sorted(THRESHOLD_FACTORS, key=lambda k: k.value))
@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION])
def test_a_threshold_factor_cannot_support_go_or_caution_by_omitting_its_status(
    factor: RiskFactorKind, outcome: Recommendation
) -> None:
    with pytest.raises(ValidationError):
        RuleResult(rule_id="r", outcome=outcome, rationale="x", factor=factor)
    RuleResult(
        rule_id="r",
        outcome=outcome,
        rationale="x",
        factor=factor,
        threshold_status=ThresholdStatus.VALIDATED,
    )


def test_the_production_engine_cannot_return_go_or_caution_with_perfect_evidence() -> None:
    from divesafe.api.state import INTERIM_REQUIRED_CATEGORIES, build_engine
    from divesafe.config import Settings

    engine = build_engine(Settings(evidence_max_age_minutes=60))
    assert engine is not None
    everything = [make_evidence(c) for c in DataCategory]
    plan = DivePlan(
        site_id="s",
        planned_start=NOW + timedelta(hours=1),
        planned_duration_minutes=30,
        max_depth_m=10,
    )
    result = engine.assess(plan, everything, NOW)
    assert {e.category for e in everything} >= INTERIM_REQUIRED_CATEGORIES
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert set(result.unevaluated_factors) == set(THRESHOLD_FACTORS)


def test_the_interim_note_says_more_data_will_not_help() -> None:
    from divesafe.api.schemas import AssessmentView
    from divesafe.api.state import build_engine
    from divesafe.config import Settings

    engine = build_engine(Settings(evidence_max_age_minutes=60))
    note = AssessmentView.of(run_pipeline(None, risk_engine=engine)).outcome_note
    assert "no dive can currently return GO" in note and "More data alone" in note
