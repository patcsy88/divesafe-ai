"""WITHHELD evidence (owner decision, risk-reviewer Major on finding 2).

Different items sharing one id are never evaluated. They are kept apart as `withheld` and shown to
the human in the record, because they may hold a MORE SEVERE result than anything evaluated. They
are never in `evidence.items`, never given to the engine, the agents or a prompt, and never
citable. A conflict can hide a NO-GO, so the result is INSUFFICIENT EVIDENCE (pinned below).

Mutations run on COPIES (`mutated_module`) of `divesafe.services.evidence`,
`divesafe.orchestration.pipeline`, `divesafe.domain.models` and `divesafe.api.schemas`, never src.

EQUIVALENT MUTANTS (not tested):
  - the `withheld` list built with a set or a dict instead of `if item not in withheld` (same
    de-duplicated content, and the result is sorted afterwards);
  - `sorted(...)` key written with a different but equivalent tie-break for items that are fully
    equal (they are de-duplicated before sorting);
  - the `withheld` dataclass default `()` written as `tuple()`;
  - the order of the two validator checks in `AssessmentRecord` (both raise ValueError);
  - changing the wording of the two validator messages (only the refusal is asserted, with a
    fragment of the message so a different refusal cannot hide a missing one);
  - dropping the logger `connectors` extra (covered by test_evidence_conflict.py).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.agent import scripted_llm as sl
from tests.api_polling import create_and_poll, started
from tests.conftest import NOW
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, KEY_A, KEY_B, _Clock, _hash
from tests.safety.test_api_human_gate import PLAN as API_PLAN
from tests.safety.test_evidence_conflict import PLAN, _assess, _Source, waves, wind
from tests.safety.test_rule_definitions import defn

from divesafe.api import schemas as schemas_module
from divesafe.api.app import create_app
from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role
from divesafe.api.schemas import AssessmentView, EvidenceView
from divesafe.api.state import (
    INTERIM_REQUIRED_CATEGORIES,
    INTERIM_RULESET_VERSION,
    AppState,
    RunLimits,
)
from divesafe.data import SITES, TIOMAN_ISLAND, FetchResult
from divesafe.domain import (
    AssessmentRecord,
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskFactorKind,
    RuleResult,
)
from divesafe.domain import models as models_module
from divesafe.orchestration import assess_dive
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.risk import DefinitionRule, EvidencePolicy, RiskRulesEngine
from divesafe.services import InMemoryAssessmentRepository, UnstorableRecordError
from divesafe.services import evidence as evidence_module
from divesafe.services.evidence import build_evidence_set
from divesafe.services.postgres import _same, _storable

pytestmark = pytest.mark.safety

R = Recommendation
WIND = DataCategory.WIND
WAVES = DataCategory.WAVES_SWELL
SECRET = 987654
Build = Callable[[Sequence[tuple[str, FetchResult | None, str | None]]], Any]


def result(*items: EvidenceItem) -> FetchResult:
    return FetchResult(tuple(items))


def speeds(items: Sequence[EvidenceItem]) -> list[Any]:
    return sorted(i.value.get("speed", -1) for i in items)


def issue_text(connectors: str, categories: str) -> str:
    return (
        f"evidence.conflict: {connectors} returned different {categories} evidence under one id; "
        "none of it used (withheld, not evaluated; it could hold a more severe result)"
    )


# --- (a) what is withheld: checks that work on the real function and on a mutated copy ---------


def withheld_misses(build: Build) -> list[str]:
    bad: list[str] = []

    def expect(label: str, found: Any, held: list[Any], kept: list[str] | None = None) -> None:
        if speeds(found.withheld) != held:
            bad.append(f"{label}: withheld")
        if kept is not None and sorted(i.id for i in found.items) != kept:
            bad.append(f"{label}: items")

    a, b, c = wind(1), wind(2), wind(3)
    expect("two", build([("x", result(a), None), ("y", result(b), None)]), [1, 2], [])
    expect("reversed", build([("y", result(b), None), ("x", result(a), None)]), [1, 2], [])
    expect(
        "third-equals-first-is-not-listed-twice",
        build([("x", result(a), None), ("y", result(b), None), ("z", result(wind(1)), None)]),
        [1, 2],
        [],
    )
    expect(
        "three-distinct-claims-and-a-repeat",
        build(
            [
                ("x", result(c), None),
                ("y", result(a), None),
                ("z", result(b), None),
                ("w", result(wind(3)), None),
            ]
        ),
        [1, 2, 3],
        [],
    )
    expect("same-connector-twice", build([("x", result(a, b), None)]), [1, 2], [])
    expect(
        "identical-duplicates-withhold-nothing",
        build([("x", result(a), None), ("y", result(wind(1)), None), ("z", result(a), None)]),
        [],
        ["ev-wind"],
    )
    expect(
        "a-non-conflicting-item-is-never-withheld",
        build([("x", result(a, waves(1)), None), ("y", result(b), None)]),
        [1, 2],
        ["ev-waves_swell"],
    )
    # sorted by (category, valid_at, id) whatever the arrival order
    late = wind(7).model_copy(update={"id": "a-late", "valid_at": NOW + timedelta(hours=1)})
    late2 = wind(8).model_copy(update={"id": "a-late", "valid_at": NOW + timedelta(hours=1)})
    early = wind(5).model_copy(update={"id": "b-early"})
    early2 = wind(6).model_copy(update={"id": "b-early"})
    swell = waves(1)
    swell2 = waves(2)
    ordered = build(
        [
            ("x", result(late, early, swell), None),
            ("y", result(late2, early2, swell2), None),
        ]
    )
    keys = [(i.category.value, i.valid_at, i.id) for i in ordered.withheld]
    if keys != sorted(keys) or len(keys) != 6:
        bad.append("sorted: withheld")
    if [i.category.value for i in ordered.withheld][:2] != ["waves_swell", "waves_swell"]:
        bad.append("sorted-by-category: withheld")
    if [i.id for i in ordered.withheld if i.category == WIND][:2] != ["b-early", "b-early"]:
        bad.append("sorted-by-valid-at: withheld")
    return bad


def test_the_real_function_withholds_exactly_the_conflicting_items() -> None:
    found = withheld_misses(build_evidence_set)
    assert found == []


def test_withheld_holds_every_distinct_claim_in_any_order() -> None:
    items = [wind(n) for n in (4, 2, 9, 1)]
    results = [(f"c{n}", result(item), None) for n, item in enumerate(items)]
    forward = build_evidence_set(results)
    backward = build_evidence_set(list(reversed(results)))
    assert speeds(forward.withheld) == [1, 2, 4, 9]
    assert speeds(backward.withheld) == [1, 2, 4, 9]
    assert forward.items == () and backward.items == ()


def test_identical_duplicates_withhold_nothing_and_make_no_issue() -> None:
    found = build_evidence_set([("x", result(wind(1)), None), ("y", result(wind(1)), None)])
    assert found.withheld == ()
    assert found.issues == ()
    assert len(found.items) == 1


def test_the_issue_names_sorted_connectors_and_sorted_categories_and_nothing_else() -> None:
    swell = waves(1).model_copy(update={"id": "shared"})
    blow = wind(1).model_copy(update={"id": "shared"})
    for order in ([("zed", swell), ("abe", blow)], [("abe", blow), ("zed", swell)]):
        found = build_evidence_set([(name, result(item), None) for name, item in order])
        assert found.issues == (issue_text("abe, zed", "waves_swell, wind"),)
        assert "shared" not in found.issues[0]
    # a category that appears in three claims is named once
    three = build_evidence_set(
        [
            ("x", result(blow), None),
            ("y", result(blow.model_copy(update={"value": {"speed": 5}})), None),
            ("z", result(swell), None),
        ]
    )
    assert three.issues == (issue_text("x, y, z", "waves_swell, wind"),)


# --- (b) withheld items reach nothing: engine, agents, prompts, citations ----------------------


class _Recorder:
    rule_id = "synthetic.record"
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        self.seen.extend(i.id for i in evidence)
        return RuleResult(rule_id=self.rule_id, outcome=R.GO, rationale="synthetic")


def fresh(item: EvidenceItem) -> EvidenceItem:
    return item.model_copy(
        update={
            "retrieved_at": sl.NOW - timedelta(minutes=5),
            "valid_at": sl.NOW,
            "valid_until": sl.NOW + timedelta(days=1),
        }
    )


def citing(*ids: str) -> str:
    return json.dumps(
        {
            "recommendation": "CAUTION",
            "rationale": "synthetic rationale",
            "evidence_ids": list(ids),
            "uncertainties": [],
        }
    )


def reaches_nowhere(module: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way a withheld item reaches the record's items, the engine, the agent stage, a prompt
    or a citation. Empty for the real pipeline. `module` is the pipeline (real or a copy)."""
    bad: list[str] = []
    original = module.run_agent_stage

    def run(provider: Any) -> tuple[AssessmentRecord, _Recorder, list[str]]:
        recorder = _Recorder()
        stage_ids: list[str] = []

        async def recording(prov: Any, plan: Any, evidence: Any, rules: Any, det: Any) -> Any:
            stage_ids.extend(i.id for i in evidence)
            return await original(prov, plan, evidence, rules, det)

        monkeypatch.setattr(module, "run_agent_stage", recording)
        engine = RiskRulesEngine(
            [recorder],
            EvidencePolicy(
                frozenset({WAVES}), timedelta(hours=1), degraded_may_support_go=True
            ),  # synthetic
            "synthetic",
            required_factors=frozenset(),
        )
        record = asyncio.run(
            module.assess_dive(
                assessment_id="w-1",
                plan=sl.PLAN,
                site=TIOMAN_ISLAND,
                connectors=[
                    _Source("a", fresh(waves(1)), fresh(wind(SECRET))),
                    _Source("b", fresh(wind(SECRET + 1))),
                ],
                engine=engine,
                now=sl.NOW,
                provider=provider,
                clock=lambda: sl.NOW,
            )
        )
        return record, recorder, stage_ids

    provider = sl.fake()
    record, recorder, stage_ids = run(provider)
    prompt = "\n".join(r.messages[1].content for r in provider.requests)
    if "ev-wind" in recorder.seen:
        bad.append("engine saw a withheld item")
    if "ev-waves_swell" not in recorder.seen:
        bad.append("control: engine saw no evidence")
    if "ev-wind" in stage_ids:
        bad.append("agent stage got a withheld item")
    if "ev-waves_swell" not in stage_ids:
        bad.append("control: agent stage got no evidence")
    for fragment in ("ev-wind", str(SECRET), str(SECRET + 1)):
        if fragment in prompt:
            bad.append(f"prompt holds {fragment}")
    if "ev-waves_swell" not in prompt or not provider.requests:
        bad.append("control: the prompt holds no evidence")
    if "ev-wind" in [i.id for i in record.evidence]:
        bad.append("left in evidence.items")
    if sorted(speeds_of(record.withheld_evidence)) != [SECRET, SECRET + 1]:
        bad.append("record does not carry the withheld items")

    cited, _, _ = run(sl.fake(overrides={"risk_assessment": citing("ev-wind")}))
    if "ev-wind" in cited.proposal_evidence_ids:
        bad.append("a withheld id was citable")
    if not any("unknown evidence id" in issue for issue in cited.agent_issues):
        bad.append("citing a withheld id was not refused by the citation check")
    fine, _, _ = run(sl.fake(overrides={"risk_assessment": citing("ev-waves_swell")}))
    if fine.proposal_evidence_ids != ("ev-waves_swell",):
        bad.append("control: citing an evaluated id was refused")
    return bad


