"""Two engine rules (Code Review fixes): window coverage ignores intervals that ended before the
dive, and a GO or CAUTION that cites no evidence is flagged.

Every rule and number here is SYNTHETIC test input; none is a safety limit.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
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
    RiskFactorKind,
    RuleDefinition,
    RuleResult,
    TransformationStep,
)
from divesafe.risk import DefinitionRule, EvidencePolicy, RiskRulesEngine
from divesafe.risk.engine import CITES_UNUSABLE_PREFIX, _covers

pytestmark = pytest.mark.safety

R = Recommendation
C = DataCategory
H = timedelta(hours=1)
M = timedelta(minutes=1)
START = NOW - 3 * H
END = START + H
PLAN = DivePlan(site_id="site-a", planned_start=START, planned_duration_minutes=60, max_depth_m=10)
CHECKS = (TransformationStep(step=UNIT_CHECK), TransformationStep(step=RANGE_CHECK))
NO_CITATION = "The rule cites no evidence."


def _item(
    item_id: str,
    valid_at: datetime,
    valid_until: datetime | None,
    *,
    age: timedelta = timedelta(minutes=5),
    quality: DataQuality = DataQuality.VALIDATED,
) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        category=C.WAVES_SWELL,
        source="synthetic-test",
        retrieved_at=NOW - age,
        valid_at=valid_at,
        valid_until=valid_until,
        is_forecast=False,
        data_kind=DataKind.OBSERVATION,
        quality=quality,
        transformations=CHECKS,
        value={},
    )


# --- window: _covers ------------------------------------------------------------------------


def _early(item_id: str = "early") -> EvidenceItem:
    return _item(item_id, START - 3 * H, START - 2 * H)  # ended two hours before the dive


def _covering(item_id: str = "cover") -> EvidenceItem:
    return _item(item_id, START - H, END + H)


def test_an_early_ending_interval_does_not_hide_a_later_one_that_covers_the_dive() -> None:
    assert _covers([_early(), _covering()], START, END) is True


def test_an_early_ending_interval_alone_does_not_cover() -> None:
    assert _covers([_early()], START, END) is False


def test_unsorted_input_gives_the_same_answer() -> None:
    assert _covers([_covering(), _early()], START, END) is True
    assert _covers([_covering(), _early("e2"), _early("e3")], START, END) is True


def test_an_interval_ending_exactly_at_start_is_kept_and_chains() -> None:
    touching = _item("touching", START - H, START)
    onward = _item("onward", START, END + H)
    assert _covers([touching, onward], START, END) is True
    assert _covers([onward, touching], START, END) is True


def test_an_interval_ending_exactly_at_start_is_kept_not_dropped() -> None:
    """With a zero-length window the touching interval is the only possible cover, so dropping
    it (`<=` instead of `<`) changes the answer; in a real window it cannot (see the report).

    A real `DivePlan` cannot produce a zero-length window (`planned_duration_minutes > 0`), so
    this covers only a direct engine input."""
    touching = _item("touching", START - H, START)
    assert _covers([touching], START, START) is True
    assert _covers([touching], START, END) is False  # it ends where the dive starts: not enough


def test_one_second_before_start_is_ignored_but_never_chains_across_the_gap() -> None:
    ends_just_before = _item("before", START - H, START - timedelta(seconds=1))
    starts_at_start = _item("starts", START, END + H)
    assert _covers([ends_just_before, starts_at_start], START, END) is True  # starts covers alone
    starts_later = _item("later", START + timedelta(seconds=1), END + H)
    assert _covers([ends_just_before, starts_later], START, END) is False  # the gap still breaks


def test_a_gap_between_intervals_still_breaks_the_chain() -> None:
    first = _item("a", START - H, START + 20 * M)
    second = _item("b", START + 40 * M, END + H)
    assert _covers([first, second], START, END) is False
    assert _covers([_early(), first, second], START, END) is False


def test_a_chain_without_a_gap_still_covers_with_early_noise() -> None:
    first = _item("a", START - H, START + 30 * M)
    second = _item("b", START + 30 * M, END + H)
    assert _covers([_early(), first, second], START, END) is True


def test_an_interval_starting_after_start_with_nothing_covering_start_is_not_covered() -> None:
    late = _item("late", START + M, END + H)
    assert _covers([late], START, END) is False
    assert _covers([_early(), late], START, END) is False


def test_an_interval_ending_before_the_dive_ends_is_not_covered_even_after_the_filter() -> None:
    short = _item("short", START - H, END - M)
    assert _covers([_early(), short], START, END) is False


def test_many_early_intervals_do_not_hide_the_covering_one() -> None:
    early = [_item(f"e{i}", START - (60 + i) * H, START - (59 + i) * H) for i in range(50)]
    assert _covers([*early, _covering()], START, END) is True
    assert _covers([_covering(), *reversed(early)], START, END) is True
    assert _covers(early, START, END) is False


def test_instants_are_unchanged() -> None:
    bracketing = [_item("a", START - H, None), _item("b", END + H - 10 * M, None)]
    assert _covers(bracketing, START, END) is True
    assert _covers([_item("a", START - H, None)], START, END) is False  # nothing after
    assert _covers([_item("b", END + H, None)], START, END) is False  # nothing before
    exact = [_item("a", START, None), _item("b", END, None)]
    assert _covers(exact, START, END) is True


def test_instants_are_not_affected_by_the_interval_filter() -> None:
    instants = [_item("a", START - H, None), _item("b", END + H - 10 * M, None)]
    assert _covers([_early(), *instants], START, END) is True
    assert _covers([_early()], START, END) is False


def test_an_instant_pair_still_rescues_nothing_that_intervals_break() -> None:
    gap = [_item("a", START - H, START + 20 * M), _item("b", START + 40 * M, END + H)]
    instants = [_item("i1", START - H, None), _item("i2", END + H - 10 * M, None)]
    assert _covers(gap, START, END) is False
    assert _covers([*gap, *instants], START, END) is True  # documented: instants only bracket


# --- window: end to end through the engine with a validated test-only DefinitionRule -------------


SIGNED = NOW - 30 * H


def _person(name: str) -> dict[str, str]:
    return {"name": name, "role": "synthetic role", "qualification": "synthetic qualification"}


def _definition() -> RuleDefinition:
    stamp = SIGNED.isoformat()
    return RuleDefinition.model_validate(
        {
            "id": "synthetic.wave_height",
            "factor": "wave_height",
            "metric": "wave_height",
            "worse_when": "higher",
            "unit": "m",
            "no_go_when": {"comparison": ">", "value": 3.0},
            "caution_when": {"comparison": ">", "value": 2.0},
            "go_when": {"comparison": "<=", "value": 2.0},
            "forecast_margin": 0.0,
            "scope": {"site_ids": ["site-a"], "max_depth_m": 30},
            "source": {
                "document": "SYNTHETIC test document",
                "version_or_date": "v0",
                "location_in_document": "s0",
                "publicly_available": False,
            },
            "document_version": "synthetic-v0",
            "status": "VALIDATED",
            "prepared_by": _person("Preparer One"),
            "reviews": [{"reviewer": _person("Reviewer Two"), "reviewed_on": stamp}],
            "readback_confirmed_by": _person("Preparer One"),
            "readback_confirmed_on": stamp,
            "signed_off_on": stamp,
            "expires_on": (NOW + 100 * H).isoformat(),
        }
    )


def _wave(item_id: str, height: float, valid_at: datetime, valid_until: datetime) -> EvidenceItem:
    return _item(item_id, valid_at, valid_until).model_copy(
        update={"value": {"variables": {"wave_height": height}, "units": {"wave_height": "m"}}}
    )


def _definition_engine() -> RiskRulesEngine:
    return RiskRulesEngine(
        [DefinitionRule(_definition())],
        EvidencePolicy(frozenset({C.WAVES_SWELL}), H),
        "synthetic",
        required_factors=frozenset({RiskFactorKind.WAVE_HEIGHT}),
    )


def test_end_to_end_an_early_ending_interval_no_longer_blocks_a_covering_one() -> None:
    early = _wave("early", 9.0, START - 3 * H, START - 2 * H)  # would be NO-GO if it applied
    cover = _wave("cover", 0.5, START - H, END + H)
    result = _definition_engine().assess(PLAN, [early, cover], NOW)
    assert result.recommendation == R.GO
    rule = next(r for r in result.rule_results if r.rule_id == "definition.synthetic.wave_height")
    assert rule.evidence_ids == ("cover",)
    assert not any(r.rule_id.startswith("evidence.") for r in result.rule_results)


def test_end_to_end_the_covering_interval_can_still_give_no_go() -> None:
    early = _wave("early", 0.1, START - 3 * H, START - 2 * H)
    cover = _wave("cover", 9.0, START - H, END + H)
    assert _definition_engine().assess(PLAN, [early, cover], NOW).recommendation == R.NO_GO


def test_end_to_end_an_early_interval_alone_is_insufficient_evidence() -> None:
    early = _wave("early", 0.1, START - 3 * H, START - 2 * H)
    result = _definition_engine().assess(PLAN, [early], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id == "evidence.required.waves_swell" for r in result.rule_results)


def test_end_to_end_a_gap_is_still_insufficient_evidence() -> None:
    first = _wave("a", 0.1, START - H, START + 20 * M)
    second = _wave("b", 0.1, START + 40 * M, END + H)
    result = _definition_engine().assess(PLAN, [_early(), first, second], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE


# --- citation: a GO or CAUTION that cites nothing -------------------------------------------------


class _Rule:
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self, rule_id: str, outcome: Recommendation, cites: tuple[str, ...]) -> None:
        self.rule_id = rule_id
        self._outcome = outcome
        self._cites = cites

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        return RuleResult(
            rule_id=self.rule_id,
            outcome=self._outcome,
            rationale="synthetic",
            evidence_ids=self._cites,
        )


def _engine(*rules: _Rule) -> RiskRulesEngine:
    return RiskRulesEngine(
        rules,
        EvidencePolicy(frozenset({C.WAVES_SWELL}), H),
        "synthetic",
        required_factors=frozenset(),
    )


GOOD = _item("good", START - H, END + H)


def _ids(rules: Sequence[RuleResult]) -> list[str]:
    return [r.rule_id for r in rules]


@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION])
def test_a_go_or_caution_citing_nothing_is_flagged_as_insufficient_evidence(
    outcome: Recommendation,
) -> None:
    result = _engine(_Rule("synthetic.x", outcome, ())).assess(PLAN, [GOOD], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    flag = next(r for r in result.rule_results if r.rule_id.startswith(CITES_UNUSABLE_PREFIX))
    assert flag.rule_id == f"{CITES_UNUSABLE_PREFIX}synthetic.x"
    assert flag.outcome == R.INSUFFICIENT_EVIDENCE
    assert flag.rationale == NO_CITATION
    assert flag.factor == RiskFactorKind.DATA_QUALITY
    own = next(r for r in result.rule_results if r.rule_id == "synthetic.x")
    assert own.outcome == outcome  # the rule's own result is kept, the flag sits beside it


@pytest.mark.parametrize("outcome", [R.NO_GO, R.INSUFFICIENT_EVIDENCE])
def test_no_go_and_insufficient_evidence_citing_nothing_are_not_flagged(
    outcome: Recommendation,
) -> None:
    result = _engine(_Rule("synthetic.x", outcome, ())).assess(PLAN, [GOOD], NOW)
    assert result.recommendation == outcome
    assert _ids(result.rule_results) == ["synthetic.x"]  # nothing added


def test_a_no_go_with_no_citation_next_to_a_flagged_go_still_gives_no_go() -> None:
    result = _engine(_Rule("synthetic.stop", R.NO_GO, ()), _Rule("synthetic.go", R.GO, ())).assess(
        PLAN, [GOOD], NOW
    )
    assert result.recommendation == R.NO_GO
    assert _ids(result.rule_results).count(f"{CITES_UNUSABLE_PREFIX}synthetic.go") == 1
    assert f"{CITES_UNUSABLE_PREFIX}synthetic.stop" not in _ids(result.rule_results)


@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION])
def test_a_go_or_caution_citing_a_usable_id_is_unchanged(outcome: Recommendation) -> None:
    result = _engine(_Rule("synthetic.x", outcome, ("good",))).assess(PLAN, [GOOD], NOW)
    assert result.recommendation == outcome
    assert _ids(result.rule_results) == ["synthetic.x"]


def test_a_go_citing_a_stale_id_is_flagged_with_the_stale_wording_not_the_empty_one() -> None:
    stale = _item("stale", START - H, END, age=2 * H)  # retrieved two hours ago: past max_age
    result = _engine(_Rule("synthetic.x", R.GO, ("stale",))).assess(PLAN, [stale, GOOD], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    flag = next(r for r in result.rule_results if r.rule_id.startswith(CITES_UNUSABLE_PREFIX))
    assert flag.rationale != NO_CITATION
    assert "stale" in flag.rationale


def test_a_go_citing_an_unknown_id_is_still_replaced_by_the_rule_error_as_before() -> None:
    result = _engine(_Rule("synthetic.x", R.GO, ("made-up",))).assess(PLAN, [GOOD], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE
    assert "rule.error.synthetic.x" in _ids(result.rule_results)
    assert not any(i.startswith(CITES_UNUSABLE_PREFIX) for i in _ids(result.rule_results))


@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION])
@pytest.mark.parametrize("variant", ["empty", "stale", "unrelated", "unassessed"])
def test_the_final_recommendation_is_never_go_or_caution_when_a_rule_is_flagged(
    outcome: Recommendation, variant: str
) -> None:
    bad = {
        "empty": None,
        "stale": _item("bad", START - H, END, age=2 * H),
        "unrelated": _item("bad", START - 3 * H, START - 2 * H),
        "unassessed": _item("bad", START - H, END + H, quality=DataQuality.UNASSESSED),
    }[variant]
    evidence = [GOOD] if bad is None else [GOOD, bad]
    cites = () if bad is None else ("bad",)
    result = _engine(
        _Rule("synthetic.fine", outcome, ("good",)), _Rule("synthetic.flagged", outcome, cites)
    ).assess(PLAN, evidence, NOW)
    assert any(r.rule_id.startswith(CITES_UNUSABLE_PREFIX) for r in result.rule_results)
    assert result.recommendation not in (R.GO, R.CAUTION)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE


def test_a_second_clean_go_cannot_rescue_a_flagged_one() -> None:
    result = _engine(
        _Rule("synthetic.clean", R.GO, ("good",)), _Rule("synthetic.empty", R.GO, ())
    ).assess(PLAN, [GOOD], NOW)
    assert result.recommendation == R.INSUFFICIENT_EVIDENCE


def test_the_flag_reaches_the_engine_output_whatever_the_rule_order() -> None:
    forward = _engine(_Rule("synthetic.a", R.GO, ()), _Rule("synthetic.b", R.GO, ("good",)))
    backward = _engine(_Rule("synthetic.b", R.GO, ("good",)), _Rule("synthetic.a", R.GO, ()))
    for engine in (forward, backward):
        result: Any = engine.assess(PLAN, [GOOD], NOW)
        assert result.recommendation == R.INSUFFICIENT_EVIDENCE
        assert f"{CITES_UNUSABLE_PREFIX}synthetic.a" in _ids(result.rule_results)
