"""Code Review finding 3: clients never see a field called `confidence` on scenarios or findings.

The stored form is unchanged (`Scenario.confidence`, `Finding.confidence`). `AssessmentView.record`
is an `AssessmentRecordView` whose scenarios and findings carry `uncalibrated_self_estimate` (same
value and bounds) and no `confidence`. The record-level `confidence` (None until a documented
method exists) and `confidence_note` stay.

Mutations run on COPIES of `divesafe.api.schemas` (`mutated_module`). A mutant that makes the view
models refuse its own data (pydantic `ValidationError`, `extra="forbid"` or the bounds) is caught by
that validator; any other exception is a crash and does not count.

EQUIVALENT MUTANTS (not tested):
  - removing `frozen=True` from the view models (immutability is not part of this finding);
  - the order of keys in `_renamed`'s output dict, and the wording of the description beyond the
    phrases asserted ("NOT a confidence", "not a measure of dive safety");
  - `dict(item)` instead of the dict comprehension when the key is then overwritten (same dict).
"""

from __future__ import annotations

import json
import math
import warnings
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from tests.agent.scripted_llm import fake, run_pipeline, task_of
from tests.api_polling import create_and_poll
from tests.safety.kb_fakes import mutated_module
from tests.safety.test_api_human_gate import AUTH_A, PLAN, _build, _hash

from divesafe.agents.prompts import agent_view
from divesafe.api import schemas as schemas_module
from divesafe.api.auth import KeyEntry, Role
from divesafe.api.schemas import AssessmentRecordView, AssessmentView, FindingView, ScenarioView
from divesafe.domain import AssessmentRecord
from divesafe.services import RepositoryIntegrityError
from divesafe.services.postgres import _same, _storable

pytestmark = pytest.mark.safety

PRESENT_REFUSAL = "the record could not be presented"
SCENARIO_VALUES = (0.11, 0.0, 1.0)
FINDING_VALUES = (0.7, 0.0, 1.0, 0.25)


def varied_record() -> AssessmentRecord:
    """A real pipeline record whose estimates are all different, including 0.0 and 1.0, plus a
    finding with no estimate (None) and a no_evidence finding."""
    base = run_pipeline(fake())
    data = json.loads(base.model_dump_json())
    for item, value in zip(data["scenarios"], SCENARIO_VALUES, strict=True):
        item["confidence"] = value
    plain = [f for f in data["findings"] if not f["no_evidence"]]
    for item, value in zip(plain, FINDING_VALUES, strict=False):
        item["confidence"] = value
    plain[-1]["confidence"] = None  # an evidence-backed finding that gave no estimate
    return AssessmentRecord.model_validate(data)


