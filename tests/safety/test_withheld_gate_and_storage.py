"""Backend follow-ups to WITHHELD evidence: the storage successor rule, the stricter record
validator, attributions that include withheld items, and the fixed 409 for an invalid successor.

Mutations run on COPIES (`mutated_module`) of `divesafe.services.postgres`,
`divesafe.domain.models`, `divesafe.api.schemas` and `divesafe.api.app`, never on src.
A mutant that makes the API raise `InvalidSuccessorError` instead of answering 409 is counted as a
miss (the observable difference is an unhandled error); any other exception fails the test.

EQUIVALENT MUTANTS (not tested):
  - `_same(default, data[name])` written as `==` in `_without_new_defaults`: the compared values
    come from the same field of the same model, so their JSON types cannot differ;
  - removing `or field.is_required()`: a required field has no default (PydanticUndefined) that
    could equal a value, so nothing changes unless it crashes;
  - removing the `isinstance(stored, dict)` early return: the repository only ever passes
    `json.loads` of a stored JSONB object, so the branch is defensive and the mutant only crashes;
  - the order of the `by_id` grouping in the validator, and the wording of its messages (only the
    refusal and a message fragment are asserted);
  - the attribution set built with a list and `dict.fromkeys` instead of a set (the result is
    sorted and de-duplicated either way); a `+ ''` on the cut text (same string);
  - the wording of the 409 log message (only its level, `assessment_id` and the absence of the
    exception text are asserted).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError
from tests.api_polling import create_and_poll, started
from tests.conftest import make_evidence
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, KEY_A, KEY_B, _Clock, _hash
from tests.safety.test_api_human_gate import PLAN as API_PLAN
from tests.safety.test_evidence_conflict import _assess, _Source, waves, wind
from tests.safety.test_evidence_withheld import clean_record, conflict_record, doc

from divesafe.api import app as app_module
from divesafe.api import schemas as schemas_module
from divesafe.api.auth import ApiKeyAuthenticator, KeyEntry, Role
from divesafe.api.schemas import AssessmentView
from divesafe.api.state import (
    INTERIM_REQUIRED_CATEGORIES,
    INTERIM_RULESET_VERSION,
    AppState,
    RunLimits,
)
from divesafe.data import SITES
from divesafe.domain import AssessmentRecord, DataCategory
from divesafe.domain import models as models_module
from divesafe.risk import EvidencePolicy, RiskRulesEngine
from divesafe.services import InMemoryAssessmentRepository, InvalidSuccessorError
from divesafe.services import postgres as postgres_module
from divesafe.services.postgres import _without_new_defaults

pytestmark = pytest.mark.safety

# --- _without_new_defaults ----------------------------------------------------------------------


class Standin(BaseModel):
    """No field name here appears in the real code, so the function cannot be matching names."""

    id: str
    count: int = 3
    tags: tuple[str, ...] = ("t",)
    extra: list[int] = Field(default_factory=lambda: [1])
    note: str | None = None


FULL = {"id": "a", "count": 3, "tags": ["t"], "extra": [1], "note": None}


def standin_misses(mod: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(mod, "AssessmentRecord", Standin)
    fn: Callable[[Any, Any], dict[str, Any]] = mod._without_new_defaults
    bad: list[str] = []

    def expect(label: str, new: Standin, stored: Any, wanted: dict[str, Any]) -> None:
        if fn(new, stored) != wanted:
            bad.append(label)

    expect("absent and default: dropped", Standin(id="a"), {"id": "a"}, {"id": "a"})
    expect(
        "absent and non-default: kept",
        Standin(id="a", count=9, tags=("q",)),
        {"id": "a"},
        {"id": "a", "count": 9, "tags": ["q"]},
    )
    expect(
        "absent and non-default but empty/falsy: kept",
        Standin(id="a", tags=(), count=0, extra=[], note=""),
        {"id": "a"},
        {"id": "a", "count": 0, "tags": [], "extra": [], "note": ""},
    )
    expect("present and default: kept", Standin(id="a"), FULL, FULL)
    expect(
        "only the absent default keys are dropped",
        Standin(id="a"),
        {"id": "a", "count": 3},
        {"id": "a", "count": 3},
    )
    expect("a stored value that is not an object: nothing dropped", Standin(id="a"), None, FULL)
    expect("a stored list: nothing dropped", Standin(id="a"), [], FULL)
    expect("a required key is never dropped", Standin(id="a"), {}, {"id": "a"})
    return bad


def real_misses(mod: Any) -> list[str]:
    fn: Callable[[Any, Any], dict[str, Any]] = mod._without_new_defaults
    bad: list[str] = []
    clean, held = clean_record(), conflict_record()
    full = doc(clean)
    old = {k: v for k, v in full.items() if k != "withheld_evidence"}
    if fn(clean, old) != old:
        bad.append("old row: the default withheld_evidence key was added")
    if fn(clean, full) != full:
        bad.append("new row: the present key was dropped")
    if fn(held, old) != doc(held):
        bad.append("old row, non-default withheld items: dropped")
    if AssessmentRecord.model_validate_json(json.dumps(fn(clean, old))) != clean:
        bad.append("the omitted key does not load as the default")
    return bad


def test_the_real_function_meets_every_stand_in_check(monkeypatch: pytest.MonkeyPatch) -> None:
    found = standin_misses(postgres_module, monkeypatch)
    assert found == []


def test_the_real_function_meets_every_real_record_check() -> None:
    found = real_misses(postgres_module)
    assert found == []


def test_an_old_row_successor_gains_no_new_default_key_but_keeps_everything_else() -> None:
    clean = clean_record()
    old = {k: v for k, v in doc(clean).items() if k != "withheld_evidence"}
    intended = _without_new_defaults(clean, old)
    assert "withheld_evidence" not in intended
    assert intended.keys() == old.keys()


WRITE_EDITS = [
    pytest.param(("            del data[name]\n", "            pass\n"), id="omit-logic-removed"),
    pytest.param(
        ("        if _same(default, data[name]):\n", "        if True:\n"), id="key-always-dropped"
    ),
    pytest.param(
        (
            "        if name in stored or name not in data or field.is_required():\n",
            "        if name not in data or field.is_required():\n",
        ),
        id="present-key-dropped",
    ),
    pytest.param(
        (
            "        if name in stored or name not in data or field.is_required():\n",
            "        if name in stored or name not in data or field.is_required() or True:\n",
        ),
        id="nothing-ever-dropped",
    ),
]


@pytest.mark.parametrize("edit", WRITE_EDITS)
def test_mutation_of_without_new_defaults_is_caught(
    edit: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mutant = mutated_module(postgres_module, edit)
    try:
        found = real_misses(mutant)  # before the stand-in patch replaces AssessmentRecord
        found += standin_misses(mutant, monkeypatch)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []


# --- the record validator -----------------------------------------------------------------------


def accepted(cls: Any, base: dict[str, Any], **changes: Any) -> bool:
    try:
        cls.model_validate({**base, **changes})
    except ValidationError:
        return False
    return True


def rule_misses(cls: Any) -> list[str]:
    bad: list[str] = []
    base = doc(conflict_record())
    pair = base["withheld_evidence"]
    if len(pair) != 2 or pair[0]["id"] != pair[1]["id"] or pair[0] == pair[1]:
        return ["the fixture is not a conflict pair"]
    if not accepted(cls, base):
        bad.append("a valid conflict pair was refused")
    connector = "a: ConnectorTransportError: HTTP 503"
    if accepted(cls, base, evidence_issues=[connector]):
        bad.append("withheld items with no evidence.conflict issue were accepted")
    if accepted(cls, base, evidence_issues=[]):
        bad.append("withheld items with no issue at all were accepted")
    if accepted(cls, base, evidence_issues=["note: evidence.conflict: forged"]):
        bad.append("a substring of the prefix was taken for the issue")
    if not accepted(cls, base, evidence_issues=[connector, *base["evidence_issues"]]):
        bad.append("a conflict issue after another issue was refused")
    if accepted(cls, base, withheld_evidence=pair[:1]):
        bad.append("an id on a single withheld item was accepted")
    if accepted(cls, base, withheld_evidence=[pair[0], pair[0]]):
        bad.append("an id on two equal items was accepted")
    other = {**pair[0], "id": "ev-other"}
    if accepted(cls, base, withheld_evidence=[*pair, other]):
        bad.append("a second id on a single item hid behind a valid pair")
    if not accepted(cls, base, withheld_evidence=[pair[0], pair[0], pair[1]]):
        bad.append("three claims, two equal and one different, were refused")
    if not accepted(cls, doc(clean_record())):
        bad.append("a record with no withheld items was refused")
    return bad


def test_the_real_validator_meets_every_rule() -> None:
    found = rule_misses(AssessmentRecord)
    assert found == []


def test_a_valid_pair_is_accepted_and_each_broken_rule_is_refused_with_its_message() -> None:
    base = doc(conflict_record())
    pair = base["withheld_evidence"]
    assert accepted(AssessmentRecord, base)
    with pytest.raises(ValidationError, match="requires an evidence.conflict issue"):
        AssessmentRecord.model_validate({**base, "evidence_issues": ["x: failed"]})
    for broken in (pair[:1], [pair[0], pair[0]]):
        with pytest.raises(ValidationError, match="at least two different items"):
            AssessmentRecord.model_validate({**base, "withheld_evidence": broken})


RULE_EDITS = [
    pytest.param(
        ('s.startswith("evidence.conflict:")', 's.startswith("")'), id="conflict-prefix-removed"
    ),
    pytest.param(
        ('s.startswith("evidence.conflict:") for s in', '"evidence.conflict:" in s for s in'),
        id="conflict-prefix-matched-anywhere",
    ),
    pytest.param(
        (
            "            if any(all(x == items[0] for x in items) for items in by_id.values()):\n",
            "            if False:\n",
        ),
        id="two-different-rule-removed",
    ),
    pytest.param(
        ("all(x == items[0] for x in items)", "len(items) < 2"),
        id="two-different-rule-counts-only",
    ),
    pytest.param(
        ("if any(all(x == items[0]", "if all(all(x == items[0]"),
        id="two-different-rule-needs-every-id-to-fail",
    ),
]


@pytest.mark.parametrize("edit", RULE_EDITS)
def test_mutation_of_the_validator_rules_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(models_module, edit)
    try:
        found = rule_misses(mutant.AssessmentRecord)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []


# --- attributions include withheld items --------------------------------------------------------

ATTR_A = "Weather data by Open-Meteo.com (CC BY 4.0), https://open-meteo.com/"
ATTR_B = "Marine data by Open-Meteo.com, synthetic second attribution"


def attributed_record() -> AssessmentRecord:
    first = make_evidence(DataCategory.WIND, speed=1, attribution=ATTR_A)
    second = make_evidence(DataCategory.WIND, speed=2, attribution=ATTR_B)
    return _assess([_Source("a", waves(1), first), _Source("b", second)])


def attribution_misses(view_cls: Any) -> list[str]:
    record = attributed_record()
    bad = []
    if record.withheld_evidence == () or any("attribution" in e.value for e in record.evidence):
        return ["the fixture does not hold the attributions only in withheld items"]
    shown = view_cls.of(record).attributions
    if tuple(shown) != tuple(sorted([ATTR_A, ATTR_B])):
        bad.append(f"attributions of withheld items are missing: {shown!r}")
    return bad


def test_the_real_view_credits_a_withheld_open_meteo_item() -> None:
    found = attribution_misses(AssessmentView)
    assert found == []


def test_the_attributions_are_sorted_and_not_repeated_for_evaluated_and_withheld_items() -> None:
    kept = make_evidence(DataCategory.CURRENTS, v=1, attribution=ATTR_A)
    first = make_evidence(DataCategory.WIND, speed=1, attribution=ATTR_A)
    second = make_evidence(DataCategory.WIND, speed=2, attribution=ATTR_A)
    record = _assess([_Source("a", waves(1), kept, first), _Source("b", second)])
    assert AssessmentView.of(record).attributions == (ATTR_A,)


def test_a_record_with_no_attribution_anywhere_lists_none() -> None:
    assert AssessmentView.of(conflict_record()).attributions == ()


def test_mutation_attributions_without_withheld_items_is_caught() -> None:
    mutant = mutated_module(
        schemas_module,
        ("for e in (*record.evidence, *record.withheld_evidence)", "for e in record.evidence"),
    )
    found = attribution_misses(mutant.AssessmentView)
    assert found != []


# --- attribution values: only text, stripped, capped, de-duplicated, sorted ---------------------

CAP = 300
CATEGORIES = (
    DataCategory.WIND,
    DataCategory.CURRENTS,
    DataCategory.SEA_TEMPERATURE,
    DataCategory.TIDES,
    DataCategory.WAVES_SWELL,
)


def shown_for(*values: object, withheld: Sequence[object] = ()) -> tuple[str, ...]:
    """The attributions of a record whose evaluated items carry `values` (one category each) and
    whose withheld pair (a conflict on another category) carries `withheld`."""
    items = [
        make_evidence(category, speed=n, attribution=value)
        for n, (category, value) in enumerate(zip(CATEGORIES, values, strict=False))
    ]
    sources = [_Source("a", *items)]
    if withheld:
        first = make_evidence(DataCategory.MARINE_WARNINGS, n=1, attribution=withheld[0])
        second = make_evidence(DataCategory.MARINE_WARNINGS, n=2, attribution=withheld[-1])
        sources = [_Source("a", *items, first), _Source("b", second)]
    return tuple(AssessmentView.of(_assess(sources)).attributions)


def cap_misses(view_cls: Any) -> list[str]:
    def shown(*values: object, withheld: Sequence[object] = ()) -> tuple[str, ...]:
        items = [
            make_evidence(category, speed=n, attribution=value)
            for n, (category, value) in enumerate(zip(CATEGORIES, values, strict=False))
        ]
        sources = [_Source("a", *items)]
        if withheld:
            one = make_evidence(DataCategory.MARINE_WARNINGS, n=1, attribution=withheld[0])
            two = make_evidence(DataCategory.MARINE_WARNINGS, n=2, attribution=withheld[-1])
            sources = [_Source("a", *items, one), _Source("b", two)]
        return tuple(view_cls.of(_assess(sources)).attributions)

    bad: list[str] = []
    if shown(123, ["x"], {"k": "v"}, None, True) != ():
        bad.append("a value that is not text was accepted")
    if shown("   ", "\n\t", "") != ():
        bad.append("a blank value was listed")
    if shown("  padded  ") != ("padded",):
        bad.append("the value was not stripped")
    if shown("b text", "a text", "c text") != ("a text", "b text", "c text"):
        bad.append("short values were changed or not sorted")
    if shown("same", "same") != ("same",):
        bad.append("equal values were listed twice")
    long = "x" * (CAP + 50)
    if shown(long) != ("x" * CAP,):
        bad.append("an over-long value was not cut to exactly the cap")
    if shown("y" * CAP) != ("y" * CAP,):
        bad.append("a value of exactly the cap was changed")
    if shown("z" * (CAP - 1)) != ("z" * (CAP - 1),):
        bad.append("a value under the cap was changed")
    if shown("k" * CAP + "one", "k" * CAP + "two") != ("k" * CAP,):
        bad.append("long values equal after the cut were not de-duplicated")
    if shown("  " + "s" * (CAP + 10)) != ("s" * CAP,):
        bad.append("the value was cut before it was stripped")
    if shown(withheld=["only held", "only held"]) != ("only held",):
        bad.append("an attribution found only on withheld items was not credited")
    if shown(withheld=[123, "held text"]) != ("held text",):
        bad.append("a non-text value on a withheld item was accepted")
    if shown("mixed", withheld=["held", "held"]) != ("held", "mixed"):
        bad.append("evaluated and withheld values were not merged and sorted")
    return bad


def test_the_real_view_meets_every_attribution_rule() -> None:
    found = cap_misses(AssessmentView)
    assert found == []


def test_non_text_attribution_values_are_ignored_not_converted() -> None:
    assert shown_for(123, ["a list"], {"k": "v"}, None, 4.5) == ()


def test_blank_attribution_values_are_ignored() -> None:
    assert shown_for("", "   ", "\n\t ") == ()


def test_an_over_long_attribution_is_cut_to_exactly_300_characters() -> None:
    (cut,) = shown_for("q" * 1000)
    assert cut == "q" * CAP
    assert len(cut) == 300


def test_long_attributions_that_become_equal_after_the_cut_are_listed_once() -> None:
    shown = shown_for("w" * CAP + "-first", "w" * CAP + "-second", "w" * CAP + "-third")
    assert shown == ("w" * CAP,)


def test_an_attribution_found_only_on_withheld_items_is_still_credited() -> None:
    record = _assess(
        [
            _Source("a", make_evidence(DataCategory.MARINE_WARNINGS, n=1, attribution=ATTR_A)),
            _Source("b", make_evidence(DataCategory.MARINE_WARNINGS, n=2, attribution=ATTR_B)),
        ]
    )
    assert not any("attribution" in e.value for e in record.evidence)
    assert AssessmentView.of(record).attributions == tuple(sorted([ATTR_A, ATTR_B]))


def test_ordinary_short_attributions_are_unchanged_and_sorted() -> None:
    assert shown_for("zeta source", "alpha source", ATTR_A) == tuple(
        sorted(["zeta source", "alpha source", ATTR_A])
    )


ATTR_EDITS = [
    pytest.param(("text[:_MAX_ATTRIBUTION_CHARS]", "text"), id="cap-removed"),
    pytest.param(
        ("text[:_MAX_ATTRIBUTION_CHARS]", "text[:_MAX_ATTRIBUTION_CHARS + 1]"), id="cap-off-by-one"
    ),
    pytest.param(
        ("text[:_MAX_ATTRIBUTION_CHARS]", "raw[:_MAX_ATTRIBUTION_CHARS]"), id="cut-before-strip"
    ),
    pytest.param(
        (
            'if isinstance(raw := e.value.get("attribution"), str) and (text := raw.strip())',
            'if (raw := e.value.get("attribution")) is not None and (text := str(raw).strip())',
        ),
        id="non-text-accepted-via-str",
    ),
    pytest.param(
        ("for e in (*record.evidence, *record.withheld_evidence)", "for e in record.evidence"),
        id="withheld-items-excluded",
    ),
    pytest.param(("and (text := raw.strip())", "and (text := raw)"), id="strip-removed"),
    pytest.param(
        ("and (text := raw.strip())", "and (text := raw.strip() or ' ')"), id="blank-kept"
    ),
]


@pytest.mark.parametrize("edit", ATTR_EDITS)
def test_mutation_of_the_attribution_rules_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(schemas_module, edit)
    try:
        found = cap_misses(mutant.AssessmentView)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"the mutant crashed instead of being refused: {type(exc).__name__}")
    assert found != []


# --- the API: an invalid successor is a fixed 409 -----------------------------------------------

FIXED = "the assessment could not be updated; reload it"
SECRET = "internal detail: field 'secret_field' may not change"


class _Flaky(InMemoryAssessmentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    def replace(self, expected: AssessmentRecord, new: AssessmentRecord) -> None:
        if self.fail_next:
            raise InvalidSuccessorError(SECRET)
        super().replace(expected, new)


def _api(
    connectors: Sequence[Any], repo: InMemoryAssessmentRepository | None = None, mod: Any = None
) -> tuple[TestClient, _Clock, InMemoryAssessmentRepository]:
    clock, store = _Clock(), repo if repo is not None else InMemoryAssessmentRepository()
    engine = RiskRulesEngine(
        [],
        EvidencePolicy(INTERIM_REQUIRED_CATEGORIES, timedelta(hours=1)),
        INTERIM_RULESET_VERSION,
    )
    state = AppState(
        repository=store,
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
    build = (mod or app_module).create_app
    return started(TestClient(build(state))), clock, store


DECISION = {"decision": "INSUFFICIENT EVIDENCE"}
ACTUAL = {"observations": {"vis": "ok"}}


class _Capture(logging.Handler):
    """Starting the app calls `configure_logging`, which replaces the root handlers (and so
    caplog's), so the logs are collected on the `divesafe` logger itself, mutant copies included."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def gate_misses(mod: Any) -> list[str]:
    bad: list[str] = []
    for step in ("decision", "actual-conditions"):
        repo = _Flaky()
        client, clock, _ = _api([_Source("a", wind(1)), _Source("b", wind(2))], repo, mod)
        created = create_and_poll(client, API_PLAN, AUTH_A)
        aid = created.json()["record"]["id"]
        if step == "actual-conditions":
            decided = client.post(f"/v1/assessments/{aid}/decision", json=DECISION, headers=AUTH_A)
            if decided.status_code != 200:
                return [f"control: the decision was refused ({decided.status_code})"]
            clock.now += timedelta(hours=3)
        repo.fail_next = True
        capture = _Capture()
        package_logger = logging.getLogger("divesafe")
        package_logger.addHandler(capture)
        previous = package_logger.level
        package_logger.setLevel(logging.DEBUG)
        body = DECISION if step == "decision" else ACTUAL
        try:
            response = client.post(f"/v1/assessments/{aid}/{step}", json=body, headers=AUTH_A)
        except InvalidSuccessorError:
            bad.append(f"{step}: the error escaped instead of a 409")
            continue
        finally:
            package_logger.removeHandler(capture)
            package_logger.setLevel(previous)
        if response.status_code != 409:
            bad.append(f"{step}: status {response.status_code}")
        if response.json().get("detail") != FIXED:
            bad.append(f"{step}: the detail is not the fixed message")
        if "secret_field" in response.text or "internal" in response.text:
            bad.append(f"{step}: internals in the body")
        refusals = [r for r in capture.records if r.levelno >= logging.ERROR]
        if len(refusals) != 1:
            bad.append(f"{step}: expected one error log, saw {len(refusals)}")
        for record in refusals:
            if record.__dict__.get("assessment_id") != aid:
                bad.append(f"{step}: the log lacks the assessment id")
            if any("secret_field" in str(v) for v in record.__dict__.values()):
                bad.append(f"{step}: internals in the log")
            if record.exc_info:
                bad.append(f"{step}: a traceback was logged")
        if any("secret_field" in r.getMessage() for r in capture.records):
            bad.append(f"{step}: internals in the log text")
        repo.fail_next = False
        stored = repo.get(aid)
        if stored is None or (step == "decision") != (stored.human_decision is None):
            bad.append(f"{step}: the stored record changed")
    return bad


def test_the_real_api_answers_a_fixed_409_and_leaks_nothing() -> None:
    found = gate_misses(app_module)
    assert found == []


def test_the_api_shows_the_attributions_of_withheld_items() -> None:
    first = make_evidence(DataCategory.WIND, speed=1, attribution=ATTR_A)
    second = make_evidence(DataCategory.WIND, speed=2, attribution=ATTR_B)
    client, _, _ = _api([_Source("a", first), _Source("b", second)])
    body = create_and_poll(client, API_PLAN, AUTH_A).json()
    assert body["record"]["evidence"] == []
    assert body["attributions"] == sorted([ATTR_A, ATTR_B])


DECISION_BLOCK = (
    "        except InvalidSuccessorError as exc:\n"
    "            logger.error(\n"
    '                "decision refused: invalid successor", '
    'extra={"assessment_id": assessment_id}\n'
    "            )\n"
    "            raise HTTPException(status_code=409, detail=_NOT_UPDATABLE) from exc\n"
)
ACTUAL_BLOCK = (
    "        except InvalidSuccessorError as exc:\n"
    "            logger.error(\n"
    '                "actual conditions refused: invalid successor",\n'
    '                extra={"assessment_id": assessment_id},\n'
    "            )\n"
    "            raise HTTPException(status_code=409, detail=_NOT_UPDATABLE) from exc\n"
)


def _without_mapping(block: str) -> str:
    return block.replace("except InvalidSuccessorError as exc:", "except KeyboardInterrupt as exc:")


def _echoing(block: str) -> str:
    return block.replace("detail=_NOT_UPDATABLE", "detail=str(exc)")


def _logging_the_error(block: str) -> str:
    return block.replace(
        'extra={"assessment_id": assessment_id}', 'extra={"assessment_id": assessment_id, "e": exc}'
    )


def _without_id(block: str) -> str:
    return block.replace('extra={"assessment_id": assessment_id}', "extra={}")


API_EDITS = [
    pytest.param((DECISION_BLOCK, _without_mapping(DECISION_BLOCK)), id="decision-409-removed"),
    pytest.param((ACTUAL_BLOCK, _without_mapping(ACTUAL_BLOCK)), id="actual-409-removed"),
    pytest.param((DECISION_BLOCK, _echoing(DECISION_BLOCK)), id="decision-message-echoes-error"),
    pytest.param((ACTUAL_BLOCK, _echoing(ACTUAL_BLOCK)), id="actual-message-echoes-error"),
    pytest.param(
        (DECISION_BLOCK, _logging_the_error(DECISION_BLOCK)), id="decision-log-holds-the-error"
    ),
    pytest.param((ACTUAL_BLOCK, _logging_the_error(ACTUAL_BLOCK)), id="actual-log-holds-the-error"),
    pytest.param((DECISION_BLOCK, _without_id(DECISION_BLOCK)), id="decision-log-without-id"),
    pytest.param((ACTUAL_BLOCK, _without_id(ACTUAL_BLOCK)), id="actual-log-without-id"),
    pytest.param(
        (DECISION_BLOCK, DECISION_BLOCK.replace("logger.error(", "logger.debug(")),
        id="decision-not-logged-as-an-error",
    ),
    pytest.param(
        (ACTUAL_BLOCK, ACTUAL_BLOCK.replace("logger.error(", "logger.debug(")),
        id="actual-not-logged-as-an-error",
    ),
]


@pytest.mark.parametrize("edit", API_EDITS)
def test_mutation_of_the_409_mapping_is_caught(edit: tuple[str, str]) -> None:
    mutant = mutated_module(app_module, edit)
    found = gate_misses(mutant)
    assert found != []