def speeds_of(items: Sequence[EvidenceItem]) -> list[Any]:
    return [i.value.get("speed", -1) for i in items]


def test_withheld_items_reach_neither_the_engine_nor_the_agents_nor_a_prompt_nor_a_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = reaches_nowhere(pipeline_module, monkeypatch)
    assert found == []


# --- (c) PINNED: a conflict can hide a NO-GO, and the result is INSUFFICIENT EVIDENCE -----------

HOUR = timedelta(hours=1)
START = NOW + 2 * HOUR


def height(at_hour: int, value: float) -> EvidenceItem:
    return EvidenceItem(
        id=f"e-{at_hour}",
        category=WAVES,
        source="synthetic",
        retrieved_at=NOW - timedelta(minutes=5),
        valid_at=START + at_hour * HOUR,
        is_forecast=True,
        data_kind=DataKind.MODEL,
        quality=DataQuality.DEGRADED,
        value={"variables": {"wave_height": value}, "units": {"wave_height": "m"}},
    )


def no_go_engine() -> RiskRulesEngine:
    definition = defn(scope={"site_ids": [TIOMAN_ISLAND.id], "max_depth_m": 30})
    return RiskRulesEngine(
        [DefinitionRule(definition)],
        EvidencePolicy(frozenset({WAVES}), HOUR, degraded_may_support_go=True),  # synthetic
        "synthetic",
        required_factors=frozenset({RiskFactorKind.WAVE_HEIGHT}),
    )


