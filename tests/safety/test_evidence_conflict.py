"""Evidence items that share an id but differ (owner decision, Code Review finding 2).

`build_evidence_set` no longer raises. EVERY item with a conflicting id is dropped, whichever
connector sent it, in whatever order, however many claims there are; one fixed issue per
conflicting id names the (sorted) connectors and the (sorted) categories, and nothing else;
identical duplicates collapse to one item with no issue; `data_versions` come from kept items
only. The issue becomes the
`evidence.connector_issues` rule in the pipeline, so a conflict never gives GO. That is "more
cautious" only for GO and CAUTION: a conflict can hide a more severe result (NO-GO), so the result
is INSUFFICIENT EVIDENCE and the dropped items are kept as WITHHELD evidence for the human.

Mutations run on COPIES of `divesafe.services.evidence` (`mutated_module`), never on src.

EQUIVALENT MUTANTS (not tested):
  - `sorted(...)` of the connector names written as `sorted(set(...))`, or the set built from the
    claims in another way that gives the same sorted list;
  - the order of the `kept` list (the result is sorted again afterwards);
  - dropping the `or "unversioned"` fallback only changes an item with no source_version, which
    the evidence model does not allow to be empty here (covered by the existing version test);
  - the text of the logger message (only the fact of a warning and its `connectors` extra are
    asserted);
  - `claims` as a defaultdict instead of `setdefault`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from tests.conftest import NOW, make_evidence
from tests.safety.kb_fakes import mutated_module

from divesafe.data import TIOMAN_ISLAND, DiveSite, FetchResult
from divesafe.domain import DataCategory, DivePlan, EvidenceItem, Recommendation
from divesafe.orchestration import assess_dive
from divesafe.risk import EvidencePolicy, RiskRulesEngine
from divesafe.services import evidence as evidence_module
from divesafe.services import gather_evidence
from divesafe.services.evidence import build_evidence_set

pytestmark = pytest.mark.safety

R = Recommendation
WIND = DataCategory.WIND
WAVES = DataCategory.WAVES_SWELL
SECRET_VALUE = 987654


def wind(speed: int, source: str = "test") -> EvidenceItem:
    item = make_evidence(WIND, speed=speed)
    return item if source == "test" else item.model_copy(update={"source": source})


def waves(height: int = 1) -> EvidenceItem:
    return make_evidence(WAVES, height=height)


def conflict_issue(*connectors: str, categories: tuple[str, ...] = ("wind",)) -> str:
    return (
        f"evidence.conflict: {', '.join(connectors)} returned different "
        f"{', '.join(categories)} evidence under one id; none of it used "
        "(withheld, not evaluated; it could hold a more severe result)"
    )


def result(*items: EvidenceItem) -> FetchResult:
    return FetchResult(tuple(items))


Build = Callable[[Sequence[tuple[str, FetchResult | None, str | None]]], Any]


# --- the behaviour, as checks that work on the real module and on a mutated copy -------------


def misses(build: Build) -> list[str]:
    """Every way `build` departs from the owner decision. Empty for the real function."""
    bad: list[str] = []

    def check(label: str, found: Any, items: list[EvidenceItem], issues: list[str]) -> None:
        if sorted(i.id + str(i.value) for i in found.items) != sorted(
            i.id + str(i.value) for i in items
        ):
            bad.append(f"{label}: items")
        if list(found.issues) != issues:
            bad.append(f"{label}: issues")

    a, b = wind(1), wind(2)
    check(
        "two-connectors",
        build([("x", result(a), None), ("y", result(b), None)]),
        [],
        [conflict_issue("x", "y")],
    )
    check(
        "arrival-order",
        build([("y", result(b), None), ("x", result(a), None)]),
        [],
        [conflict_issue("x", "y")],
    )
    # a third claim identical to the first still leaves a conflict: nothing is used
    check(
        "third-identical-to-first",
        build([("x", result(a), None), ("y", result(b), None), ("z", result(wind(1)), None)]),
        [],
        [conflict_issue("x", "y", "z")],
    )
    check(
        "conflict-arrives-last",
        build([("x", result(a), None), ("y", result(wind(1)), None), ("z", result(b), None)]),
        [],
        [conflict_issue("x", "y", "z")],
    )
    check(
        "conflict-arrives-first",
        build([("x", result(b), None), ("y", result(a), None), ("z", result(wind(1)), None)]),
        [],
        [conflict_issue("x", "y", "z")],
    )
    check(
        "same-connector-twice",
        build([("x", result(a, b), None)]),
        [],
        [conflict_issue("x")],
    )
    check(
        "two-different-ids",
        build(
            [
                ("x", result(a, waves(1)), None),
                ("y", result(b, waves(2)), None),
            ]
        ),
        [],
        [conflict_issue("x", "y"), conflict_issue("x", "y", categories=("waves_swell",))],
    )
    check(
        "a-good-item-is-kept",
        build([("x", result(a, waves(1)), None), ("y", result(b), None)]),
        [waves(1)],
        [conflict_issue("x", "y")],
    )
    check(
        "identical-duplicates-collapse",
        build([("x", result(a), None), ("y", result(wind(1)), None), ("z", result(a), None)]),
        [a],
        [],
    )
    check(
        "failed-connector-and-conflict",
        build(
            [
                ("down", None, "ConnectorTransportError: HTTP 503"),
                ("x", result(a), None),
                ("y", result(b), None),
            ]
        ),
        [],
        ["down: ConnectorTransportError: HTTP 503", conflict_issue("x", "y")],
    )
    # data_versions: a source whose only items were dropped is not recorded
    dropped = build(
        [
            ("x", result(wind(1, "only-dropped"), waves(1)), None),
            ("y", result(wind(2, "only-dropped")), None),
        ]
    )
    if dict(dropped.data_versions) != {"test": "unversioned"}:
        bad.append("data-versions")
    # the issue text is fixed: no id, no payload, no item content
    leaky = build(
        [
            ("x", result(make_evidence(WIND, speed=SECRET_VALUE)), None),
            ("y", result(make_evidence(WIND, speed=SECRET_VALUE + 1)), None),
        ]
    )
    text = " ".join(leaky.issues)
    for fragment in ("ev-wind", str(SECRET_VALUE), "speed", "test", "retrieved"):
        if fragment in text:
            bad.append(f"echo: {fragment}")
    # one issue per id, not per connector
    if len(leaky.issues) != 1:
        bad.append("one-issue-per-id")
    return bad


def test_the_real_function_meets_every_check() -> None:
    found = misses(build_evidence_set)
    assert found == []


# --- unit cases, spelled out ---------------------------------------------------------------------


def test_two_connectors_with_different_evidence_under_one_id_use_none_of_it() -> None:
    found = build_evidence_set(
        [("x", result(wind(1)), None), ("y", result(wind(2)), None)],
    )
    assert found.items == ()
    assert found.issues == (conflict_issue("x", "y"),)
    assert found.data_versions == {}


def test_connector_names_are_sorted_whatever_the_arrival_order() -> None:
    found = build_evidence_set(
        [("zeta", result(wind(1)), None), ("alpha", result(wind(2)), None)],
    )
    assert found.issues == (conflict_issue("alpha", "zeta"),)


def test_a_third_claim_equal_to_the_first_does_not_rescue_the_second() -> None:
    found = build_evidence_set(
        [
            ("x", result(wind(1)), None),
            ("y", result(wind(2)), None),
            ("z", result(wind(1)), None),
        ]
    )
    assert found.items == ()
    assert found.issues == (conflict_issue("x", "y", "z"),)


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_the_conflicting_claim_may_arrive_at_any_position(position: int) -> None:
    claims = [wind(1), wind(1), wind(1)]
    claims.insert(position, wind(2))
    results = [(f"c{n}", result(item), None) for n, item in enumerate(claims)]
    found = build_evidence_set(results)
    assert found.items == ()
    assert found.issues == (conflict_issue("c0", "c1", "c2", "c3"),)


def test_two_conflicting_ids_give_two_issues_and_a_good_item_survives() -> None:
    found = build_evidence_set(
        [
            ("x", result(wind(1), waves(1), make_evidence(DataCategory.CURRENTS, v=1)), None),
            ("y", result(wind(2), waves(2), make_evidence(DataCategory.CURRENTS, v=1)), None),
        ]
    )
    assert [i.category for i in found.items] == [DataCategory.CURRENTS]
    assert found.issues == (
        conflict_issue("x", "y"),
        conflict_issue("x", "y", categories=("waves_swell",)),
    )


def test_data_versions_leave_out_a_source_whose_only_items_were_dropped() -> None:
    found = build_evidence_set(
        [
            ("x", result(wind(1, "only-dropped"), waves(1)), None),
            ("y", result(wind(2, "only-dropped")), None),
        ]
    )
    assert "only-dropped" not in found.data_versions
    assert found.data_versions == {"test": "unversioned"}


def test_the_issue_never_holds_the_id_or_any_item_content() -> None:
    item = make_evidence(WIND, speed=SECRET_VALUE, note="private payload")
    other = make_evidence(WIND, speed=SECRET_VALUE + 1, note="private payload")
    found = build_evidence_set([("x", result(item), None), ("y", result(other), None)])
    text = " ".join(found.issues)
    assert item.id not in text
    assert str(SECRET_VALUE) not in text
    assert "private payload" not in text
    assert found.issues == (conflict_issue("x", "y"),)


def test_a_failed_connector_and_a_conflict_both_appear_in_order() -> None:
    found = build_evidence_set(
        [
            ("x", result(wind(1)), None),
            ("down", None, "ConnectorTransportError: HTTP 503"),
            ("y", result(wind(2)), None),
        ]
    )
    assert found.issues == (
        "down: ConnectorTransportError: HTTP 503",
        conflict_issue("x", "y"),
    )


def test_a_conflict_logs_a_warning_with_the_connector_names_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        build_evidence_set([("y", result(wind(1)), None), ("x", result(wind(SECRET_VALUE)), None)])
    records = [r for r in caplog.records if r.getMessage() == "conflicting evidence dropped"]
    assert len(records) == 1
    assert records[0].__dict__["connectors"] == ["x", "y"]
    assert str(SECRET_VALUE) not in caplog.text
    assert "ev-wind" not in caplog.text


def test_identical_duplicates_make_no_issue_and_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        found = build_evidence_set(
            [("x", result(wind(1)), None), ("y", result(wind(1)), None)],
        )
    assert len(found.items) == 1
    assert found.issues == ()
    assert [r for r in caplog.records if "conflicting" in r.getMessage()] == []


# --- end to end: assess_dive / gather_evidence ---------------------------------------------------


class _Source:
    def __init__(self, name: str, *items: EvidenceItem) -> None:
        self.name = name
        self._items = items

    async def fetch(
        self, site: DiveSite, window_start: datetime, window_end: datetime, now: datetime
    ) -> FetchResult:
        return FetchResult(self._items)


class _GoRule:
    rule_id = "synthetic.go"
    citation = "test fixture (synthetic, not a real safety source)"

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> Any:
        from divesafe.domain import RuleResult

        return RuleResult(rule_id=self.rule_id, outcome=R.GO, rationale="synthetic")


PLAN = DivePlan(
    site_id=TIOMAN_ISLAND.id,
    planned_start=NOW + timedelta(hours=2),
    planned_duration_minutes=45,
    max_depth_m=18,
)


def _engine() -> RiskRulesEngine:
    # only WAVES_SWELL is required, so a conflict on WIND is not "missing required evidence": the
    # only thing that can stop GO is the connector-issues rule
    return RiskRulesEngine(
        [_GoRule()],
        EvidencePolicy(
            frozenset({WAVES}), timedelta(hours=1), degraded_may_support_go=True
        ),  # synthetic
        "synthetic",
        required_factors=frozenset(),
    )


def _assess(connectors: Sequence[Any]) -> Any:
    return asyncio.run(
        assess_dive(
            assessment_id="a-1",
            plan=PLAN,
            site=TIOMAN_ISLAND,
            connectors=connectors,
            engine=_engine(),
            now=NOW,
        )
    )


def test_the_setup_is_go_capable_without_a_conflict() -> None:
    record = _assess([_Source("a", waves(1), wind(1)), _Source("b", wind(1))])
    assert record.final_recommendation == R.GO  # so the next test proves the conflict did it
    assert record.evidence_issues == ()


def test_a_conflict_alone_stops_go_and_adds_the_connector_issues_rule() -> None:
    record = _assess([_Source("a", waves(1), wind(1)), _Source("b", wind(2))])
    rules = [r for r in record.rule_results if r.rule_id == "evidence.connector_issues"]
    assert record.final_recommendation != R.GO
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert len(rules) == 1
    assert rules[0].outcome == R.INSUFFICIENT_EVIDENCE
    assert conflict_issue("a", "b") in rules[0].rationale
    assert record.evidence_issues == (conflict_issue("a", "b"),)
    assert all(item.category != WIND for item in record.evidence)  # none of the conflict is used
    assert record.status == "PENDING_HUMAN"


def test_a_conflict_with_a_proposal_of_go_still_cannot_give_go() -> None:
    record = asyncio.run(
        assess_dive(
            assessment_id="a-2",
            plan=PLAN,
            site=TIOMAN_ISLAND,
            connectors=[_Source("a", waves(1), wind(1)), _Source("b", wind(2))],
            engine=_engine(),
            now=NOW,
            proposed=R.GO,
        )
    )
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert record.llm_attempted_downgrade is True


def test_a_conflict_on_a_required_category_is_insufficient_evidence() -> None:
    engine = RiskRulesEngine(
        [_GoRule()],
        EvidencePolicy(frozenset({WAVES}), timedelta(hours=1), degraded_may_support_go=True),
        "synthetic",
        required_factors=frozenset(),
    )
    record = asyncio.run(
        assess_dive(
            assessment_id="a-3",
            plan=PLAN,
            site=TIOMAN_ISLAND,
            connectors=[_Source("a", waves(1)), _Source("b", waves(2))],
            engine=engine,
            now=NOW,
        )
    )
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id == "evidence.connector_issues" for r in record.rule_results)


def test_gather_evidence_no_longer_raises_on_a_conflict() -> None:
    found = asyncio.run(
        gather_evidence(
            [_Source("a", wind(1)), _Source("b", wind(2))], TIOMAN_ISLAND, NOW, NOW, NOW
        )
    )
    assert found.items == ()
    assert found.issues == (conflict_issue("a", "b"),)


# --- mutations of the service (copies) --------------------------------------------------------

CONDITION = "        if any(item != first for _, item in claimed[1:]):"
KEEP = "        kept.append(first)\n"
ISSUE = "            issues.append(\n"
FIRST = "        first = claimed[0][1]\n"
ECHO = "f\"evidence.conflict: {', '.join(connectors)} returned"

MUTATIONS = [
    pytest.param((CONDITION, "        if False:"), id="conflict-ignored-first-wins"),
    pytest.param(
        (CONDITION, "        if False:"),
        (KEEP, "        kept.append(claimed[-1][1])\n"),
        id="conflict-ignored-last-wins",
    ),
    pytest.param(
        ("            continue\n" + KEEP, KEEP),
        id="only-the-later-item-is-dropped",
    ),
    pytest.param(
        (ISSUE, "            _unused: list[str] = []\n            _unused.append(\n"),
        id="issue-not-added",
    ),
    pytest.param(
        (ISSUE, "            for _c in connectors:\n              issues.append(\n"),
        id="issue-per-connector-not-per-id",
    ),
    pytest.param((CONDITION, "        if len(claimed) > 1:"), id="identical-duplicates-conflict"),
    pytest.param(
        (CONDITION, "        if any(item != first for _, item in claimed[1:2]):"),
        id="only-the-second-claim-is-compared",
    ),
    pytest.param(
        (CONDITION, "        if any(item != claimed[-1][1] for _, item in claimed[:1]):"),
        id="only-first-and-last-compared",
    ),
    pytest.param(
        (FIRST, FIRST + "        versions[first.source] = first.source_version or 'unversioned'\n"),
        id="data-versions-from-dropped-items",
    ),
    pytest.param(
        (ECHO, "f\"evidence.conflict: {claimed[0][1].id} {', '.join(connectors)} returned"),
        id="id-echoed-in-the-issue",
    ),
    pytest.param(
        (ECHO, "f\"evidence.conflict: {claimed[0][1].value} {', '.join(connectors)} returned"),
        id="payload-echoed-in-the-issue",
    ),
    pytest.param(
        (
            "            connectors = sorted({name for name, _ in claimed})",
            "            connectors = [name for name, _ in claimed]",
        ),
        id="connectors-unsorted-and-repeated",
    ),
    pytest.param(
        (
            "            connectors = sorted({name for name, _ in claimed})",
            "            connectors = sorted({name for name, _ in claimed[:2]})",
        ),
        id="third-connector-not-named",
    ),
    pytest.param(
        (
            "        for item in result.items:\n"
            "            claims.setdefault(item.id, []).append((name, item))",
            "        for item in result.items:\n            claims[item.id] = [(name, item)]",
        ),
        id="claims-overwritten-last-claim-wins-silently",
    ),
]


@pytest.mark.parametrize("edits", [p.values[0:] for p in MUTATIONS], ids=[p.id for p in MUTATIONS])
def test_mutation_of_build_evidence_set_is_caught(edits: tuple[tuple[str, str], ...]) -> None:
    mutant = mutated_module(evidence_module, *edits)
    try:
        found = misses(mutant.build_evidence_set)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []


LOG_LINE = (
    '            logger.warning("conflicting evidence dropped", extra={"connectors": connectors})\n'
)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(LOG_LINE, "", id="no-warning-logged"),
        pytest.param(
            LOG_LINE,
            LOG_LINE.replace("connectors}", "connectors, 'id': claimed[0][1].id}"),
            id="id-logged",
        ),
    ],
)
def test_mutation_of_the_conflict_warning_is_caught(
    caplog: pytest.LogCaptureFixture, old: str, new: str
) -> None:
    mutant = mutated_module(evidence_module, (old, new))
    with caplog.at_level(logging.WARNING):
        mutant.build_evidence_set([("x", result(wind(1)), None), ("y", result(wind(2)), None)])
    records = [r for r in caplog.records if r.getMessage() == "conflicting evidence dropped"]
    ok = len(records) == 1 and records[0].__dict__.get("connectors") == ["x", "y"]
    clean = "id" not in records[0].__dict__ if records else False
    assert not (ok and clean), "the mutation was not noticed"
