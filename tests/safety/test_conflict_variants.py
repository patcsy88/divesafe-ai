"""ADR 0026 Part A: conflicting evidence ids are decided by the most severe variant.

Deterministic, no model. Uses the labelled SYNTHETIC wave-height limit from
`test_evidence_withheld` (`no_go_engine`); no real limit is used.

EQUIVALENT MUTANTS (not tested):
  - the `break` after NO-GO (only run time changes; `test_stopping_early_...` pins the count);
  - the order of `itertools.product` (the maximum does not depend on order);
  - the variant check in Rejudge (no rule takes the clock, so it can add nothing Assess did not);
  - removing `if not groups: return []` (one run on no variants cannot be worse than the baseline,
    so the `worst_choice is None` guard returns the same empty list).
"""

from __future__ import annotations

import asyncio
import itertools
import math
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from tests.conftest import NOW
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_evidence_conflict import _Source
from tests.safety.test_evidence_withheld import START, assess_with, height, no_go_engine

from divesafe.domain import EvidenceItem, Recommendation, most_severe
from divesafe.orchestration import conflict_variants as cv
from divesafe.orchestration.conflict_variants import (
    MAX_COMBINATIONS,
    MAX_CONFLICT_IDS,
    MAX_VARIANTS_PER_ID,
    UNBOUNDED_RULE,
    VARIANT_RULE,
    variant_results,
)

pytestmark = pytest.mark.safety

R = Recommendation
SAFE, DANGER = 0.1, 5.0  # metres, SYNTHETIC (the engine's limit is a labelled test definition)


def sources(hour_values: dict[int, Sequence[float]]) -> list[_Source]:
    """One connector per variant index; connector k sends variant k of every conflicting hour."""
    width = max(len(v) for v in hour_values.values())
    out = []
    for k in range(width):
        items = [height(h, vals[k]) for h, vals in hour_values.items() if k < len(vals)]
        out.append(_Source(f"c{k}", *items))
    return out


def outcome(hour_values: dict[int, Sequence[float]]) -> tuple[R, list[str]]:
    record = assess_with(sources(hour_values))
    return record.final_recommendation, [r.rule_id for r in record.rule_results]


def test_a_dangerous_variant_gives_no_go_whichever_connector_sent_it() -> None:
    for values in ([SAFE, DANGER], [DANGER, SAFE]):
        final, rules = outcome({0: values, 1: [SAFE]})
        assert final == R.NO_GO
        assert VARIANT_RULE in rules


def test_with_every_variant_safe_the_result_is_not_made_more_severe_by_the_variants() -> None:
    final, rules = outcome({0: [SAFE, SAFE + 0.1], 1: [SAFE]})
    assert VARIANT_RULE not in rules and UNBOUNDED_RULE not in rules
    assert final == R.INSUFFICIENT_EVIDENCE  # the conflict itself still stops GO (ADR 0008)


def test_a_variant_can_never_relax_the_main_result() -> None:
    engine = no_go_engine()
    plan = _plan()
    baseline = R.NO_GO
    safe_group = (height(0, SAFE), height(0, SAFE + 0.1))
    assert variant_results(engine, plan, [], [safe_group], NOW, baseline) == []


def _plan() -> Any:
    from tests.safety.test_evidence_conflict import PLAN

    return PLAN.model_copy(
        update={"planned_start": START, "planned_duration_minutes": 60, "max_depth_m": 20}
    )


def test_the_result_does_not_depend_on_connector_order() -> None:
    forward = outcome({0: [SAFE, DANGER, SAFE + 0.2], 1: [SAFE, SAFE + 0.1]})
    backward = outcome({0: [SAFE + 0.2, DANGER, SAFE], 1: [SAFE + 0.1, SAFE]})
    assert forward[0] == backward[0] == R.NO_GO