def confidence_paths(value: Any, path: str = "") -> list[str]:
    """Every place a dict key is exactly `confidence`, found by walking the whole JSON."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}.{key}" if path else str(key)
            if key == "confidence":
                found.append(here)
            found += confidence_paths(child, here)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found += confidence_paths(child, f"{path}[{index}]")
    return found


def shown(view: Any) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # a mutant may hold a record of the wrong type
        out: dict[str, Any] = json.loads(view.model_dump_json())
    return out


def view_misses(body: dict[str, Any], stored: AssessmentRecord, where: str) -> list[str]:
    """What is wrong with one serialised AssessmentView, given the domain record behind it."""
    bad: list[str] = []
    record = body.get("record")
    if not isinstance(record, dict):
        return [f"{where}: no record"]
    for key, items in (("scenarios", stored.scenarios), ("findings", stored.findings)):
        got = record.get(key)
        if not isinstance(got, list) or len(got) != len(items):
            bad.append(f"{where}: {key} do not match the record")
            continue
        for index, (out, domain) in enumerate(zip(got, items, strict=True)):
            if "uncalibrated_self_estimate" not in out:
                bad.append(f"{where}: {key}[{index}] has no uncalibrated_self_estimate")
            elif out["uncalibrated_self_estimate"] != domain.confidence:
                bad.append(f"{where}: {key}[{index}] value {out['uncalibrated_self_estimate']!r}")
            elif type(out["uncalibrated_self_estimate"]) is not type(domain.confidence):
                bad.append(f"{where}: {key}[{index}] value changed type")
        bad += [f"{where}: nested key {p}" for p in confidence_paths(got, key)]
    paths = confidence_paths(body)
    if paths != ["record.confidence"]:
        bad.append(f"{where}: confidence keys at {paths}")
    if record.get("confidence", "missing") is not None:
        bad.append(f"{where}: record-level confidence is not None")
    note = body.get("confidence_note")
    if not isinstance(note, str) or not note:
        bad.append(f"{where}: confidence_note missing")
    return bad


def schema_misses(mod: Any) -> list[str]:
    """(d) on the model schemas of `mod` (the OpenAPI components are built from these)."""
    bad: list[str] = []
    for name in ("ScenarioView", "FindingView"):
        schema = getattr(mod, name).model_json_schema()
        props = schema["properties"]
        if "confidence" in props:
            bad.append(f"{name} has a confidence property")
        estimate = props.get("uncalibrated_self_estimate")
        if estimate is None:
            bad.append(f"{name} has no uncalibrated_self_estimate")
            continue
        text = estimate.get("description", "")
        if "NOT a confidence" not in text or "not a measure of dive safety" not in text:
            bad.append(f"{name}: description does not say it is not a confidence")
    return bad


def estimate_data(kind: str, value: Any) -> dict[str, Any]:
    if kind == "scenario":
        return {"kind": "marginal", "summary": "s", "evidence_ids": ["e"], **value}
    return {"agent": "a", "summary": "s", **value}


def bound_misses(mod: Any) -> list[str]:
    """(c) 0..1 on both view models; 1.5, -0.1 and NaN refused, 0.0 and 1.0 kept."""
    bad: list[str] = []
    for kind, cls in (("scenario", mod.ScenarioView), ("finding", mod.FindingView)):
        for value in (1.5, -0.1, math.nan, math.inf, 1.0000001):
            try:
                cls(**estimate_data(kind, {"uncalibrated_self_estimate": value}))
                bad.append(f"{kind}: {value!r} was accepted")
            except ValidationError:
                pass
        for value in (0.0, 1.0, 0.5):
            built = cls(**estimate_data(kind, {"uncalibrated_self_estimate": value}))
            if built.uncalibrated_self_estimate != value:
                bad.append(f"{kind}: {value!r} was changed")
        try:
            cls(**estimate_data(kind, {"uncalibrated_self_estimate": 0.5, "confidence": 0.5}))
            bad.append(f"{kind}: a field called confidence was accepted")
        except ValidationError:
            pass
    if mod.FindingView(**estimate_data("finding", {})).uncalibrated_self_estimate is not None:
        bad.append("a finding without an estimate does not default to None")
    return bad


def build_misses(mod: Any) -> list[str]:
    """(a)(b) of() and withheld() on a record with varied values, via the module `mod`."""
    stored = varied_record()
    bad: list[str] = []
    bad += view_misses(shown(mod.AssessmentView.of(stored)), stored, "of")
    bad += view_misses(shown(mod.AssessmentView.withheld(stored)), stored, "withheld")
    data = json.loads(stored.model_dump_json())
    data["human_decision"] = {
        "decided_by": "alice",
        "decision": data["final_recommendation"],
        "decided_at": data["created_at"],
        "is_override": False,
    }
    decided = AssessmentRecord.model_validate(data)
    hidden = shown(mod.AssessmentView.withheld(decided))
    if hidden["record"]["human_decision"] is not None or hidden["status"] != "DECISION_WITHHELD":
        bad.append("withheld() on a decided record shows the decision")
    body = shown(mod.AssessmentView.of(stored))
    findings = body["record"]["findings"]
    if [f["uncalibrated_self_estimate"] for f in findings if f["no_evidence"]] != [None]:
        bad.append("a no_evidence finding does not show null")
    if None not in [f["uncalibrated_self_estimate"] for f in findings if not f["no_evidence"]]:
        bad.append("an evidence-backed finding with no estimate does not show null")
    return bad


# --- the real code ------------------------------------------------------------------------------


def test_the_fixture_has_every_case_it_needs() -> None:
    record = varied_record()
    assert [s.confidence for s in record.scenarios] == list(SCENARIO_VALUES)
    values = [f.confidence for f in record.findings]
    assert {0.0, 1.0, None} <= set(values)
    assert any(f.no_evidence for f in record.findings)
    assert len({v for v in values if v is not None}) >= 3


def test_of_and_withheld_show_the_self_estimate_and_no_nested_confidence() -> None:
    found = build_misses(schemas_module)
    assert found == []


def test_the_view_record_is_the_view_subclass_and_keeps_the_record_level_fields() -> None:
    stored = varied_record()
    view = AssessmentView.of(stored)
    assert type(view.record) is AssessmentRecordView
    assert isinstance(view.record, AssessmentRecord)
    assert view.record.confidence is None
    assert view.confidence_note.startswith("Confidence was not computed")
    withheld = AssessmentView.withheld(stored)
    assert type(withheld.record) is AssessmentRecordView
    assert withheld.status == "DECISION_WITHHELD"


def test_a_record_level_confidence_with_a_method_is_still_shown_at_the_record_level() -> None:
    data = json.loads(varied_record().model_dump_json())
    data["confidence"], data["confidence_method"] = 0.9, "documented-method-v1"
    body = shown(AssessmentView.of(AssessmentRecord.model_validate(data)))
    assert body["record"]["confidence"] == 0.9
    assert confidence_paths(body) == ["record.confidence"]


def test_the_view_bounds_are_enforced() -> None:
    found = bound_misses(schemas_module)
    assert found == []


def test_the_view_models_have_no_confidence_field() -> None:
    assert "confidence" not in ScenarioView.model_fields
    assert "confidence" not in FindingView.model_fields
    assert "confidence" in AssessmentRecordView.model_fields  # the record-level one


# --- (d) OpenAPI --------------------------------------------------------------------------------


def test_the_model_schemas_describe_the_estimate_as_not_a_confidence() -> None:
    found = schema_misses(schemas_module)
    assert found == []


def test_the_openapi_document_has_one_confidence_property_the_record_level_one() -> None:
    client, _, _ = _build()
    spec = client.app.openapi()
    schemas = spec["components"]["schemas"]
    for name in ("ScenarioView", "FindingView"):
        props = schemas[name]["properties"]
        assert "confidence" not in props
        assert "NOT a confidence" in props["uncalibrated_self_estimate"]["description"]
    holders = sorted(n for n, s in schemas.items() if "confidence" in s.get("properties", {}))
    assert holders, "the record-level confidence property disappeared"
    for name in holders:
        assert name.startswith("AssessmentRecord"), name  # only the record-level one
    view_props = (
        schemas["AssessmentRecordView-Output"]["properties"]
        if ("AssessmentRecordView-Output" in schemas)
        else schemas["AssessmentRecordView"]["properties"]
    )
    assert "confidence" in view_props
    assert "scenarios" in view_props and "findings" in view_props
    refs = json.dumps([view_props["scenarios"], view_props["findings"]])
    assert "ScenarioView" in refs and "FindingView" in refs
    assert '#/components/schemas/Scenario"' not in refs
    assert '#/components/schemas/Finding"' not in refs


# --- (a) end to end through the app -------------------------------------------------------------

DECISION = {"decision": "INSUFFICIENT EVIDENCE"}


def stored_of(client: Any, repo: Any, body: dict[str, Any]) -> AssessmentRecord:
    record = repo.get(body["record"]["id"])
    assert isinstance(record, AssessmentRecord)
    assert type(record) is AssessmentRecord  # what is stored is the domain record, never a view
    return record


def api_misses() -> list[str]:
    bad: list[str] = []
    client, clock, repo = _build(provider=fake())
    key = {**AUTH_A, "Idempotency-Key": "view-estimate-key-0123456789"}
    created = create_and_poll(client, PLAN, key)
    if created.status_code != 200:
        return [f"control: create/poll answered {created.status_code}"]
    body = created.json()
    stored = stored_of(client, repo, body)
    if not stored.scenarios or not any(f.confidence is not None for f in stored.findings):
        return ["control: the pipeline record has no estimates"]
    rid = body["record"]["id"]
    bad += view_misses(body, stored, "create+poll")
    first = client.get(f"/v1/assessments/{rid}", headers=AUTH_A)
    bad += view_misses(first.json(), stored, "GET")
    again = client.post("/v1/assessments", json=PLAN, headers=key)
    if again.status_code != 200 or again.headers.get("Idempotent-Replay") != "true":
        bad.append(f"control: create replay answered {again.status_code}")
    bad += view_misses(again.json(), stored, "create replay")
    dkey = {**AUTH_A, "Idempotency-Key": "decision-key-0123456789"}
    decided = client.post(f"/v1/assessments/{rid}/decision", json=DECISION, headers=dkey)
    if decided.status_code != 200:
        return [*bad, f"control: decision answered {decided.status_code}"]
    bad += view_misses(decided.json(), stored, "decision")
    replay = client.post(f"/v1/assessments/{rid}/decision", json=DECISION, headers=dkey)
    if replay.headers.get("Idempotent-Replay") != "true":
        bad.append("control: the decision replay was not marked as a replay")
    if replay.status_code != 200:
        bad.append(f"control: decision replay answered {replay.status_code}")
    bad += view_misses(replay.json(), stored, "decision replay")
    clock.now += timedelta(hours=3)
    actual = client.post(
        f"/v1/assessments/{rid}/actual-conditions",
        json={"observations": {"vis": "ok"}},
        headers=AUTH_A,
    )
    if actual.status_code != 200:
        return [*bad, f"control: actual conditions answered {actual.status_code}"]
    bad += view_misses(actual.json(), stored, "actual conditions")
    after = client.get(f"/v1/assessments/{rid}", headers=AUTH_A)
    bad += view_misses(after.json(), stored, "GET after")
    # a create-only key sees the record through withheld()
    state = client.app.state.divesafe  # type: ignore[attr-defined]
    state.authenticator = type(state.authenticator)(
        {_hash("create-only-key-0123456789"): KeyEntry("alice", frozenset({Role.ASSESSOR}))}
    )
    only = {"Authorization": "Bearer create-only-key-0123456789"}
    seen = client.get(f"/v1/assessments/{rid}", headers=only)
    if seen.status_code != 200 or seen.json().get("decision_withheld") is not True:
        bad.append(f"control: the create-only GET answered {seen.status_code}")
    bad += view_misses(seen.json(), stored, "create-only GET")
    if seen.json()["record"]["human_decision"] is not None:
        bad.append("create-only GET shows the human decision")
    return bad


def test_every_api_path_shows_the_self_estimate_and_no_nested_confidence() -> None:
    found = api_misses()
    assert found == []


def test_the_api_estimates_are_the_domain_values_not_defaults() -> None:
    client, _, repo = _build(provider=fake())
    body = create_and_poll(client, PLAN, AUTH_A).json()
    stored = stored_of(client, repo, body)
    shown_scenarios = [s["uncalibrated_self_estimate"] for s in body["record"]["scenarios"]]
    assert shown_scenarios == [s.confidence for s in stored.scenarios] == [0.5, 0.5, 0.5]
    shown_findings = [f["uncalibrated_self_estimate"] for f in body["record"]["findings"]]
    assert shown_findings == [f.confidence for f in stored.findings]
    assert shown_findings.count(0.6) == 4 and shown_findings.count(None) == 1


# --- (e) the stored form is unchanged -----------------------------------------------------------


def test_the_stored_json_keeps_confidence_inside_scenarios_and_findings_and_round_trips() -> None:
    record = varied_record()
    dumped = record.model_dump_json()
    data = json.loads(dumped)
    paths = confidence_paths(data)
    assert "scenarios[0].confidence" in paths and "findings[0].confidence" in paths
    assert not any("uncalibrated_self_estimate" in key for key in _keys(data))
    back = AssessmentRecord.model_validate_json(dumped)
    assert back == record
    assert [s.confidence for s in back.scenarios] == list(SCENARIO_VALUES)
    assert back.model_dump_json() == dumped


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [*map(str, value), *(k for v in value.values() for k in _keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in _keys(v)]
    return []


def test_the_storage_checks_accept_the_domain_dump_and_the_view_is_a_different_shape() -> None:
    record = varied_record()
    dumped = json.loads(record.model_dump_json())
    _storable(record.model_dump())  # raises UnstorableRecordError if not storable
    assert _same(dumped, json.loads(record.model_dump_json()))
    view = json.loads(AssessmentRecordView.of(record).model_dump_json())
    assert not _same(dumped, view)  # the read-back would refuse a view written by mistake
    assert view != dumped
    assert "confidence" not in view["scenarios"][0] and "confidence" in dumped["scenarios"][0]
    # a view does not load back as a stored record either
    with pytest.raises(ValidationError):
        AssessmentRecord.model_validate(view)


def test_the_view_is_built_from_a_record_and_changes_nothing_in_it() -> None:
    record = varied_record()
    before = record.model_dump_json()
    AssessmentView.of(record)
    AssessmentView.withheld(record)
    assert record.model_dump_json() == before


# --- (f) the prompt layer -----------------------------------------------------------------------


def test_no_self_estimate_reaches_a_later_prompt() -> None:
    provider = fake()
    run_pipeline(provider)
    later = [r for r in provider.requests if task_of(r) == "risk_assessment"]
    later += [r for r in provider.requests if task_of(r).startswith("scenario:")]
    assert len(later) == 4
    for request in later:
        content = request.messages[1].content
        # the reply schema after the data legitimately names `confidence`; the DATA must not
        data = content.split("<untrusted_data>", 1)[1].split("</untrusted_data>", 1)[0]
        assert "findings" in data  # control: the earlier findings are in the prompt
        assert "confidence" not in data
        assert "uncalibrated_self_estimate" not in data


def prompt_misses(mod: Any) -> list[str]:
    record = varied_record()
    bad: list[str] = []
    for name, items in (("findings", record.findings), ("scenarios", record.scenarios)):
        out = mod.agent_view(items)
        if len(out) != len(items):
            bad.append(f"{name}: items were dropped")
        if any("confidence" in item for item in out):
            bad.append(f"{name}: confidence reached the prompt view")
        if any("uncalibrated_self_estimate" in item for item in out):
            bad.append(f"{name}: the self estimate reached the prompt view")
        if any("summary" not in item for item in out):
            bad.append(f"{name}: other fields were lost")
    return bad


def test_the_prompt_view_strips_every_estimate() -> None:
    found = prompt_misses(__import__("divesafe.agents.prompts", fromlist=["x"]))
    assert found == []
    shown_items = agent_view(AssessmentView.of(varied_record()).record.findings)
    assert all("confidence" not in item for item in shown_items)


def test_mutation_of_the_prompt_strip_is_caught() -> None:
    from divesafe.agents import prompts as prompts_module

    mutant = mutated_module(prompts_module, ('if k != "confidence"', "if True"))
    found = prompt_misses(mutant)
    assert found != []


# --- mutations of divesafe.api.schemas ----------------------------------------------------------

LOOP = (
    '        for key in ("scenarios", "findings"):\n'
    "            data[key] = [_renamed(item) for item in data[key]]\n"
)
SCENARIO_FIELD = (
    "    uncalibrated_self_estimate: float = Field(ge=0.0, le=1.0, description=_SELF_ESTIMATE)"
)
CONFIG = '    model_config = ConfigDict(frozen=True, extra="forbid")\n\n    kind: ScenarioKind'
RENAME = '    out["uncalibrated_self_estimate"] = item["confidence"]\n'
KEEP_BOTH = (
    '    out["uncalibrated_self_estimate"] = item["confidence"]\n    out["confidence"] = 1\n'
)
EDITS = [
    pytest.param(
        [
            (
                "    uncalibrated_self_estimate: float = Field(ge=0.0, le=1.0,",
                "    confidence: float = Field(ge=0.0, le=1.0,",
            ),
            (RENAME, '    out["confidence"] = item["confidence"]\n'),
        ],
        id="scenario-field-named-confidence-again",
    ),
    pytest.param(
        [
            (
                "    uncalibrated_self_estimate: float | None = Field(\n        default=None,",
                "    confidence: float | None = Field(\n        default=None,",
            ),
            (RENAME, '    out["confidence"] = item["confidence"]\n'),
        ],
        id="finding-field-named-confidence-again",
    ),
    pytest.param([(RENAME, KEEP_BOTH.replace("= 1", "= item['confidence']"))], id="both-keys-kept"),
    pytest.param(
        [(RENAME, '    out["uncalibrated_self_estimate"] = None\n')], id="value-not-copied"
    ),
    pytest.param(
        [(RENAME, '    out["uncalibrated_self_estimate"] = item["confidence"] or None\n')],
        id="zero-estimate-lost",
    ),
    pytest.param(
        [(RENAME, '    out["uncalibrated_self_estimate"] = item["confidence"] or 0.5\n')],
        id="missing-estimate-invented",
    ),
    pytest.param(
        [
            (
                LOOP,
                '        data["scenarios"] = [_renamed(item) for item in data["scenarios"]]\n',
            )
        ],
        id="rename-skipped-for-findings",
    ),
    pytest.param(
        [
            (
                LOOP,
                '        data["findings"] = [_renamed(item) for item in data["findings"]]\n',
            )
        ],
        id="rename-skipped-for-scenarios",
    ),
    pytest.param(
        [("It is NOT a confidence, is not ", "It is a confidence, is not ")],
        id="description-loses-NOT",
    ),
    pytest.param(
        [("and is not a measure of dive safety", "and is a measure of dive safety")],
        id="description-claims-a-safety-measure",
    ),
    pytest.param(
        [
            (
                "        stripped = record.model_copy(",
                "        return cls.model_construct(record=record, status='DECISION_WITHHELD')\n"
                "        stripped = record.model_copy(",
            )
        ],
        id="withheld-bypasses-of",
    ),
    pytest.param(
        [("        view = cls.of(stripped)\n", "        view = cls.of(record)\n")],
        id="withheld-shows-the-decision",
    ),
    pytest.param(
        [
            (
                SCENARIO_FIELD,
                "    uncalibrated_self_estimate: float = Field(description=_SELF_ESTIMATE)",
            )
        ],
        id="scenario-bounds-removed",
    ),
    pytest.param(
        [
            (
                "        default=None, ge=0.0, le=1.0, description=_SELF_ESTIMATE\n",
                "        default=None, description=_SELF_ESTIMATE\n",
            )
        ],
        id="finding-bounds-removed",
    ),
    pytest.param(
        [
            (
                SCENARIO_FIELD,
                SCENARIO_FIELD.replace("le=1.0", "le=2.0"),
            )
        ],
        id="scenario-upper-bound-loosened",
    ),
    pytest.param(
        [
            (
                CONFIG,
                CONFIG.replace("forbid", "ignore"),
            )
        ],
        id="scenario-accepts-unknown-fields",
    ),
]


@pytest.mark.parametrize("edits", EDITS)
def test_every_mutation_of_the_view_is_caught(edits: list[tuple[str, str]]) -> None:
    mutant = mutated_module(schemas_module, *edits)
    misses: list[str] = []
    crashes: list[str] = []
    for name, check in (
        ("schema", schema_misses),
        ("bounds", bound_misses),
        ("build", build_misses),
    ):
        try:
            misses += check(mutant)
        except ValidationError:
            misses.append(f"{name}: the view refused its own data")  # a validator is the defence
        except RepositoryIntegrityError as exc:
            # A broken view refusing to build is a catch, but only this exact fixed-text error.
            if str(exc) != PRESENT_REFUSAL:
                crashes.append(f"{name}: RepositoryIntegrityError({exc})")
            else:
                misses.append(f"{name}: the view refused to build the record")
        except Exception as exc:  # noqa: BLE001
            crashes.append(f"{name}: {type(exc).__name__}")
    assert misses != [], f"survived (crashes: {crashes})"