def assess_with(connectors: Sequence[Any]) -> AssessmentRecord:
    plan = PLAN.model_copy(
        update={"planned_start": START, "planned_duration_minutes": 60, "max_depth_m": 20}
    )
    return asyncio.run(
        assess_dive(
            assessment_id="n-1",
            plan=plan,
            site=TIOMAN_ISLAND,
            connectors=connectors,
            engine=no_go_engine(),
            now=NOW,
        )
    )


def test_without_a_conflict_the_signed_off_rule_gives_no_go() -> None:
    record = assess_with([_Source("a", height(0, 5.0), height(1, 0.1))])
    assert record.final_recommendation == R.NO_GO  # so the next test proves the conflict did it
    assert record.withheld_evidence == ()


def test_a_conflict_that_hides_the_no_go_gives_insufficient_evidence_and_shows_the_items() -> None:
    hidden = height(0, 5.0)
    record = assess_with(
        [_Source("a", hidden, height(1, 0.1)), _Source("b", height(0, 0.1))],
    )
    assert record.final_recommendation == R.INSUFFICIENT_EVIDENCE  # pinned, NOT NO-GO
    assert record.deterministic_recommendation == R.INSUFFICIENT_EVIDENCE
    assert record.final_recommendation != R.NO_GO
    assert [i.id for i in record.withheld_evidence] == ["e-0", "e-0"]
    assert hidden in record.withheld_evidence  # the human can see the item that would be NO-GO
    assert [i.id for i in record.evidence] == ["e-1"]
    assert record.evidence_issues == (issue_text("a, b", "waves_swell"),)
    assert any(r.rule_id == "evidence.connector_issues" for r in record.rule_results)