def test_the_variant_rule_has_no_evidence_ids_and_names_no_id_or_value() -> None:
    record = assess_with(sources({0: [SAFE, DANGER], 1: [SAFE]}))
    rule = next(r for r in record.rule_results if r.rule_id == VARIANT_RULE)
    assert rule.evidence_ids == ()
    assert "e-0" not in rule.rationale and "5.0" not in rule.rationale
    assert "waves_swell" in rule.rationale


# --- the search is exhaustive and bounded ------------------------------------------------------


def test_the_worst_combination_is_found_among_many() -> None:
    """Only one of 3 x 2 = 6 combinations is dangerous; all must be tried."""
    for position in range(3):
        values = [SAFE] * 3
        values[position] = DANGER
        final, rules = outcome({0: values, 1: [SAFE, SAFE + 0.1]})
        assert final == R.NO_GO, position
        assert VARIANT_RULE in rules


def test_stopping_early_gives_the_same_result_as_trying_every_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = no_go_engine()
    plan = _plan()
    groups = [
        tuple(height(0, v) for v in (DANGER, SAFE, SAFE + 0.1)),
        tuple(height(1, v) for v in (SAFE, SAFE + 0.1)),
    ]
    calls: list[int] = []
    real = engine.assess

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "assess", counting)
    found = variant_results(engine, plan, [], groups, NOW, R.INSUFFICIENT_EVIDENCE)
    assert [r.outcome for r in found] == [R.NO_GO]
    assert len(calls) < math.prod(len(g) for g in groups)  # stopped on the first NO-GO

    brute = most_severe(
        tuple(
            most_severe(tuple(r.outcome for r in real(plan, list(combo), NOW).rule_results))
            for combo in itertools.product(*groups)
        )
    )
    assert brute == R.NO_GO


def conflict_groups(ids: int, variants: int) -> list[tuple[EvidenceItem, ...]]:
    return [tuple(height(n, SAFE + 0.01 * k) for k in range(variants)) for n in range(ids)]


@pytest.mark.parametrize(
    ("ids", "variants"),
    [
        (MAX_CONFLICT_IDS + 1, 1 + 1),
        (1, MAX_VARIANTS_PER_ID + 1),
        (MAX_CONFLICT_IDS, MAX_VARIANTS_PER_ID),  # 4 ** 3 = 81 combinations > the cap
    ],
)
def test_beyond_a_bound_nothing_is_guessed_and_the_result_is_unbounded(
    ids: int, variants: int
) -> None:
    assert not cv.within_bounds(conflict_groups(ids, variants))
    found = variant_results(
        no_go_engine(), _plan(), [], conflict_groups(ids, variants), NOW, R.INSUFFICIENT_EVIDENCE
    )
    assert [(r.rule_id, r.outcome) for r in found] == [(UNBOUNDED_RULE, R.INSUFFICIENT_EVIDENCE)]


def test_the_bounds_are_the_owners_numbers_and_the_edges_are_inside() -> None:
    assert (MAX_CONFLICT_IDS, MAX_VARIANTS_PER_ID, MAX_COMBINATIONS) == (4, 3, 64)
    assert cv.within_bounds(conflict_groups(4, 2))  # 16
    assert cv.within_bounds(conflict_groups(3, 3))  # 27
    assert cv.within_bounds(conflict_groups(1, 3))
    assert cv.within_bounds([])


def test_an_unbounded_conflict_in_a_whole_run_is_insufficient_evidence_not_go() -> None:
    values = {h: [SAFE, SAFE + 0.1] for h in range(MAX_CONFLICT_IDS + 1)}
    record = assess_with(sources(values))
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert UNBOUNDED_RULE in [r.rule_id for r in record.rule_results]


def test_no_conflict_adds_no_variant_rule() -> None:
    record = assess_with([_Source("a", height(0, DANGER), height(1, SAFE))])
    assert record.final_recommendation == R.NO_GO
    assert not {VARIANT_RULE, UNBOUNDED_RULE} & {r.rule_id for r in record.rule_results}


