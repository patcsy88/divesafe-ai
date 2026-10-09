"""Evidence usability: window coverage, effective quality and the guards (ADR 0008).

The engine is exercised on a dive window in the past relative to retrieval, because a measured
observation may only describe time up to when it was retrieved.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.agent.scripted_llm import run_pipeline
from tests.conftest import NOW

from divesafe.domain import (
    RANGE_CHECK,
    UNIT_CHECK,
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RuleResult,
    TransformationStep,
    effective_quality,
)
from divesafe.risk import EvidencePolicy, RiskRulesEngine, Rule
from divesafe.risk.engine import DEFAULT_ACCEPTED_QUALITY

pytestmark = pytest.mark.safety
R = Recommendation
C = DataCategory
H = timedelta(hours=1)
START = NOW - 3 * H
END = START + H
PLAN = DivePlan(site_id="s", planned_start=START, planned_duration_minutes=60, max_depth_m=10)
CHECKS = (TransformationStep(step=UNIT_CHECK), TransformationStep(step=RANGE_CHECK))
OBS, MODEL, NOTICE, KNOWLEDGE = (
    DataKind.OBSERVATION,
    DataKind.MODEL,
    DataKind.NOTICE,
    DataKind.KNOWLEDGE,
)


class _Go:
    rule_id = "synthetic.go"
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self, cites: tuple[str, ...] = ()) -> None:
        self._cites = cites

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        return RuleResult(
            rule_id=self.rule_id, outcome=R.GO, rationale="synthetic", evidence_ids=self._cites
        )


def _item(
    *,
    category: DataCategory = C.WAVES_SWELL,
    kind: DataKind = OBS,
    quality: DataQuality = DataQuality.VALIDATED,
    valid_at: datetime = START,
    valid_until: datetime | None = END,
    notes: tuple[str, ...] = (),
    steps: tuple[TransformationStep, ...] = CHECKS,
    age: timedelta = timedelta(minutes=5),
    item_id: str = "e1",
) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        category=category,
        source="synthetic-test",
        retrieved_at=NOW - age,
        valid_at=valid_at,
        valid_until=valid_until,
        is_forecast=kind in (DataKind.MODEL, DataKind.FORECAST, DataKind.PREDICTION),
        data_kind=kind,
        quality=quality,
        quality_notes=notes,
        transformations=steps,
        value={},
    )


def _engine(
    required: set[DataCategory] | None = None,
    *,
    rules: Sequence[Rule] | None = None,
    accepted_quality: frozenset[DataQuality] = DEFAULT_ACCEPTED_QUALITY,
    require_window_coverage: bool = True,
    degraded_may_support_go: bool = False,
) -> RiskRulesEngine:
    policy = EvidencePolicy(
        frozenset(required or {C.WAVES_SWELL}),
        H,
        accepted_quality=accepted_quality,
        require_window_coverage=require_window_coverage,
        degraded_may_support_go=degraded_may_support_go,
    )
    return RiskRulesEngine(
        rules or [_Go()],
        policy,
        "synthetic",
        required_factors=frozenset(),
    )


def _verdict(
    items: Sequence[EvidenceItem],
    *,
    require_window_coverage: bool = True,
    degraded_may_support_go: bool = False,
) -> Recommendation:
    engine = _engine(
        require_window_coverage=require_window_coverage,
        degraded_may_support_go=degraded_may_support_go,
    )
    return engine.assess(PLAN, items, NOW).recommendation


def _reasons(items: Sequence[EvidenceItem]) -> str:
    result = _engine().assess(PLAN, items, NOW)
    return " | ".join(r.rationale for r in result.rule_results)


# --- window coverage ------------------------------------------------------------------------


def test_an_interval_covering_the_window_is_usable() -> None:
    assert _verdict([_item()]) == R.GO


def test_a_chain_of_touching_intervals_is_usable() -> None:
    chain = [
        _item(valid_at=START - H, valid_until=START + 30 * timedelta(minutes=1), item_id="a"),
        _item(valid_at=START + timedelta(minutes=30), valid_until=END + H, item_id="b"),
    ]
    assert _verdict(chain) == R.GO


def test_intervals_with_a_gap_inside_the_window_are_not_sufficient() -> None:
    gap = [
        _item(valid_at=START - H, valid_until=START + timedelta(minutes=20), item_id="a"),
        _item(valid_at=START + timedelta(minutes=40), valid_until=END + H, item_id="b"),
    ]
    assert _verdict(gap) == R.INSUFFICIENT_EVIDENCE
    assert "does not cover" in _reasons(gap)


def test_a_series_of_instants_is_only_bracketed_and_that_limit_is_explicit() -> None:
    """Documented residual (ADR 0008): a hole inside instants is not detected by the engine."""
    instants = [
        _item(valid_at=START - H, valid_until=None, item_id="a"),
        _item(valid_at=END + H - timedelta(minutes=10), valid_until=None, item_id="b"),
    ]
    assert _verdict(instants) == R.GO


@pytest.mark.parametrize(
    ("valid_at", "valid_until"),
    [
        (START + timedelta(minutes=1), END + H / 2),  # starts after the dive starts
        (START - 2 * H, END - timedelta(minutes=1)),  # ends before the dive ends
        (START - 3 * H, None),  # a single instant long before
        (END + timedelta(minutes=1), END + H),  # entirely after
        (START - 3 * H, START - H),  # entirely before
    ],
)
def test_evidence_that_does_not_cover_the_window_is_not_sufficient(
    valid_at: datetime, valid_until: datetime | None
) -> None:
    item = _item(valid_at=valid_at, valid_until=valid_until)
    assert _verdict([item]) == R.INSUFFICIENT_EVIDENCE
    assert "does not cover the dive window" in _reasons([item])


def test_exact_boundaries_count_as_covering_and_one_second_short_does_not() -> None:
    assert _verdict([_item(valid_at=START, valid_until=END)]) == R.GO
    late = _item(valid_at=START + timedelta(seconds=1), valid_until=END)
    early = _item(valid_at=START, valid_until=END - timedelta(seconds=1))
    assert _verdict([late]) == R.INSUFFICIENT_EVIDENCE
    assert _verdict([early]) == R.INSUFFICIENT_EVIDENCE


def test_a_fresh_fetch_of_out_of_window_data_is_still_insufficient() -> None:
    just_fetched = _item(valid_at=END + timedelta(minutes=1), valid_until=None, age=timedelta(0))
    assert _verdict([just_fetched]) == R.INSUFFICIENT_EVIDENCE


def test_window_coverage_can_be_disabled_only_explicitly() -> None:
    far = [_item(valid_at=START - 3 * H, valid_until=None)]
    assert _verdict(far, require_window_coverage=False) == R.GO
    assert _verdict(far) == R.INSUFFICIENT_EVIDENCE


# --- the exemption is an allow-list ---------------------------------------------------------


def _warning(valid_until: datetime | None, valid_at: datetime = NOW - 10 * H) -> EvidenceItem:
    return _item(
        category=C.MARINE_WARNINGS,
        kind=NOTICE,
        quality=DataQuality.DEGRADED,
        valid_at=valid_at,
        valid_until=valid_until,
        item_id="w",
    )


def test_a_current_notice_satisfies_marine_warnings_without_spanning_the_window() -> None:
    engine = _engine({C.MARINE_WARNINGS}, degraded_may_support_go=True)
    assert engine.assess(PLAN, [_warning(valid_until=None)], NOW).recommendation == R.GO


def test_a_notice_that_ended_before_the_dive_does_not_count() -> None:
    expired = _warning(valid_until=START - timedelta(minutes=1))
    engine = _engine({C.MARINE_WARNINGS}, degraded_may_support_go=True)
    assert engine.assess(PLAN, [expired], NOW).recommendation == R.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize("kind", [NOTICE, KNOWLEDGE])
@pytest.mark.parametrize("category", [C.WAVES_SWELL, C.CURRENTS, C.WIND, C.TIDES, C.WEATHER])
def test_live_condition_data_cannot_dodge_coverage_by_being_labelled_a_notice(
    kind: DataKind, category: DataCategory
) -> None:
    mislabelled = _item(
        category=category,
        kind=kind,
        quality=DataQuality.DEGRADED,
        valid_at=START - 3 * H,
        valid_until=None,
    )
    engine = _engine({category}, degraded_may_support_go=True)
    assert engine.assess(PLAN, [mislabelled], NOW).recommendation == R.INSUFFICIENT_EVIDENCE


def test_knowledge_satisfies_only_the_knowledge_categories() -> None:
    guidance = _item(
        category=C.LOCAL_GUIDANCE,
        kind=KNOWLEDGE,
        quality=DataQuality.DEGRADED,
        valid_at=NOW - 10 * H,
        valid_until=None,
    )
    engine = _engine({C.LOCAL_GUIDANCE}, degraded_may_support_go=True)
    assert engine.assess(PLAN, [guidance], NOW).recommendation == R.GO


# --- effective quality ----------------------------------------------------------------------


def test_a_measured_observation_with_both_checks_earns_validated() -> None:
    assert effective_quality(_item()) == DataQuality.VALIDATED


@pytest.mark.parametrize(
    "kind", [DataKind.MODEL, DataKind.FORECAST, DataKind.PREDICTION, NOTICE, KNOWLEDGE]
)
def test_only_a_measurement_can_be_validated_whatever_the_connector_claims(kind: DataKind) -> None:
    claimed = _item(kind=kind, quality=DataQuality.VALIDATED)
    assert effective_quality(claimed) == DataQuality.DEGRADED


@pytest.mark.parametrize(
    "steps",
    [(), (TransformationStep(step=UNIT_CHECK),), (TransformationStep(step=RANGE_CHECK),)],
)
def test_a_validated_claim_without_both_checks_is_capped(
    steps: tuple[TransformationStep, ...],
) -> None:
    assert effective_quality(_item(steps=steps)) == DataQuality.DEGRADED


def test_known_limitations_cancel_a_validated_claim() -> None:
    assert effective_quality(_item(notes=("coarse grid",))) == DataQuality.DEGRADED


def test_lower_claims_are_never_raised() -> None:
    assert effective_quality(_item(quality=DataQuality.UNASSESSED)) == DataQuality.UNASSESSED
    assert effective_quality(_item(quality=DataQuality.DEGRADED)) == DataQuality.DEGRADED


def test_unassessed_evidence_does_not_satisfy_a_required_category() -> None:
    unassessed = [_item(quality=DataQuality.UNASSESSED)]
    assert _verdict(unassessed) == R.INSUFFICIENT_EVIDENCE
    assert "not of an accepted quality" in _reasons(unassessed)


def test_a_falsely_validated_model_cannot_unlock_go() -> None:
    liar = _item(kind=MODEL, quality=DataQuality.VALIDATED)
    assert _verdict([liar]) == R.INSUFFICIENT_EVIDENCE


def test_the_real_connector_check_names_are_the_shared_constants() -> None:
    record = run_pipeline(None)
    marine = next(e for e in record.evidence if e.category == C.WAVES_SWELL)
    assert {UNIT_CHECK, RANGE_CHECK} <= {t.step for t in marine.transformations}
    assert effective_quality(marine) == DataQuality.DEGRADED  # a model can never be validated


def test_an_observation_cannot_claim_to_be_valid_after_it_was_retrieved() -> None:
    with pytest.raises(ValidationError):
        _item(valid_at=NOW, valid_until=NOW + H)  # retrieved 5 minutes before NOW
    with pytest.raises(ValidationError):
        _item(valid_at=NOW + H, valid_until=None)
    _item(valid_at=NOW - H, valid_until=NOW - timedelta(minutes=5))  # exactly up to retrieval
    _item(kind=MODEL, valid_at=NOW, valid_until=NOW + H)  # forecasts and models may look ahead


# --- degraded data and GO -------------------------------------------------------------------


def _degraded(
    item_id: str = "d",
    *,
    category: DataCategory = C.WAVES_SWELL,
    valid_at: datetime = START,
    valid_until: datetime | None = END,
) -> EvidenceItem:
    return _item(
        kind=MODEL,
        quality=DataQuality.DEGRADED,
        item_id=item_id,
        category=category,
        valid_at=valid_at,
        valid_until=valid_until,
    )


def test_degraded_evidence_is_sufficient_but_cannot_support_go_by_default() -> None:
    result = _engine().assess(PLAN, [_degraded()], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    ids = [r.rule_id for r in result.rule_results]
    assert "evidence.quality_insufficient_for_go" in ids
    assert not any(i.startswith("evidence.required") for i in ids)  # it WAS sufficient


def test_the_policy_can_explicitly_allow_degraded_evidence_to_support_go() -> None:
    assert _verdict([_degraded()], degraded_may_support_go=True) == R.GO


def test_a_validated_item_covering_only_part_of_the_window_cannot_launder_degraded_cover() -> None:
    """Pooled coverage passes (validated + degraded chain across the window) but the validated
    evidence alone covers only the first ten minutes."""
    brief = timedelta(minutes=10)
    validated_start = _item(valid_at=START - H, valid_until=START + brief, item_id="v")
    degraded_rest = _degraded(valid_at=START + brief, valid_until=END + H)
    result = _engine().assess(PLAN, [validated_start, degraded_rest], NOW)
    ids = [r.rule_id for r in result.rule_results]
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert "evidence.quality_insufficient_for_go" in ids
    assert not any(i.startswith("evidence.required") for i in ids)  # pooled coverage was fine


def test_a_validated_item_at_the_wrong_hour_is_no_help() -> None:
    wrong_hour = _item(valid_at=START - 3 * H, valid_until=START - 2 * H, item_id="v")
    covering_but_degraded = _degraded(valid_at=START - H, valid_until=END + H)
    assert _verdict([wrong_hour, covering_but_degraded]) == R.INSUFFICIENT_EVIDENCE


def test_validated_evidence_that_covers_the_window_supports_go() -> None:
    assert _verdict([_degraded(), _item(item_id="v")]) == R.GO


def test_the_quality_guard_names_each_degraded_category() -> None:
    engine = _engine({C.WAVES_SWELL, C.CURRENTS})
    items = [
        _item(category=C.WAVES_SWELL, item_id="w"),
        _degraded("c", category=C.CURRENTS),
    ]
    guard = next(
        r
        for r in engine.assess(PLAN, items, NOW).rule_results
        if r.rule_id.startswith("evidence.quality")
    )
    assert "currents" in guard.rationale and "waves_swell" not in guard.rationale


def test_the_quality_guard_never_softens_a_no_go() -> None:
    class _NoGo(_Go):
        def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
            return RuleResult(rule_id="synthetic.nogo", outcome=R.NO_GO, rationale="x")

    engine = _engine(rules=[_NoGo()])
    assert engine.assess(PLAN, [_degraded()], NOW).recommendation == R.NO_GO


def test_rejected_quality_can_never_be_accepted_by_a_policy() -> None:
    with pytest.raises(ValueError):
        EvidencePolicy(frozenset({C.WIND}), H, accepted_quality=frozenset({DataQuality.REJECTED}))


# --- a GO may not cite evidence the engine judged unusable ---------------------------------


def test_a_go_rule_citing_stale_evidence_is_downgraded() -> None:
    stale = _item(age=2 * H, item_id="stale")
    fresh = _item(item_id="fresh")
    engine = _engine(rules=[_Go(cites=("stale",))])
    result = engine.assess(PLAN, [stale, fresh], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id.startswith("rule.cites_unusable_evidence") for r in result.rule_results)


def test_a_go_rule_citing_unassessed_evidence_is_downgraded() -> None:
    bad = _item(quality=DataQuality.UNASSESSED, item_id="bad")
    fresh = _item(item_id="fresh")
    engine = _engine(rules=[_Go(cites=("bad",))])
    assert engine.assess(PLAN, [bad, fresh], NOW).recommendation == R.INSUFFICIENT_EVIDENCE


def test_a_go_rule_citing_usable_evidence_is_not_affected() -> None:
    engine = _engine(rules=[_Go(cites=("fresh",))])
    assert engine.assess(PLAN, [_item(item_id="fresh")], NOW).recommendation == R.GO


# --- production wiring ---------------------------------------------------------------------


def test_the_production_engine_is_conservative_by_default() -> None:
    from divesafe.api.state import build_engine
    from divesafe.config import Settings

    engine = build_engine(Settings(evidence_max_age_minutes=60))
    assert engine is not None
    policy = engine.policy
    assert policy.degraded_may_support_go is False
    assert policy.require_window_coverage is True
    assert DataQuality.UNASSESSED not in policy.accepted_quality


def test_test_only_opt_outs_never_appear_in_production_code() -> None:
    """Flags keyword uses of `degraded_may_support_go=True`, `require_window_coverage=False` and
    `required_factors=frozenset()` under src/. (Positional or computed values are not caught.)"""
    root = Path(__file__).resolve().parents[2] / "src"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.keyword):
                continue
            value = node.value
            weakened = (
                node.arg == "degraded_may_support_go"
                and isinstance(value, ast.Constant)
                and value.value is True
            ) or (
                node.arg == "require_window_coverage"
                and isinstance(value, ast.Constant)
                and value.value is False
            )
            emptied = (
                node.arg == "required_factors"
                and isinstance(value, ast.Call)
                and getattr(value.func, "id", "") == "frozenset"
                and not value.args
            )
            if weakened or emptied:
                offenders.append(f"{path.relative_to(root)}:{value.lineno} {node.arg}")
    assert offenders == []


def test_the_api_explains_degraded_only_evidence_in_the_users_terms() -> None:
    from tests.agent.scripted_llm import engine as synthetic_engine

    from divesafe.api.schemas import AssessmentView

    strict_quality = synthetic_engine(with_warning_rule=False)
    strict = RiskRulesEngine(
        [_Go()],
        EvidencePolicy(strict_quality.policy.required_categories, H),  # default: degraded cannot GO
        "interim-test",
        required_factors=frozenset(),
    )
    note = AssessmentView.of(run_pipeline(None, risk_engine=strict)).outcome_note
    assert "VALIDATED evidence covering the dive window" in note
    assert "Missing or stale" not in note  # no mislabelling


# --- review follow-ups: citation relevance, duplicate ids, unknown citations ----------------


def test_a_go_citing_a_fresh_validated_item_unrelated_to_the_window_is_downgraded() -> None:
    covering = _item(item_id="covering")
    unrelated = _item(valid_at=START - 3 * H, valid_until=START - 2 * H, item_id="unrelated")
    engine = _engine(rules=[_Go(cites=("unrelated",))])
    result = engine.assess(PLAN, [covering, unrelated], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id.startswith("rule.cites_unusable_evidence") for r in result.rule_results)


def test_a_go_citing_an_id_that_is_not_in_the_evidence_is_downgraded() -> None:
    engine = _engine(rules=[_Go(cites=("made-up",))])
    assert engine.assess(PLAN, [_item()], NOW).recommendation == R.INSUFFICIENT_EVIDENCE


def test_citation_relevance_is_not_required_when_coverage_is_disabled() -> None:
    far = _item(valid_at=START - 3 * H, valid_until=START - 2 * H, item_id="far")
    policy = EvidencePolicy(frozenset({C.WAVES_SWELL}), H, require_window_coverage=False)
    engine = RiskRulesEngine(
        [_Go(cites=("far",))], policy, "synthetic", required_factors=frozenset()
    )
    assert engine.assess(PLAN, [far], NOW).recommendation == R.GO


def test_duplicate_evidence_ids_are_reported_as_insufficient_evidence() -> None:
    first = _item(item_id="same")
    second = _item(item_id="same", valid_at=START - H, valid_until=END + H)
    result = _engine().assess(PLAN, [first, second], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id == "evidence.duplicate_ids" for r in result.rule_results)


def test_a_quality_failure_is_tagged_with_the_quality_factor_not_freshness() -> None:
    from divesafe.domain import RiskFactorKind

    unassessed = _item(quality=DataQuality.UNASSESSED)
    stale = _item(age=2 * H)
    factors = {r.rule_id: r.factor for r in _engine().assess(PLAN, [unassessed], NOW).rule_results}
    assert factors["evidence.required.waves_swell"] == RiskFactorKind.DATA_QUALITY
    stale_factors = [r.factor for r in _engine().assess(PLAN, [stale], NOW).rule_results]
    assert stale_factors[0] == RiskFactorKind.DATA_FRESHNESS