# --- (d) AssessmentRecord validation ----------------------------------------------------------


def conflict_record() -> AssessmentRecord:
    return _assess([_Source("a", waves(1), wind(1)), _Source("b", wind(2))])


def clean_record() -> AssessmentRecord:
    return _assess([_Source("a", waves(1), wind(1)), _Source("b", wind(1))])


def doc(record: AssessmentRecord) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(record.model_dump_json())
    return loaded


def refuses(cls: Any, document: dict[str, Any], fragment: str) -> bool:
    try:
        cls.model_validate(document)
    except ValidationError as exc:
        return fragment in str(exc)
    return False


def validation_misses(cls: Any) -> list[str]:
    bad: list[str] = []
    if cls.model_fields["withheld_evidence"].default != ():
        return ["the default is not ()"]
    base = doc(conflict_record())
    clean = doc(clean_record())
    in_both = {**base, "withheld_evidence": [*base["withheld_evidence"], base["evidence"][0]]}
    if not refuses(cls, in_both, "withheld evidence ids must not appear in the evidence"):
        bad.append("a withheld id equal to an evidence id was accepted")
    if not refuses(
        cls,
        {**base, "evidence_issues": []},
        "withheld evidence requires an evidence.conflict issue",
    ):
        bad.append("withheld items without an evidence issue were accepted")
    if cls.model_validate(clean).withheld_evidence != ():
        bad.append("the default is not ()")
    old = {k: v for k, v in clean.items() if k != "withheld_evidence"}
    if cls.model_validate_json(json.dumps(old)) != cls.model_validate(clean):
        bad.append("an old record without the key does not load unchanged")
    again = cls.model_validate_json(json.dumps(base))
    if len(again.withheld_evidence) != 2 or doc(again) != base:
        bad.append("round trip with withheld items changed the record")
    return bad


def test_the_real_record_meets_every_validation_check() -> None:
    found = validation_misses(AssessmentRecord)
    assert found == []


def test_a_withheld_id_equal_to_an_evidence_id_is_refused() -> None:
    base = doc(conflict_record())
    forged = {**base, "withheld_evidence": [base["evidence"][0]]}
    with pytest.raises(ValidationError, match="must not appear in the evidence"):
        AssessmentRecord.model_validate(forged)