def test_the_stage_trace_names_the_variant_result_with_a_fixed_code() -> None:
    record = assess_with(sources({0: [SAFE, DANGER], 1: [SAFE]}))
    assess = next(t for t in record.stages if t.name.value == "assess")
    assert "assess.conflict_variant" in assess.issues
    unbounded = assess_with(sources({h: [SAFE, SAFE + 0.1] for h in range(5)}))
    assert (
        "assess.conflict_unbounded"
        in next(t for t in unbounded.stages if t.name.value == "assess").issues
    )


def test_variants_are_checked_again_at_the_later_clock() -> None:
    """The variant result survives Rejudge (the call there is an equivalent mutant, see above)."""
    from divesafe.data import TIOMAN_ISLAND
    from divesafe.orchestration import assess_dive

    clock_values = iter([NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=1)])
    plan = _plan()
    record = asyncio.run(
        assess_dive(
            assessment_id="v-1",
            plan=plan,
            site=TIOMAN_ISLAND,
            connectors=sources({0: [SAFE, DANGER], 1: [SAFE]}),
            engine=no_go_engine(),
            now=NOW,
            clock=lambda: next(clock_values),
        )
    )
    assert record.final_recommendation == R.NO_GO
    assert [r.rule_id for r in record.rule_results].count(VARIANT_RULE) == 1


# --- mutations on a copy of the module ----------------------------------------------------------


def misses(module: Any) -> list[str]:
    bad: list[str] = []
    engine, plan = no_go_engine(), _plan()
    run = module.variant_results

    def outs(groups: Any, baseline: R = R.INSUFFICIENT_EVIDENCE) -> list[tuple[str, R]]:
        return [(r.rule_id, r.outcome) for r in run(engine, plan, [], groups, NOW, baseline)]

    last = [tuple(height(0, v) for v in (SAFE, SAFE + 0.1, DANGER)), (height(1, SAFE),)]
    if outs(last) != [(VARIANT_RULE, R.NO_GO)]:
        bad.append("the dangerous variant (last) was missed")
    first = [tuple(height(0, v) for v in (DANGER, SAFE)), (height(1, SAFE),)]
    if outs(first) != [(VARIANT_RULE, R.NO_GO)]:
        bad.append("the dangerous variant (first) was missed")
    if outs([tuple(height(0, v) for v in (SAFE, SAFE + 0.1))]) != []:
        bad.append("a safe variant added a result")
    if outs([tuple(height(0, v) for v in (SAFE, DANGER))], R.NO_GO) != []:
        bad.append("a variant not worse than the baseline added a result")
    if outs([]) != []:
        bad.append("no conflict added a result")
    for ids, variants in ((5, 2), (1, 4), (4, 3)):
        got = outs(conflict_groups(ids, variants))
        if got != [(UNBOUNDED_RULE, R.INSUFFICIENT_EVIDENCE)]:
            bad.append(f"bound {ids}x{variants} not enforced")
    for ids, variants in ((4, 2), (3, 3)):
        if outs(conflict_groups(ids, variants)) == [(UNBOUNDED_RULE, R.INSUFFICIENT_EVIDENCE)]:
            bad.append(f"a conflict inside the bounds {ids}x{variants} was refused")
    both = [tuple(height(0, v) for v in (SAFE, DANGER)), tuple(height(1, v) for v in (SAFE, SAFE))]
    if outs(both) != [(VARIANT_RULE, R.NO_GO)]:
        bad.append("a two-id search missed the danger")
    return bad


def test_the_real_module_meets_every_check() -> None:
    assert misses(cv) == []


