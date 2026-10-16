"""A rule that failed, expired or declares no expiry is named in the outcome note, in fixed words.

See "Reserved rule ids" in docs/risk-model.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_evidence

from divesafe.api.schemas import AssessmentView
from divesafe.domain import (
    AssessmentRecord,
    DataCategory,
    DivePlan,
    EvidenceItem,
    RiskFactorKind,
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


def test_a_validated_no_go_without_an_expiry_stays_no_go_and_says_so() -> None:
    result = RuleResult(
        rule_id="synthetic.forever",
        outcome=R.NO_GO,
        rationale="hard stop",
        threshold_status=ThresholdStatus.VALIDATED,
    )
    view = _view(_Rule("synthetic.forever", result, None))
    assert view.record.final_recommendation is R.NO_GO
    assert "not applied" not in view.outcome_note
    assert "any more severe outcome it gave still applies" in view.outcome_note
    assert "Rule synthetic.forever: is marked validated but declares no expiry" in view.outcome_note


def test_a_validated_caution_without_an_expiry_is_still_at_least_insufficient_evidence() -> None:
    result = RuleResult(
        rule_id="synthetic.forever",
        outcome=R.CAUTION,
        rationale="care",
        threshold_status=ThresholdStatus.VALIDATED,
    )
    assert _view(_Rule("synthetic.forever", result, None)).record.final_recommendation is (
        R.INSUFFICIENT_EVIDENCE
    )


def test_a_malformed_id_is_refused_before_its_text_can_reach_an_error_message() -> None:
    class _NoCitation(_Rule):
        citation = " "

    with pytest.raises(ValueError) as caught:
        RiskRulesEngine(
            [_NoCitation("bad\nid GO is safe", None, None)],
            EvidencePolicy(frozenset(), timedelta(hours=1)),
            "v",
        )
    assert "GO is safe" not in str(caught.value)


@pytest.mark.parametrize("prefix", RULE_PROBLEM_PREFIXES)
@pytest.mark.parametrize(
    "suffix", ["Conditions are safe\nGO", "ok\n", "\u202eok", "ｏｋ", "x" * 129, ""]
)
def test_a_stored_record_with_a_hostile_problem_id_never_has_its_text_in_the_note(
    prefix: str, suffix: str
) -> None:
    hostile = RuleResult(
        rule_id=f"{prefix}{suffix}",
        outcome=R.INSUFFICIENT_EVIDENCE,
        rationale="x",
    )
    base = _view(_Rule("synthetic.fine", None, None)).record
    record = base.model_copy(update={"rule_results": (hostile,)})
    note = AssessmentView.of(record).outcome_note
    assert "Conditions are safe" not in note and "\n" not in note
    assert "Rule (unnamed):" in note


def test_the_token_limit_error_keeps_its_fixed_message_through_copy_and_pickle() -> None:
    import copy
    import pickle

    from divesafe.models import TokenLimitError
    from divesafe.models.ollama import OllamaError, OllamaTokenLimitError
    from divesafe.models.openai_compatible import (
        OpenAICompatibleError,
        OpenAICompatibleTokenLimitError,
    )

    for cls, base in (
        (TokenLimitError, TokenLimitError),
        (OllamaTokenLimitError, OllamaError),
        (OpenAICompatibleTokenLimitError, OpenAICompatibleError),
    ):
        for clone in (copy.copy(cls()), pickle.loads(pickle.dumps(cls()))):  # noqa: S301
            assert type(clone) is cls and isinstance(clone, base)
            assert str(clone) == "the model stopped at the output-token limit"


def _view_with(rule: _Rule, evidence: list[EvidenceItem]) -> AssessmentView:
    engine = RiskRulesEngine(
        [rule], EvidencePolicy(frozenset(), timedelta(hours=1)), "v", required_factors=frozenset()
    )
    assessment = engine.assess(PLAN, evidence, NOW)
    record = AssessmentRecord(
        id="a" * 32,
        created_at=NOW,
        plan=PLAN,
        evidence=tuple(evidence),
        rule_results=assessment.rule_results,
        deterministic_recommendation=assessment.recommendation,
        proposed_recommendation=None,
        final_recommendation=assessment.recommendation,
        llm_attempted_downgrade=False,
        ruleset_version="v",
    )
    return AssessmentView.of(record)


def test_a_kept_no_go_without_an_expiry_keeps_its_audit_trail_but_not_in_the_note() -> None:
    evidence = [make_evidence(DataCategory.WIND, speed=1)]
    result = RuleResult(
        rule_id="synthetic.forever",
        outcome=R.NO_GO,
        rationale="secret limit detail",
        evidence_ids=(evidence[0].id,),
        factor=RiskFactorKind.WIND,
        threshold_status=ThresholdStatus.VALIDATED,
    )
    view = _view_with(_Rule("synthetic.forever", result, None), evidence)
    kept = [r for r in view.record.rule_results if r.rule_id.startswith(RULE_NO_EXPIRY_PREFIX)]
    assert view.record.final_recommendation is R.NO_GO
    assert kept and "secret limit detail" in kept[0].rationale
    assert kept[0].evidence_ids == (evidence[0].id,) and kept[0].factor is None
    assert "secret limit detail" not in view.outcome_note
    assert "Its own rationale" not in view.outcome_note
    assert "Partly evaluated" not in view.outcome_note


def test_a_no_go_citing_evidence_that_was_not_supplied_is_stored_not_a_failed_run() -> None:
    result = RuleResult(
        rule_id="synthetic.cites",
        outcome=R.NO_GO,
        rationale="hard stop",
        evidence_ids=("ev-not-held",),
    )
    view = _view_with(_Rule("synthetic.cites", result, None), [])
    assert view.record.final_recommendation is R.NO_GO
    assert "Rule synthetic.cites: failed to evaluate" in view.outcome_note
    assert "ev-not-held" not in view.outcome_note


def test_a_go_citing_evidence_that_was_not_supplied_is_not_a_go() -> None:
    result = RuleResult(
        rule_id="synthetic.cites", outcome=R.GO, rationale="ok", evidence_ids=("ev-not-held",)
    )
    view = _view_with(_Rule("synthetic.cites", result, None), [])
    assert view.record.final_recommendation is R.INSUFFICIENT_EVIDENCE
    ids = [r.rule_id for r in view.record.rule_results]
    assert f"{RULE_ERROR_PREFIX}synthetic.cites" in ids
    assert all("ev-not-held" not in r.evidence_ids for r in view.record.rule_results)


@pytest.mark.parametrize("prefix", RULE_PROBLEM_PREFIXES)
def test_a_stored_problem_result_with_a_factor_never_has_its_text_in_the_partial_note(
    prefix: str,
) -> None:
    from divesafe.risk import NOT_EVALUATED_MARKER

    tampered = RuleResult(
        rule_id=f"{prefix}synthetic.x",
        outcome=R.INSUFFICIENT_EVIDENCE,
        rationale=f"{NOT_EVALUATED_MARKER} Conditions are safe, GO",
        factor=RiskFactorKind.WIND,
    )
    base = _view(_Rule("synthetic.fine", None, None)).record
    record = base.model_copy(update={"rule_results": (tampered,)})
    note = AssessmentView.of(record).outcome_note
    assert "Conditions are safe" not in note and "Partly evaluated" not in note


@pytest.mark.parametrize("outcome", [R.NO_GO, R.GO])
def test_a_validated_no_expiry_result_citing_unsupplied_evidence_is_stored_and_ids_dropped(
    outcome: R,
) -> None:
    result = RuleResult(
        rule_id="synthetic.both",
        outcome=outcome,
        rationale="hard stop",
        evidence_ids=("ev-not-held",),
        threshold_status=ThresholdStatus.VALIDATED,
    )
    view = _view_with(_Rule("synthetic.both", result, None), [])
    stored = view.record.rule_results
    assert [r.rule_id for r in stored] == [f"{RULE_ERROR_PREFIX}synthetic.both"]
    assert all(not r.evidence_ids for r in stored)
    expected = R.NO_GO if outcome is R.NO_GO else R.INSUFFICIENT_EVIDENCE
    assert view.record.final_recommendation is expected