def test_withheld_items_without_an_evidence_issue_are_refused() -> None:
    base = doc(conflict_record())
    with pytest.raises(ValidationError, match="requires an evidence.conflict issue"):
        AssessmentRecord.model_validate({**base, "evidence_issues": []})


def test_the_default_is_no_withheld_items() -> None:
    record = clean_record()
    assert record.withheld_evidence == ()
    assert AssessmentRecord.model_fields["withheld_evidence"].default == ()


def test_an_old_stored_record_without_the_key_loads() -> None:
    record = clean_record()
    old = doc(record)
    del old["withheld_evidence"]
    loaded = AssessmentRecord.model_validate_json(json.dumps(old))
    assert loaded == record
    assert loaded.withheld_evidence == ()


def test_a_record_with_withheld_items_round_trips() -> None:
    record = conflict_record()
    assert len(record.withheld_evidence) == 2
    loaded = AssessmentRecord.model_validate_json(record.model_dump_json())
    assert loaded == record
    assert loaded.withheld_evidence == record.withheld_evidence


# --- (e) the API view ---------------------------------------------------------------------------

NOTE_FRAGMENTS = ("NOT used", "more severe", "plain text")


def view_misses(view_cls: Any) -> list[str]:
    bad: list[str] = []
    record = conflict_record()
    view = view_cls.of(record)
    if tuple(view.withheld_evidence) != record.withheld_evidence or not view.withheld_evidence:
        bad.append("view does not carry the withheld items")
    for fragment in NOTE_FRAGMENTS:
        if fragment not in view.withheld_note:
            bad.append(f"withheld_note lacks {fragment!r}")
    dumped = view.model_dump(mode="json")
    if len(dumped["withheld_evidence"]) != 2 or not dumped["withheld_note"]:
        bad.append("serialised view lacks the withheld fields")
    if any(entry["item"]["id"] == "ev-wind" for entry in dumped["evidence"]):
        bad.append("a withheld item is listed among the evaluated evidence")
    return bad


def test_the_real_evidence_view_carries_the_items_and_the_fixed_note() -> None:
    found = view_misses(EvidenceView)
    assert found == []


def test_a_view_without_a_conflict_has_no_withheld_items() -> None:
    view = EvidenceView.of(clean_record())
    assert view.withheld_evidence == ()
    assert "NOT used" in view.withheld_note  # the note is fixed text, present either way


def test_the_assessment_view_embeds_the_record_that_carries_the_withheld_items() -> None:
    # The view embeds a SUBCLASS of the record (scenario/finding fields renamed for clients). It
    # must inherit the withheld field and the validators unchanged.
    embedded = AssessmentView.model_fields["record"].annotation
    assert isinstance(embedded, type)
    assert issubclass(embedded, AssessmentRecord)
    own = embedded.model_fields["withheld_evidence"]
    real = AssessmentRecord.model_fields["withheld_evidence"]
    assert (own.annotation, own.default) == (real.annotation, real.default)
    base = doc(conflict_record())
    with pytest.raises(ValidationError, match="requires an evidence.conflict issue"):
        embedded.model_validate({**base, "evidence_issues": []})


def _client(connectors: Sequence[Any]) -> TestClient:
    clock = _Clock()
    engine = RiskRulesEngine(
        [],
        EvidencePolicy(INTERIM_REQUIRED_CATEGORIES, timedelta(hours=1)),
        INTERIM_RULESET_VERSION,
    )
    state = AppState(
        repository=InMemoryAssessmentRepository(),
        connectors=list(connectors),
        engine=engine,
        authenticator=ApiKeyAuthenticator(
            {
                _hash(KEY_A): KeyEntry("alice", frozenset(Role)),
                _hash(KEY_B): KeyEntry("bob", frozenset(Role)),
            }
        ),
        clock=clock,
        sites=SITES,
        decision_max_age=None,
        provider=None,
        runs=RunLimits(),
    )
    return started(TestClient(create_app(state)))