MUTATIONS = [
    pytest.param(("len(groups) <= MAX_CONFLICT_IDS", "True"), id="id-bound-off"),
    pytest.param(
        ("all(len(g) <= MAX_VARIANTS_PER_ID for g in groups)", "True"), id="variant-bound-off"
    ),
    pytest.param(
        ("math.prod(len(g) for g in groups) <= MAX_COMBINATIONS", "True"), id="combination-cap-off"
    ),
    pytest.param(
        ("len(groups) <= MAX_CONFLICT_IDS", "len(groups) < MAX_CONFLICT_IDS"), id="id-off-by-one"
    ),
    pytest.param(
        ("if severity(outcome) > severity(worst):", "if severity(outcome) >= severity(worst):"),
        id="not-worse-still-counts",
    ),
    pytest.param(
        ("if severity(outcome) > severity(worst):", "if severity(outcome) < severity(worst):"),
        id="least-severe-kept",
    ),
    pytest.param(
        ("itertools.product(*(range(len(g)) for g in groups))", "[(0,) * len(groups)]"),
        id="first-variant-only",
    ),
    pytest.param(
        ("itertools.product(*(range(len(g)) for g in groups))", "[(len(g) - 1 for g in groups)]"),
        id="last-variant-only",
    ),
    pytest.param(
        ("    if worst_choice is None:\n        return []\n", ""), id="no-worse-guard-removed"
    ),
    pytest.param(
        (
            "outcome=Recommendation.INSUFFICIENT_EVIDENCE,\n                rationale=(\n",
            "outcome=Recommendation.GO,\n                rationale=(\n",
        ),
        id="unbounded-is-go",
    ),
    pytest.param(("outcome=worst,", "outcome=Recommendation.GO,"), id="variant-result-is-go"),
]


@pytest.mark.parametrize("edit", MUTATIONS)
def test_mutation_of_the_variant_search_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(cv, edit)
    try:
        found = misses(mutant)
    except Exception:  # noqa: BLE001
        found = ["the mutant crashed"]  # a crash is a refusal too, never a quiet pass
    assert found != []


# --- mutations of the wiring (copies of the pipeline and the evidence service) -------------------

ASSESS_CALL = (
    "    run.rule_results.extend(\n"
    "        variant_results("
    "engine, plan, evidence.items, evidence.variants, now, run.deterministic)\n"
    "    )\n"
)


def wiring_misses(pipeline: Any) -> list[str]:
    import asyncio as aio

    from divesafe.data import TIOMAN_ISLAND

    bad: list[str] = []
    clock_values = iter([NOW, NOW + timedelta(minutes=1)] * 4)
    record = aio.run(
        pipeline.assess_dive(
            assessment_id="m-1",
            plan=_plan(),
            site=TIOMAN_ISLAND,
            connectors=sources({0: [SAFE, DANGER], 1: [SAFE]}),
            engine=no_go_engine(),
            now=NOW,
            clock=lambda: next(clock_values),
        )
    )
    ids = [r.rule_id for r in record.rule_results]
    if record.final_recommendation != R.NO_GO:
        bad.append("the dangerous variant did not give NO-GO")
    if ids.count(VARIANT_RULE) != 1:
        bad.append("the variant rule is not present exactly once")
    return bad


PIPELINE_WIRING = [
    pytest.param((ASSESS_CALL, ""), id="assess-ignores-variants"),
    pytest.param(
        ("evidence.variants, now, run.deterministic", "(), now, run.deterministic"),
        id="assess-passes-no-variants",
    ),
    pytest.param(
        (
            "    run.deterministic = most_severe(tuple(r.outcome for r in run.rule_results))\n"
            "    run.unevaluated = assessment.unevaluated_factors",
            "    run.unevaluated = assessment.unevaluated_factors",
        ),
        id="deterministic-not-recomputed-after-variants",
    ),
]


def test_the_real_pipeline_meets_the_wiring_checks() -> None:
    from divesafe.orchestration import pipeline as pipeline_module

    assert wiring_misses(pipeline_module) == []


