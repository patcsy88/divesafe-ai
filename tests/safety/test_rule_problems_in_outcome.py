"""A rule that failed, expired or declares no expiry is named in the outcome note, in fixed words.

See "Reserved rule ids" in docs/risk-model.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from divesafe.api.schemas import AssessmentView
from divesafe.domain import (
    AssessmentRecord,
    DivePlan,
    RuleResult,
    ThresholdStatus,
)
from divesafe.domain import (
    Recommendation as R,
)
from divesafe.risk.engine import (
    RULE_ERROR_PREFIX,
    RULE_EXPIRED_PREFIX,
    RULE_NO_EXPIRY_PREFIX,
    RULE_PROBLEM_PREFIXES,
    RULE_PROBLEM_WORDING,
    EvidencePolicy,
    RiskRulesEngine,
)

pytestmark = pytest.mark.safety

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
PLAN = DivePlan(
    site_id="s", planned_start=NOW + timedelta(hours=2), planned_duration_minutes=30, max_depth_m=10
)


class _Rule:
    citation = "test fixture (synthetic)"

    def __init__(self, rule_id: str, result: RuleResult | None, expires: datetime | None) -> None:
        self.rule_id = rule_id
        self._result = result
        if expires is not None:
            self.expires_at = expires

    def evaluate(self, plan: object, evidence: object) -> RuleResult | None:
        if self._result is None:
            raise RuntimeError("secret internal detail")
        return self._result


def _view(rule: _Rule) -> AssessmentView:
    engine = RiskRulesEngine(
        [rule], EvidencePolicy(frozenset(), timedelta(hours=1)), "v", required_factors=frozenset()
    )
    assessment = engine.assess(PLAN, [], NOW)
    record = AssessmentRecord(
        id="a" * 32,
        created_at=NOW,
        plan=PLAN,
        evidence=(),
        rule_results=assessment.rule_results,
        deterministic_recommendation=assessment.recommendation,
        proposed_recommendation=None,
        final_recommendation=assessment.recommendation,
        llm_attempted_downgrade=False,
        ruleset_version="v",
    )
    return AssessmentView.of(record)


def test_a_rule_that_failed_is_named_without_its_internal_text() -> None:
    view = _view(_Rule("synthetic.broken", None, None))
    assert "Rule problems: Rule synthetic.broken: failed to evaluate" in view.outcome_note
    assert "secret internal detail" not in view.outcome_note
    assert view.record.final_recommendation is R.INSUFFICIENT_EVIDENCE


def test_a_rule_whose_review_expired_is_named() -> None:
    rule = _Rule("synthetic.old", None, NOW - timedelta(days=1))
    view = _view(rule)
    assert "Rule synthetic.old: its signed-off review has expired" in view.outcome_note


def test_a_validated_rule_with_no_expiry_is_named() -> None:
    result = RuleResult(
        rule_id="synthetic.forever",
        outcome=R.GO,
        rationale="ok",
        threshold_status=ThresholdStatus.VALIDATED,
    )
    view = _view(_Rule("synthetic.forever", result, None))
    assert "Rule synthetic.forever: is marked validated but declares no expiry" in view.outcome_note
    assert view.record.final_recommendation is R.INSUFFICIENT_EVIDENCE


def test_a_clean_run_lists_no_rule_problems() -> None:
    result = RuleResult(rule_id="synthetic.fine", outcome=R.NO_GO, rationale="hard stop")
    view = _view(_Rule("synthetic.fine", result, None))
    assert "Rule problems" not in view.outcome_note
    assert "Evidence problems" not in view.outcome_note


def test_the_three_problem_prefixes_are_the_ones_the_engine_emits() -> None:
    assert RULE_PROBLEM_PREFIXES == (RULE_ERROR_PREFIX, RULE_EXPIRED_PREFIX, RULE_NO_EXPIRY_PREFIX)
    assert all(p.startswith("rule.") and p.count(".") == 2 for p in RULE_PROBLEM_PREFIXES)
    assert set(RULE_PROBLEM_WORDING) == set(RULE_PROBLEM_PREFIXES)


@pytest.mark.parametrize("prefix", RULE_PROBLEM_PREFIXES)
def test_a_rule_cannot_be_registered_under_a_problem_prefix(prefix: str) -> None:
    rule = _Rule(f"{prefix}x", None, None)
    with pytest.raises(ValueError, match="reserved prefix"):
        RiskRulesEngine([rule], EvidencePolicy(frozenset(), timedelta(hours=1)), "v")


@pytest.mark.parametrize(
    "posed",
    ["assessment.window_started", "evidence.required.waves", "evidence.quality", "ruleset.x"]
    + [f"{p}synthetic.victim" for p in RULE_PROBLEM_PREFIXES],
)
def test_a_result_posing_under_any_reserved_id_is_replaced_and_never_softens(posed: str) -> None:
    hard_stop = RuleResult(rule_id=posed, outcome=R.NO_GO, rationale="hard stop")
    view = _view(_Rule("synthetic.poser", hard_stop, None))
    ids = [r.rule_id for r in view.record.rule_results]
    assert posed not in ids and f"{RULE_ERROR_PREFIX}synthetic.poser" in ids
    assert view.record.final_recommendation is R.NO_GO
    # A kept NO-GO must not read as the work of an ignored rule.
    assert "not applied" not in view.outcome_note
    assert "any more severe outcome it gave still applies" in view.outcome_note

    friendly = RuleResult(rule_id=posed, outcome=R.GO, rationale="Inside the signed-off GO range.")
    view = _view(_Rule("synthetic.poser", friendly, None))
    assert posed not in [r.rule_id for r in view.record.rule_results]
    assert view.record.final_recommendation is R.INSUFFICIENT_EVIDENCE
    assert "Rule synthetic.poser: failed to evaluate" in view.outcome_note
    # The rejected rule's own words stay in the audit trail, never in the note a person reads.
    assert "GO range" not in view.outcome_note and "Its own rationale" not in view.outcome_note
    assert any("GO range" in r.rationale for r in view.record.rule_results)


def test_a_rule_id_read_twice_is_checked_and_kept_as_the_same_value() -> None:
    class _Shifty:
        citation = "test fixture (synthetic)"
        reads = 0

        @property
        def rule_id(self) -> str:
            _Shifty.reads += 1
            return "synthetic.fine" if _Shifty.reads == 1 else "evidence.required.waves"

        def evaluate(self, plan: object, evidence: object) -> None:
            return None

    policy = EvidencePolicy(frozenset(), timedelta(hours=1))
    engine = RiskRulesEngine([_Shifty()], policy, "v", required_factors=frozenset())  # type: ignore[list-item]
    assert engine._rule_ids == ("synthetic.fine",)
    assert _Shifty.reads == 1


@pytest.mark.parametrize(
    "bad", ["Rule.error.x", "RULE.expired.x", "Evidence.required.x", "a b", "a\nGO", "", "x" * 129]
)
def test_a_rule_id_with_odd_characters_or_a_reserved_prefix_in_any_case_is_refused(
    bad: str,
) -> None:
    rule = _Rule(bad, None, None)
    with pytest.raises(ValueError, match="reserved prefix|rule id must be"):
        RiskRulesEngine([rule], EvidencePolicy(frozenset(), timedelta(hours=1)), "v")


def test_the_real_rule_families_still_register() -> None:
    policy = EvidencePolicy(frozenset(), timedelta(hours=1))
    for ok in ("definition.wave-limit", "definition-scope.waves", "policy.marine", "synthetic.a_b"):
        RiskRulesEngine([_Rule(ok, None, None)], policy, "v")


def test_the_same_rule_problem_found_twice_is_listed_once() -> None:
    view = _view(_Rule("synthetic.broken", None, None))
    twice = view.record.model_copy(
        update={"rule_results": view.record.rule_results + view.record.rule_results}
    )
    note = AssessmentView.of(twice).outcome_note
    assert note.count("Rule synthetic.broken: failed to evaluate") == 1


def test_a_signed_off_definition_id_of_the_longest_allowed_length_still_registers() -> None:
    rule = _Rule("definition." + "a" * 100, None, None)
    RiskRulesEngine([rule], EvidencePolicy(frozenset(), timedelta(hours=1)), "v")