def test_the_api_shows_withheld_items_in_the_assessment_and_the_evidence_endpoint() -> None:
    client = _client([_Source("a", wind(1)), _Source("b", wind(2))])
    response = create_and_poll(client, API_PLAN, AUTH_A)
    assert response.status_code == 200, response.text
    body = response.json()
    record = body["record"]
    assert len(record["withheld_evidence"]) == 2
    assert record["evidence"] == []
    assert record["final_recommendation"] == "INSUFFICIENT EVIDENCE"
    assert record["evidence_issues"] == [issue_text("a, b", "wind")]
    listed = client.get(f"/v1/assessments/{record['id']}/evidence", headers=AUTH_A)
    assert listed.status_code == 200
    evidence = listed.json()
    assert [i["value"]["speed"] for i in evidence["withheld_evidence"]] == [1, 2]
    assert evidence["evidence"] == []
    for fragment in NOTE_FRAGMENTS:
        assert fragment in evidence["withheld_note"]
    assert evidence["evidence_issues"] == [issue_text("a, b", "wind")]


def test_the_api_shows_an_empty_withheld_list_without_a_conflict() -> None:
    client = _client([_Source("a", wind(1)), _Source("b", wind(1))])
    response = create_and_poll(client, API_PLAN, AUTH_A)
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["withheld_evidence"] == []
    assert len(record["evidence"]) == 1


# --- storage (offline half): the pure functions the PostgreSQL adapter uses ---------------------


def test_the_storage_checks_accept_a_record_with_withheld_items_and_an_old_one() -> None:
    record = conflict_record()
    _storable(record.model_dump())  # raises if the new field is refused
    wire = json.loads(record.model_dump_json())
    assert _same(wire, json.loads(record.model_dump_json()))
    assert AssessmentRecord.model_validate_json(json.dumps(wire)) == record
    old = doc(clean_record())
    del old["withheld_evidence"]
    _storable(old)
    assert AssessmentRecord.model_validate_json(json.dumps(old)) == clean_record()


@pytest.mark.parametrize("bad", ["a\x00b", float("nan"), float("inf")])
def test_the_storage_check_reaches_into_withheld_items(bad: object) -> None:
    record = conflict_record()
    first, second = record.withheld_evidence  # a valid conflict pair: one id, two different items
    tainted = first.model_copy(update={"value": {"speed": bad}})
    payload = {
        **record.model_dump(),
        "withheld_evidence": (tainted.model_dump(), second.model_dump()),
    }
    loaded = AssessmentRecord.model_validate(payload)
    with pytest.raises(UnstorableRecordError):
        _storable(loaded.model_dump())


# --- (f) mutations on copies --------------------------------------------------------------------

HELD = "    held = tuple(sorted(withheld, key=lambda i: (i.category.value, i.valid_at, i.id)))\n"
RETURN = "    return EvidenceSet(ordered, tuple(issues), versions, held)\n"
KEEP = "            continue\n        kept.append(first)\n"
SORTED_CATEGORIES = "categories = sorted({item.category.value for _, item in claimed})"
CATEGORY_TEXT = "f\"{', '.join(categories)} evidence under one id; none of it used \""

EVIDENCE_MUTATIONS = [
    pytest.param(
        (RETURN, "    return EvidenceSet(ordered, tuple(issues), versions, ())\n"),
        id="withheld-not-filled",
    ),
    pytest.param(
        ("                if item not in withheld:\n", "                if True:\n"),
        id="withheld-not-deduplicated",
    ),
    pytest.param((HELD, "    held = tuple(withheld)\n"), id="withheld-not-sorted"),
    pytest.param(
        (
            "            for _, item in claimed:\n                if item not in withheld:",
            "            for _, item in claimed[1:]:\n                if item not in withheld:",
        ),
        id="first-claim-not-withheld",
    ),
    pytest.param(
        (
            KEEP,
            "            kept.append(first)\n            continue\n        kept.append(first)\n",
        ),
        id="withheld-also-left-in-items",
    ),
    pytest.param(
        (
            KEEP,
            "            continue\n        withheld.append(first)\n        kept.append(first)\n",
        ),
        id="non-conflicting-item-withheld",
    ),
    pytest.param(
        (CATEGORY_TEXT, '"evidence under one id; none of it used "'),
        id="category-missing-from-the-issue",
    ),
    pytest.param(
        (
            SORTED_CATEGORIES,
            "categories = list(dict.fromkeys(i.category.value for _, i in claimed))",
        ),
        id="categories-unsorted",
    ),
    pytest.param(
        (SORTED_CATEGORIES, "categories = sorted(i.category.value for _, i in claimed)"),
        id="categories-repeated",
    ),
]