@pytest.mark.parametrize("edit", PIPELINE_WIRING)
def test_mutation_of_the_pipeline_wiring_is_caught(edit: tuple[str, str]) -> None:
    from divesafe.orchestration import pipeline as pipeline_module

    mutant = mutated_module(pipeline_module, edit)
    try:
        found = wiring_misses(mutant)
    except Exception:  # noqa: BLE001
        found = ["the mutant crashed"]
    assert found != []


def test_mutation_of_the_variants_property_is_caught() -> None:
    from divesafe.services import evidence as evidence_module

    edit = (
        "by_id.setdefault(item.id, []).append(item)\n        return tuple(tuple(items) for",
        "by_id.setdefault(item.id, []).append(item)\n        return tuple(tuple(items[:1]) for",
    )
    mutant = mutated_module(evidence_module, edit)
    records = mutant.build_evidence_set(
        [
            ("a", _fetch(height(0, DANGER)), None),
            ("b", _fetch(height(0, SAFE)), None),
        ]
    )
    assert len(records.variants[0]) == 1  # the mutant lost a variant ...
    real = evidence_module.build_evidence_set(
        [
            ("a", _fetch(height(0, DANGER)), None),
            ("b", _fetch(height(0, SAFE)), None),
        ]
    )
    assert [len(g) for g in real.variants] == [2]  # ... the real one keeps both


def _fetch(*items: EvidenceItem) -> Any:
    from divesafe.data import FetchResult

    return FetchResult(items)


def _no_go_start_calls(module: Any) -> int:
    engine, plan = no_go_engine(), _plan()
    calls: list[int] = []
    real = engine.assess

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    engine.assess = counting  # type: ignore[method-assign]
    found = module.variant_results(engine, plan, [], conflict_groups(3, 3), NOW, R.NO_GO)
    assert found == []
    return len(calls)


def test_a_result_already_no_go_runs_the_engine_no_more_times() -> None:
    assert _no_go_start_calls(cv) == 0


def test_mutation_running_the_search_from_a_no_go_start_is_caught() -> None:
    mutant = mutated_module(
        cv,
        (
            "if not groups or baseline is Recommendation.NO_GO:",
            "if not groups:",
        ),
    )
    assert _no_go_start_calls(mutant) > 0


def _later_group_and_kept_items(module: Any) -> list[str]:
    bad: list[str] = []
    engine, plan = no_go_engine(), _plan()
    seen: list[list[str]] = []
    real = engine.assess

    def spy(p: Any, items: Any, now: Any) -> Any:
        seen.append([i.id for i in items])
        return real(p, items, now)

    engine.assess = spy  # type: ignore[method-assign]
    kept = height(7, SAFE)
    groups = [
        tuple(height(0, v) for v in (SAFE, SAFE + 0.1)),
        tuple(height(1, v) for v in (SAFE, DANGER)),  # danger only in the second group, 2nd variant
    ]
    got = module.variant_results(engine, plan, [kept], groups, NOW, R.INSUFFICIENT_EVIDENCE)
    if [(r.rule_id, r.outcome) for r in got] != [(VARIANT_RULE, R.NO_GO)]:
        bad.append("a dangerous variant in a later group was missed")
    if not seen or any(kept.id not in ids for ids in seen):
        bad.append("the kept items were not passed with the variants")
    if any(len(ids) != 3 for ids in seen):
        bad.append("a run did not have the kept item plus one variant per id")
    return bad


def test_a_danger_in_a_later_group_is_found_and_kept_items_stay_in_every_run() -> None:
    assert _later_group_and_kept_items(cv) == []


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            (
                "added = [g[i] for g, i in zip(groups, choice, strict=True)]",
                "added = [groups[0][choice[0]]]",
            ),
            id="only-the-first-group-is-added",
        ),
        pytest.param(("[*items, *added]", "[*added]"), id="kept-items-dropped"),
    ],
)
def test_mutation_of_which_items_each_run_gets_is_caught(edit: tuple[str, str]) -> None:
    assert _later_group_and_kept_items(mutated_module(cv, edit)) != []
