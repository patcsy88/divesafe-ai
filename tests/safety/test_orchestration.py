"""End-to-end pipeline and human-gate behaviour (no LLM involved)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from divesafe.data import (
    TIOMAN_ISLAND,
    ConnectorTransportError,
    DataGovMyWarningConnector,
    OpenMeteoMarineConnector,
)
from divesafe.domain import (
    ActualConditions,
    AssessmentRecord,
    DataCategory,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RuleResult,
)
from divesafe.orchestration import (
    AlreadyDecidedError,
    DecisionRequiredError,
    InvalidPlanError,
    UnsafeConfigurationError,
    assess_dive,
    decide,
    report_actual_conditions,
)
from divesafe.risk import EvidencePolicy, RiskRulesEngine, WarningNeedsHumanReadingRule

pytestmark = pytest.mark.safety

R = Recommendation
FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 10, 8, 17, 11, tzinfo=UTC)
START = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)
PLAN = DivePlan(
    site_id=TIOMAN_ISLAND.id, planned_start=START, planned_duration_minutes=180, max_depth_m=18
)
LIVE = frozenset(
    {
        DataCategory.WAVES_SWELL,
        DataCategory.CURRENTS,
        DataCategory.SEA_TEMPERATURE,
        DataCategory.MARINE_WARNINGS,
    }
)
MARINE = json.loads((FIXTURES / "open_meteo_marine_tioman_recorded_2026-10-08.json").read_text())
WARNINGS = json.loads((FIXTURES / "data_gov_my_warning_recorded_2026-10-09.json").read_text())


class _Getter:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _GoRule:
    rule_id = "synthetic.go"
    citation = "test fixture (synthetic, not a real safety source)"

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        return RuleResult(rule_id=self.rule_id, outcome=R.GO, rationale="synthetic")


def _engine(
    *, with_warning_rule: bool, required: frozenset[DataCategory] = LIVE
) -> RiskRulesEngine:
    rules: list[Any] = [_GoRule()]
    if with_warning_rule:
        rules.append(WarningNeedsHumanReadingRule())
    return RiskRulesEngine(
        rules,
        EvidencePolicy(required, timedelta(hours=1), degraded_may_support_go=True),  # synthetic
        "synthetic",
        required_factors=frozenset(),
    )


def _run(marine: Any = MARINE, warnings: Any = WARNINGS, **kwargs: Any) -> AssessmentRecord:
    connectors = [
        OpenMeteoMarineConnector(_Getter(marine)),
        DataGovMyWarningConnector(_Getter(warnings)),
    ]
    options: dict[str, Any] = {
        "assessment_id": "a-1",
        "plan": PLAN,
        "site": TIOMAN_ISLAND,
        "connectors": connectors,
        "engine": _engine(with_warning_rule=False),
        "now": NOW,
    }
    options.update(kwargs)
    return asyncio.run(assess_dive(**options))


# --- pipeline ------------------------------------------------------------------------------


def test_complete_clean_run_is_pending_human_with_no_confidence_invented() -> None:
    record = _run(warnings=[])
    assert record.final_recommendation == R.GO  # only because the synthetic rule says so
    assert record.status == "PENDING_HUMAN"
    assert record.confidence is None
    assert record.evidence_issues == ()
    assert record.data_versions == {
        "open-meteo-marine": "v1",
        "data-gov-my-weather-warning": "weather-api",
    }


def test_failed_source_still_returns_a_record_with_insufficient_evidence_and_the_reason() -> None:
    record = _run(warnings=ConnectorTransportError("HTTP 503"))
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert record.status == "PENDING_HUMAN"
    assert any("data-gov-my-weather-warning" in i for i in record.evidence_issues)
    assert any(r.rule_id == "evidence.connector_issues" for r in record.rule_results)


def test_a_failure_in_an_unrequired_category_still_blocks_go() -> None:
    broken = json.loads(json.dumps(MARINE))
    broken["hourly"]["ocean_current_velocity"][1] = None
    engine = _engine(with_warning_rule=False, required=frozenset({DataCategory.WAVES_SWELL}))
    record = _run(marine=broken, warnings=[], engine=engine)
    assert DataCategory.WAVES_SWELL in {e.category for e in record.evidence}
    assert record.evidence_issues
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE


def test_real_recorded_warnings_force_a_human_to_read_them() -> None:
    record = _run(engine=_engine(with_warning_rule=True))
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    hit = next(r for r in record.rule_results if r.rule_id.startswith("policy.marine_warning"))
    assert hit.evidence_ids


def test_an_llm_proposal_cannot_relax_the_pipeline_result() -> None:
    record = _run(engine=_engine(with_warning_rule=True), proposed=R.GO)
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert record.llm_attempted_downgrade is True
    assert record.proposed_recommendation == R.GO


def test_pipeline_refuses_a_policy_with_no_required_evidence() -> None:
    engine = _engine(with_warning_rule=False, required=frozenset())
    with pytest.raises(UnsafeConfigurationError):
        _run(engine=engine)


def test_plan_for_a_different_site_is_refused() -> None:
    other = PLAN.model_copy(update={"site_id": "elsewhere"})
    with pytest.raises(InvalidPlanError):
        _run(plan=other)


# --- human gate ----------------------------------------------------------------------------


def _pending() -> AssessmentRecord:
    return _run(
        engine=_engine(with_warning_rule=True), assigned_decider="leader"
    )  # INSUFFICIENT EVIDENCE


def test_accepting_the_recommendation_is_not_an_override() -> None:
    decided = decide(
        _pending(), decided_by="leader", decision=R.INSUFFICIENT_EVIDENCE, decided_at=NOW
    )
    assert decided.status == "DECIDED"
    assert decided.human_decision is not None and decided.human_decision.is_override is False


def test_override_flag_is_derived_not_supplied() -> None:
    decided = decide(
        _pending(), decided_by="leader", decision=R.GO, decided_at=NOW, rationale="Read warning."
    )
    assert decided.human_decision is not None and decided.human_decision.is_override is True
    assert decided.overrides_to_less_severe is True
    assert decided.final_recommendation == R.INSUFFICIENT_EVIDENCE  # system result untouched


def test_override_without_rationale_is_rejected() -> None:
    with pytest.raises(ValidationError):
        decide(_pending(), decided_by="leader", decision=R.GO, decided_at=NOW)


def test_a_recorded_decision_cannot_be_replaced() -> None:
    decided = decide(
        _pending(),
        decided_by="leader",
        decision=R.NO_GO,
        decided_at=NOW,
        rationale="Poor visibility.",
    )
    with pytest.raises(AlreadyDecidedError):
        decide(
            decided, decided_by="leader", decision=R.GO, decided_at=NOW, rationale="Changed mind."
        )


def test_original_record_is_not_mutated_by_deciding() -> None:
    pending = _pending()
    decide(pending, decided_by="leader", decision=R.NO_GO, decided_at=NOW, rationale="x")
    assert pending.human_decision is None


def test_actual_conditions_need_a_decision_and_can_be_reported_once() -> None:
    actual = ActualConditions(
        reported_at=START + timedelta(hours=1), reported_by="leader", observations={"note": "calm"}
    )
    with pytest.raises(DecisionRequiredError, match="require a recorded human decision"):
        report_actual_conditions(_pending(), actual=actual)
    decided = decide(
        _pending(), decided_by="leader", decision=R.INSUFFICIENT_EVIDENCE, decided_at=NOW
    )
    done = report_actual_conditions(decided, actual=actual)
    assert done.actual_conditions == actual
    with pytest.raises(AlreadyDecidedError):
        report_actual_conditions(done, actual=actual)


def test_every_pipeline_record_survives_a_serialization_round_trip() -> None:
    record = _pending()
    again = AssessmentRecord.model_validate_json(record.model_dump_json())
    assert again == record


# --- review follow-ups ---------------------------------------------------------------------


def test_record_with_connector_issues_cannot_claim_go() -> None:
    good = _run(warnings=ConnectorTransportError("HTTP 503"))
    tampered = good.model_dump()
    tampered.update(
        rule_results=[
            {"rule_id": "synthetic.go", "outcome": "GO", "rationale": "x", "evidence_ids": []}
        ],
        deterministic_recommendation="GO",
        final_recommendation="GO",
    )
    with pytest.raises(ValidationError):
        AssessmentRecord.model_validate(tampered)


def test_connector_issues_do_not_soften_a_no_go() -> None:
    class _NoGo:
        rule_id = "synthetic.nogo"
        citation = "test fixture (synthetic, not a real safety source)"

        def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
            return RuleResult(rule_id=self.rule_id, outcome=R.NO_GO, rationale="synthetic")

    engine = RiskRulesEngine(
        [_NoGo()],
        EvidencePolicy(LIVE, timedelta(hours=1)),
        "synthetic",
        required_factors=frozenset(),
    )
    record = _run(warnings=ConnectorTransportError("HTTP 503"), engine=engine)
    assert record.final_recommendation == R.NO_GO


def test_zero_connectors_yields_insufficient_evidence_not_an_error() -> None:
    record = _run(connectors=[])
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert record.evidence == ()


def test_a_dive_window_in_the_past_is_refused() -> None:
    past = PLAN.model_copy(update={"planned_start": NOW - timedelta(hours=1)})
    with pytest.raises(InvalidPlanError):
        _run(plan=past)


def test_decision_cannot_predate_the_assessment() -> None:
    with pytest.raises(ValidationError):
        decide(
            _pending(),
            decided_by="leader",
            decision=R.INSUFFICIENT_EVIDENCE,
            decided_at=NOW - timedelta(minutes=1),
        )


def test_actual_conditions_cannot_predate_the_decision() -> None:
    decided = decide(
        _pending(), decided_by="leader", decision=R.INSUFFICIENT_EVIDENCE, decided_at=NOW
    )
    early = ActualConditions(
        reported_at=NOW - timedelta(hours=1), reported_by="leader", observations={}
    )
    with pytest.raises(ValidationError):
        report_actual_conditions(decided, actual=early)


def test_gate_round_trip_preserves_types_and_data() -> None:
    decided = decide(
        _pending(),
        decided_by="leader",
        decision=R.GO,
        decided_at=NOW,
        rationale="Read the warning.",
    )
    assert isinstance(decided.evidence, tuple) and isinstance(decided.rule_results, tuple)
    assert decided.evidence == _pending().evidence
    assert isinstance(decided.final_recommendation, Recommendation)
    assert AssessmentRecord.model_validate_json(decided.model_dump_json()) == decided


def test_actual_conditions_cannot_predate_the_planned_dive() -> None:
    decided = decide(
        _pending(), decided_by="leader", decision=R.INSUFFICIENT_EVIDENCE, decided_at=NOW
    )
    before_dive = ActualConditions(
        reported_at=NOW + timedelta(minutes=5), reported_by="leader", observations={}
    )  # after the decision (17:11) but before the 18:00 planned start
    with pytest.raises(ValidationError):
        report_actual_conditions(decided, actual=before_dive)