@pytest.mark.parametrize("edit", EVIDENCE_MUTATIONS)
def test_mutation_of_the_withheld_set_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(evidence_module, edit)
    try:
        found = withheld_misses(mutant.build_evidence_set) + category_misses(
            mutant.build_evidence_set
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []


def category_misses(build: Build) -> list[str]:
    """The issue text: categories named, sorted, once each; the real function has none."""
    swell = waves(1).model_copy(update={"id": "shared"})
    blow = wind(1).model_copy(update={"id": "shared"})
    expected = issue_text("x, y, z", "waves_swell, wind")
    bad = []
    for results in (
        [("x", result(blow), None), ("y", result(swell), None), ("z", result(blow), None)],
        [("x", result(swell), None), ("y", result(blow), None), ("z", result(swell), None)],
    ):
        if tuple(build(results).issues) != (expected,):
            bad.append("categories in the issue text")
    return bad


def test_the_real_function_meets_the_category_checks() -> None:
    found = category_misses(build_evidence_set)
    assert found == []


PIPELINE_MUTATIONS = [
    pytest.param(
        (
            "    assessment = engine.assess(plan, evidence.items, now)\n",
            "    assessment = engine.assess(plan, (*evidence.items, *evidence.withheld), now)\n",
        ),
        id="withheld-also-passed-to-the-engine",
    ),
    pytest.param(
        (
            "run_agent_stage(provider, plan, evidence.items, rule_results, deterministic)",
            "run_agent_stage(provider, plan, (*evidence.items, *evidence.withheld), "
            "rule_results, deterministic)",
        ),
        id="withheld-included-in-the-prompt-evidence",
    ),
    pytest.param(
        (
            "        evidence=evidence.items,\n",
            "        evidence=(*evidence.items, *evidence.withheld),\n",
        ),
        id="withheld-also-left-in-the-record-evidence",
    ),
    pytest.param(
        ("        withheld_evidence=evidence.withheld,\n", ""),
        id="withheld-not-recorded",
    ),
]


@pytest.mark.parametrize("edit", PIPELINE_MUTATIONS)
def test_mutation_of_the_pipeline_is_caught(
    edit: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = mutated_module(pipeline_module, edit)
    try:
        found = reaches_nowhere(mutant, monkeypatch)
    except ValidationError:
        # AssessmentRecord refused the record the mutant built: the validator is the second line
        # of defence (a cited or listed id that is not in the evidence). A real refusal.
        found = ["the record validator refused the mutant's record"]
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []


MODEL_MUTATIONS = [
    pytest.param(
        ("        if {e.id for e in self.withheld_evidence} & set(ids):\n", "        if False:\n"),
        id="id-in-both-validator-removed",
    ),
    pytest.param(
        (
            "            if not any("
            's.startswith("evidence.conflict:") for s in self.evidence_issues):\n',
            "            if False:\n",
        ),
        id="issues-required-validator-removed",
    ),
    pytest.param(
        (
            "    withheld_evidence: tuple[EvidenceItem, ...] = Field(\n        default=(),",
            "    withheld_evidence: tuple[EvidenceItem, ...] = Field(\n        default=None,",
        ),
        id="default-not-empty",
    ),
]


@pytest.mark.parametrize("edit", MODEL_MUTATIONS)
def test_mutation_of_the_record_validation_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(models_module, edit)
    try:
        found = validation_misses(mutant.AssessmentRecord)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []


NOTE = '    withheld_note: str = (\n        "The items listed here were NOT used: '
VIEW_MUTATIONS = [
    pytest.param(
        (NOTE.split("\n")[0] + "\n", '    withheld_note: str = "" and (\n'),
        id="withheld-note-removed",
    ),
    pytest.param(("here were NOT used:", "here were used:"), id="note-says-used"),
    pytest.param(
        (
            '"more severe result than anything that was evaluated,',
            '"worse result than anything that was evaluated,',
        ),
        id="note-drops-more-severe",
    ),
    pytest.param(('Read it as plain text only."', 'Read it."'), id="note-drops-plain-text"),
    pytest.param(
        ("            withheld_evidence=record.withheld_evidence,\n", ""), id="view-drops-the-items"
    ),
]


@pytest.mark.parametrize("edit", VIEW_MUTATIONS)
def test_mutation_of_the_evidence_view_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(schemas_module, edit)
    try:
        found = view_misses(mutant.EvidenceView)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []
