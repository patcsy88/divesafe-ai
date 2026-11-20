"""ADR 0008 Amendment 1: `DIVESAFE_DEGRADED_MAY_SUPPORT_GO`. DEGRADED evidence may support GO
and CAUTION only with a pinned, signed-off ruleset; such a result is marked for the human, and
the marker never changes a severity. All values are SYNTHETIC."""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.agent import scripted_llm as sl
from tests.conftest import NOW, make_evidence
from tests.safety.test_evidence_usability import PLAN, START, _degraded, _engine, _item
from tests.unit.test_ruleset_loader import tioman

from divesafe.api.schemas import DEGRADED_EVIDENCE_NOTE, AssessmentView
from divesafe.api.state import INTERIM_REQUIRED_CATEGORIES, build_engine
from divesafe.config import Settings
from divesafe.data import TIOMAN_ISLAND
from divesafe.domain import (
    AssessmentRecord,
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskAssessment,
    RuleResult,
    most_severe,
)
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.risk import (
    COARSE_SPAN,
    DEGRADED_NEVER_SUPPORTS_GO,
    TIME_RESOLVED_PREDICTIONS,
    EvidencePolicy,
    RiskRulesEngine,
)

pytestmark = pytest.mark.safety
R = Recommendation
C = DataCategory
H = timedelta(hours=1)
QUALITY_GUARD = "evidence.quality_insufficient_for_go"
NEEDS_RULESET = (
    "Value error, DIVESAFE_DEGRADED_MAY_SUPPORT_GO needs a signed-off ruleset pinned with "
    "DIVESAFE_RULESET_PATH and DIVESAFE_RULESET_SHA256"
)
PRODUCTION_NEEDS_PIN = (
    "Value error, a production ruleset must be pinned with DIVESAFE_RULESET_SHA256"
)
WARNING_TEXT = (
    "DEGRADED evidence may support GO and CAUTION (DIVESAFE_DEGRADED_MAY_SUPPORT_GO); model and "
    "forecast data can back a result, marked for the human (ADR 0008 Amendment 1)"
)
STATE_LOGGER = "divesafe.api.state"
MARK_PATH = Path("/nonexistent/degraded-marker-ruleset.json")  # never read: Settings only
MARK_SHA = "0123abcd" * 8
DEVELOPMENT: dict[str, Any] = {}
PRODUCTION: dict[str, Any] = {
    "environment": "production",
    "llm_provider": "openai_compatible",
    "api_key_hashes": json.dumps({"a" * 64: {"actor": "o", "roles": ["viewer"]}}),
}
ENVIRONMENTS = pytest.mark.parametrize(
    "env", [DEVELOPMENT, PRODUCTION], ids=["development", "production"]
)
PROD_PLAN = DivePlan(
    site_id=TIOMAN_ISLAND.id, planned_start=NOW + H, planned_duration_minutes=30, max_depth_m=10
)


class _Fixed:
    """A synthetic rule returning one fixed outcome, citing every item supplied."""

    rule_id = "synthetic.fixed"
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self, outcome: Recommendation) -> None:
        self._outcome = outcome

    def evaluate(self, plan: DivePlan, evidence: Sequence[EvidenceItem]) -> RuleResult:
        return RuleResult(
            rule_id=self.rule_id,
            outcome=self._outcome,
            rationale="synthetic",
            evidence_ids=tuple(e.id for e in evidence),
        )


def _ids(result: RiskAssessment) -> list[str]:
    return [r.rule_id for r in result.rule_results]


def _pinned_ruleset(tmp_path: Path, *definitions: dict[str, Any]) -> tuple[Path, str]:
    path = tmp_path / "ruleset.json"
    body = {"ruleset_version": "synthetic-degraded-1", "definitions": list(definitions)}
    path.write_bytes(json.dumps(body).encode())
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _settings(path: Path, sha: str, *, on: bool) -> Settings:
    return Settings(
        _env_file=None,
        evidence_max_age_minutes=60,
        ruleset_path=path,
        ruleset_sha256=sha,
        degraded_may_support_go=on,
    )


def _production_engine(tmp_path: Path, *, on: bool) -> RiskRulesEngine:
    """The production wiring with a ruleset covering only wave height (one synthetic limit)."""
    engine = build_engine(_settings(*_pinned_ruleset(tmp_path, tioman()), on=on), now=NOW)
    assert engine is not None
    return engine


def _wave(height: float) -> EvidenceItem:
    return EvidenceItem(
        id="ev-waves",
        category=C.WAVES_SWELL,
        source="synthetic",
        retrieved_at=NOW - timedelta(minutes=5),
        valid_at=NOW,
        valid_until=NOW + timedelta(days=1),
        is_forecast=True,
        data_kind=DataKind.MODEL,
        quality=DataQuality.DEGRADED,
        value={"variables": {"wave_height": height}, "units": {"wave_height": "m"}},
    )


def _all_degraded(height: float) -> list[EvidenceItem]:
    return [_wave(height), *(make_evidence(c) for c in DataCategory if c is not C.WAVES_SWELL)]


# --- a. Settings -----------------------------------------------------------------------------


@ENVIRONMENTS
def test_degraded_support_is_off_by_default(env: dict[str, Any]) -> None:
    assert Settings(_env_file=None, **env).degraded_may_support_go is False


@ENVIRONMENTS
@pytest.mark.parametrize(
    "pinned",
    [{}, {"ruleset_sha256": MARK_SHA}, {"ruleset_path": MARK_PATH}],
    ids=["neither", "sha256_only", "path_only"],
)
def test_settings_refuse_degraded_support_without_a_pinned_ruleset(
    env: dict[str, Any], pinned: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, degraded_may_support_go=True, **env, **pinned)
    errors = caught.value.errors()
    assert len(errors) == 1
    # In production a path without a pin is already refused by the older pin rule (which runs
    # first); the development case is the one that proves the new check on its own.
    path_only_in_production = env is PRODUCTION and "ruleset_path" in pinned
    assert errors[0]["msg"] == (PRODUCTION_NEEDS_PIN if path_only_in_production else NEEDS_RULESET)
    rendered = str(caught.value)  # hide_input_in_errors: no configured value is echoed
    assert "degraded-marker-ruleset" not in rendered
    assert MARK_SHA not in rendered


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_the_environment_variable_is_refused_without_a_pinned_ruleset(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DIVESAFE_DEGRADED_MAY_SUPPORT_GO", value)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert [e["msg"] for e in caught.value.errors()] == [NEEDS_RULESET]


def test_an_unrecognised_value_is_refused_without_being_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIVESAFE_DEGRADED_MAY_SUPPORT_GO", "enabled-XYZZY")
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert "XYZZY" not in str(caught.value)


def test_an_empty_value_leaves_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIVESAFE_DEGRADED_MAY_SUPPORT_GO", "")
    assert Settings(_env_file=None).degraded_may_support_go is False


def test_build_engine_is_the_only_place_src_sets_the_policy_and_it_reads_settings() -> None:
    """Security review SF3: the source scan in test_evidence_usability catches only literals.
    Every keyword `degraded_may_support_go=` in src/ is the one in `build_engine`, its value is
    the local `degraded_support`, and that local is `settings.degraded_may_support_go is True`."""
    import ast
    import inspect

    from divesafe.api import state

    root = Path(state.__file__).resolve().parents[1]
    uses: list[str] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "degraded_may_support_go":
                uses.append(f"{path.relative_to(root)}:{ast.unparse(node.value)}")
    assert uses == ["api/state.py:degraded_support"]
    tree = ast.parse(inspect.getsource(state.build_engine).lstrip())
    assigns = [
        ast.unparse(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "degraded_support" for t in n.targets)
    ]
    assert assigns == ["settings.degraded_may_support_go is True"]


def test_the_policy_switches_cannot_be_passed_by_position() -> None:
    """Security review nit (2026-11-20): the source scan above sees keywords only, so a switch
    passed by position would slip past it. Only the category set and max age are positional."""
    with pytest.raises(TypeError):
        EvidencePolicy(frozenset(), H, frozenset(), True, True)  # type: ignore[misc]
    kw_only = {f.name for f in dataclasses.fields(EvidencePolicy) if f.kw_only and f.init}
    assert {"accepted_quality", "require_window_coverage", "degraded_may_support_go"} <= kw_only


def test_build_engine_ignores_a_non_bool_truthy_value(tmp_path: Path) -> None:
    path, sha = _pinned_ruleset(tmp_path, tioman())
    settings = _settings(path, sha, on=False).model_copy(
        update={"degraded_may_support_go": "false"}
    )
    engine = build_engine(settings, now=NOW)
    assert engine is not None
    assert engine._policy.degraded_may_support_go is False  # noqa: SLF001


def test_the_web_copy_of_the_note_equals_the_api_constant() -> None:
    web = Path(__file__).parents[2] / "web/components/DegradedEvidenceNote.ts"
    assert json.dumps(DEGRADED_EVIDENCE_NOTE, ensure_ascii=False) in web.read_text("utf8")


@ENVIRONMENTS
def test_settings_accept_degraded_support_with_a_pinned_ruleset(env: dict[str, Any]) -> None:
    settings = Settings(
        _env_file=None,
        degraded_may_support_go=True,
        ruleset_path=MARK_PATH,
        ruleset_sha256=MARK_SHA,
        **env,
    )
    assert settings.degraded_may_support_go is True  # control for the refusals above


# --- b. build_engine -------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["model_copy", "model_construct"])
@pytest.mark.parametrize("pinned", ["neither", "path_only", "sha256_only"])
def test_build_engine_refuses_degraded_support_that_bypassed_the_settings_check(
    how: str, pinned: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path, sha = _pinned_ruleset(tmp_path, tioman())
    fields: dict[str, Any] = {
        "degraded_may_support_go": True,
        "ruleset_path": path if pinned == "path_only" else None,
        "ruleset_sha256": sha if pinned == "sha256_only" else None,
    }
    base = Settings(_env_file=None, evidence_max_age_minutes=60)
    if how == "model_copy":
        bypassed = base.model_copy(update=fields)
    else:
        bypassed = Settings.model_construct(**{**dict(base), **fields})
    assert bypassed.degraded_may_support_go is True  # the validator really was skipped
    caplog.set_level(logging.WARNING, logger=STATE_LOGGER)
    with pytest.raises(RuntimeError) as caught:
        build_engine(bypassed, now=NOW)
    assert str(caught.value) == "DIVESAFE_DEGRADED_MAY_SUPPORT_GO needs a pinned signed-off ruleset"
    assert WARNING_TEXT not in [r.getMessage() for r in caplog.records]


def test_build_engine_refuses_degraded_support_with_a_ruleset_without_definitions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path, sha = _pinned_ruleset(tmp_path)
    caplog.set_level(logging.WARNING, logger=STATE_LOGGER)
    with pytest.raises(RuntimeError) as caught:
        build_engine(_settings(path, sha, on=True), now=NOW)
    assert str(caught.value) == "DIVESAFE_DEGRADED_MAY_SUPPORT_GO needs a ruleset with definitions"
    assert WARNING_TEXT not in [r.getMessage() for r in caplog.records]
    off = build_engine(_settings(path, sha, on=False), now=NOW)  # control: the file itself loads
    assert off is not None and off.policy.degraded_may_support_go is False


def test_build_engine_passes_the_setting_to_the_policy(tmp_path: Path) -> None:
    on = _production_engine(tmp_path, on=True)
    off = _production_engine(tmp_path, on=False)
    assert on.policy.degraded_may_support_go is True
    assert off.policy.degraded_may_support_go is False


# --- c. the start-up warning -----------------------------------------------------------------


@pytest.mark.parametrize("on", [True, False])
def test_the_start_up_warning_is_logged_exactly_when_on(
    on: bool, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=STATE_LOGGER)
    _production_engine(tmp_path, on=on)
    records = [r for r in caplog.records if r.name == STATE_LOGGER]
    assert "ruleset loaded" in [r.getMessage() for r in records]  # control: capture works
    degraded = [(r.levelno, r.getMessage()) for r in records if "DEGRADED" in r.getMessage()]
    assert degraded == ([(logging.WARNING, WARNING_TEXT)] if on else [])


# --- d. the engine ---------------------------------------------------------------------------


def test_degraded_only_evidence_supports_go_only_with_the_policy_on() -> None:
    on = _engine(degraded_may_support_go=True).assess(PLAN, [_degraded()], NOW)
    off = _engine().assess(PLAN, [_degraded()], NOW)

    assert on.recommendation is R.GO
    assert on.rests_on_degraded_evidence is True
    assert QUALITY_GUARD not in _ids(on)
    assert off.recommendation is R.INSUFFICIENT_EVIDENCE
    assert off.rests_on_degraded_evidence is False
    assert QUALITY_GUARD in _ids(off)


@pytest.mark.parametrize("outcome", list(Recommendation), ids=lambda r: r.name)
def test_the_marker_never_changes_a_severity(outcome: Recommendation) -> None:
    on = _engine(rules=[_Fixed(outcome)], degraded_may_support_go=True).assess(
        PLAN, [_degraded()], NOW
    )
    off = _engine(rules=[_Fixed(outcome)]).assess(PLAN, [_degraded()], NOW)

    for result in (on, off):
        assert result.recommendation is most_severe(tuple(r.outcome for r in result.rule_results))
    # identical rule results except the quality guard, which only the policy-off engine adds
    assert [r for r in off.rule_results if r.rule_id != QUALITY_GUARD] == list(on.rule_results)
    assert QUALITY_GUARD not in _ids(on)
    assert on.recommendation is outcome
    assert on.rests_on_degraded_evidence is (outcome in (R.GO, R.CAUTION))
    assert off.rests_on_degraded_evidence is False
    if outcome not in (R.GO, R.CAUTION):
        assert off.recommendation is outcome


@pytest.mark.parametrize("extra", [(), ("degraded",)], ids=["validated", "validated_and_degraded"])
def test_validated_cover_for_every_required_category_leaves_no_marker(
    extra: tuple[str, ...],
) -> None:
    items = [_item(quality=DataQuality.VALIDATED)] + [_degraded("d2") for _ in extra]
    result = _engine(degraded_may_support_go=True).assess(PLAN, items, NOW)
    assert result.recommendation is R.GO
    assert result.rests_on_degraded_evidence is False


def test_a_required_category_without_validated_cover_sets_the_marker() -> None:
    """Per category: validated waves do not hide degraded-only wind, and a validated item over
    half the window does not launder the degraded item that covers the rest."""
    two = _engine({C.WAVES_SWELL, C.WIND}, degraded_may_support_go=True).assess(
        PLAN, [_item(quality=DataQuality.VALIDATED), _degraded("w", category=C.WIND)], NOW
    )
    half = _item(quality=DataQuality.VALIDATED, valid_until=START + H / 2)
    partial = _engine(degraded_may_support_go=True).assess(PLAN, [half, _degraded("d2")], NOW)
    for result in (two, partial):
        assert result.recommendation is R.GO
        assert result.rests_on_degraded_evidence is True


# --- e. placeholders and signed limits still decide ------------------------------------------


@pytest.mark.parametrize("on", [True, False])
def test_placeholders_still_block_go_on_degraded_evidence(on: bool, tmp_path: Path) -> None:
    result = _production_engine(tmp_path, on=on).assess(PROD_PLAN, _all_degraded(1.0), NOW)
    outcomes = {r.rule_id: r.outcome for r in result.rule_results}

    assert outcomes["definition.synthetic.wave_height"] is R.GO  # control: the limit passed
    placeholders = {i for i in outcomes if i.startswith("placeholder.")}
    assert placeholders == {f"placeholder.{f.value}" for f in result.unevaluated_factors}
    assert placeholders and all(outcomes[i] is R.INSUFFICIENT_EVIDENCE for i in placeholders)
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE
    assert result.rests_on_degraded_evidence is False


@pytest.mark.parametrize("on", [True, False])
def test_a_no_go_limit_still_gives_no_go_on_degraded_evidence(on: bool, tmp_path: Path) -> None:
    result = _production_engine(tmp_path, on=on).assess(PROD_PLAN, _all_degraded(4.0), NOW)
    outcomes = {r.rule_id: r.outcome for r in result.rule_results}

    assert outcomes["definition.synthetic.wave_height"] is R.NO_GO
    assert result.recommendation is R.NO_GO
    assert result.rests_on_degraded_evidence is False


# --- f. domain -------------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", list(Recommendation), ids=lambda r: r.name)
def test_a_risk_assessment_takes_the_marker_only_on_go_or_caution(outcome: Recommendation) -> None:
    fields: dict[str, Any] = {
        "recommendation": outcome,
        "rule_results": (_Fixed(outcome).evaluate(PLAN, [_degraded()]),),
        "ruleset_version": "synthetic",
        "evaluated_at": NOW,
    }
    assert RiskAssessment(**fields).rests_on_degraded_evidence is False  # the default
    if outcome in (R.GO, R.CAUTION):
        assert RiskAssessment(**fields, rests_on_degraded_evidence=True).rests_on_degraded_evidence
        return
    with pytest.raises(ValidationError) as caught:
        RiskAssessment(**fields, rests_on_degraded_evidence=True)
    assert [e["msg"] for e in caught.value.errors()] == [
        "Value error, the degraded-evidence note applies only to GO or CAUTION"
    ]


@functools.cache
def _record(proposal: str | None) -> AssessmentRecord:
    """A scripted run on the recorded MODEL/DEGRADED marine evidence, policy on, no warning
    rule: the deterministic result is GO and the proposal sets the final result."""
    provider = None if proposal is None else sl.fake(recommendation=proposal)
    return sl.run_pipeline(provider, with_warning_rule=False)


PROPOSALS = pytest.mark.parametrize("proposal", ["GO", "CAUTION", "NO-GO", "INSUFFICIENT EVIDENCE"])


@PROPOSALS
def test_a_record_takes_the_marker_only_on_a_final_go_or_caution(proposal: str) -> None:
    data = _record(proposal).model_dump(mode="json")
    assert data["final_recommendation"] == proposal  # control
    data["rests_on_degraded_evidence"] = True
    if proposal in ("GO", "CAUTION"):
        assert AssessmentRecord.model_validate(data).rests_on_degraded_evidence is True
        return
    with pytest.raises(ValidationError) as caught:
        AssessmentRecord.model_validate(data)
    assert [e["msg"] for e in caught.value.errors()] == [
        "Value error, the degraded-evidence note applies only to GO or CAUTION"
    ]


@PROPOSALS
def test_an_older_record_without_the_field_loads_unmarked(proposal: str) -> None:
    data = _record(proposal).model_dump(mode="json")
    del data["rests_on_degraded_evidence"]
    assert AssessmentRecord.model_validate(data).rests_on_degraded_evidence is False


# --- g. the pipeline -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("proposal", "final", "marked"),
    [
        (None, R.GO, True),
        ("GO", R.GO, True),
        ("CAUTION", R.CAUTION, True),
        ("NO-GO", R.NO_GO, False),
        ("INSUFFICIENT EVIDENCE", R.INSUFFICIENT_EVIDENCE, False),
    ],
    ids=["no_provider", "go", "caution", "tightened_no_go", "tightened_insufficient"],
)
def test_the_record_is_marked_only_while_the_final_result_is_go_or_caution(
    proposal: str | None, final: Recommendation, marked: bool
) -> None:
    record = _record(proposal)
    assert record.deterministic_recommendation is R.GO  # the engine itself was marked
    assert record.final_recommendation is final
    assert record.rests_on_degraded_evidence is marked


def test_a_run_tightened_by_supervise_is_not_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    original = pipeline_module.STAGE_WORK[pipeline_module.StageName.REASON]

    async def unpinned(run: Any) -> Any:
        outcome = await original(run)
        run.model_version = "fake/fake-model"  # fails supervisor.prompt_version_pinned
        return outcome

    monkeypatch.setitem(pipeline_module.STAGE_WORK, pipeline_module.StageName.REASON, unpinned)
    record = sl.run_pipeline(sl.fake(recommendation="CAUTION"), with_warning_rule=False)
    verdict = record.supervisor_verdict
    assert verdict is not None
    assert verdict.tightened_to is R.INSUFFICIENT_EVIDENCE is record.final_recommendation
    assert record.rests_on_degraded_evidence is False


def test_a_run_held_by_a_deterministic_rule_is_not_marked() -> None:
    record = sl.run_pipeline(sl.fake(recommendation="GO"))  # with the warning rule
    assert record.final_recommendation is R.INSUFFICIENT_EVIDENCE
    assert record.rests_on_degraded_evidence is False


def test_a_run_with_the_policy_off_is_not_marked() -> None:
    strict = RiskRulesEngine(
        [sl._GoRule()],
        EvidencePolicy(sl.LIVE, H),
        "synthetic",
        required_factors=frozenset(),  # synthetic GO rule, as in scripted_llm.engine
    )
    record = sl.run_pipeline(sl.fake(recommendation="GO"), risk_engine=strict)
    assert record.final_recommendation is R.INSUFFICIENT_EVIDENCE
    assert QUALITY_GUARD in [r.rule_id for r in record.rule_results]
    assert record.rests_on_degraded_evidence is False


# --- h. the view -----------------------------------------------------------------------------


def test_the_note_is_fixed_wording() -> None:
    assert DEGRADED_EVIDENCE_NOTE == "based on model and forecast data, not observations"


@PROPOSALS
def test_the_view_shows_the_note_exactly_when_marked_and_go_or_caution(proposal: str) -> None:
    record = _record(proposal)
    data = record.model_dump(mode="json")
    del data["rests_on_degraded_evidence"]
    older = AssessmentRecord.model_validate(data)

    shown = AssessmentView.of(record).degraded_evidence_note
    assert shown == (DEGRADED_EVIDENCE_NOTE if proposal in ("GO", "CAUTION") else "")
    assert AssessmentView.of(older).degraded_evidence_note == ""
    assert shown == (DEGRADED_EVIDENCE_NOTE if record.rests_on_degraded_evidence else "")


# --- i. tide predictions (ADR 0008 Amendment 2, owner 2026-11-20) -----------------------------
# Amendment 1 held DEGRADED tides; Amendment 2 exempts only tide PREDICTIONs that cover the
# window on their own, and treats a tide PREDICTION day (every high and low water with its time)
# as time-resolved. Every other DEGRADED tide item stays held, however short, and the setting off
# still holds every DEGRADED tide item.

DAY_I = timedelta(days=1)


def _tide(item_id: str = "t", *, kind: DataKind = DataKind.PREDICTION) -> EvidenceItem:
    return _item(
        category=C.TIDES,
        kind=kind,
        quality=DataQuality.DEGRADED,
        item_id=item_id,
        valid_at=START - H,
        valid_until=START - H + DAY_I,
    )


def test_tides_stay_held_and_only_tide_predictions_are_time_resolved() -> None:
    assert frozenset({C.TIDES}) == DEGRADED_NEVER_SUPPORTS_GO
    assert frozenset({C.TIDES}) == TIME_RESOLVED_PREDICTIONS
    # The PREDICTION kind check in `_fine_cover` is an extra safeguard; its mutant is equivalent
    # only while this holds (risk review, 2026-11-20).
    assert TIME_RESOLVED_PREDICTIONS <= DEGRADED_NEVER_SUPPORTS_GO
    assert C.TIDES in INTERIM_REQUIRED_CATEGORIES  # production requires tides, so it is checked


@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION], ids=lambda r: r.name)
def test_a_daily_tide_prediction_supports_go_and_caution_with_the_policy_on(
    outcome: Recommendation,
) -> None:
    items = [_item(quality=DataQuality.VALIDATED), _tide()]
    result = _engine(
        {C.WAVES_SWELL, C.TIDES}, rules=[_Fixed(outcome)], degraded_may_support_go=True
    ).assess(PLAN, items, NOW)
    assert QUALITY_GUARD not in _ids(result)
    assert result.recommendation is outcome
    assert result.rests_on_degraded_evidence is True  # the human sees the fixed note


@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION], ids=lambda r: r.name)
def test_a_daily_tide_prediction_is_still_held_with_the_policy_off(
    outcome: Recommendation,
) -> None:
    items = [_item(quality=DataQuality.VALIDATED), _tide()]
    result = _engine(
        {C.WAVES_SWELL, C.TIDES}, rules=[_Fixed(outcome)], degraded_may_support_go=False
    ).assess(PLAN, items, NOW)
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.outcome is R.INSUFFICIENT_EVIDENCE
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE
    assert result.rests_on_degraded_evidence is False


@pytest.mark.parametrize("kind", [DataKind.MODEL, DataKind.FORECAST], ids=lambda k: k.name)
def test_a_daily_tide_item_that_is_not_a_prediction_is_still_coarse(kind: DataKind) -> None:
    items = [_item(quality=DataQuality.VALIDATED), _tide(kind=kind)]
    result = _engine({C.WAVES_SWELL, C.TIDES}, degraded_may_support_go=True).assess(
        PLAN, items, NOW
    )
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.rationale.endswith("missing for: tides")
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE


def test_a_daily_prediction_in_another_category_is_still_coarse() -> None:
    item = _item(
        kind=DataKind.PREDICTION,
        quality=DataQuality.DEGRADED,
        valid_at=START - H,
        valid_until=START - H + DAY_I,
    )
    result = _engine(degraded_may_support_go=True).assess(PLAN, [item], NOW)
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.rationale.endswith("missing for: waves_swell")
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE


def test_a_tide_prediction_still_names_another_coarse_category() -> None:
    items = [_daily("w", category=C.WIND), _tide()]
    result = _engine({C.WIND, C.TIDES}, degraded_may_support_go=True).assess(PLAN, items, NOW)
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.rationale.endswith("missing for: wind")  # tides pass, wind does not
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE


def test_a_tide_prediction_that_does_not_cover_the_window_is_not_usable() -> None:
    late = _tide().model_copy(update={"valid_at": START + H / 2, "valid_until": START + DAY_I})
    result = _engine({C.TIDES}, degraded_may_support_go=True).assess(PLAN, [late], NOW)
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE
    assert result.rests_on_degraded_evidence is False


@pytest.mark.parametrize("outcome", [R.NO_GO, R.INSUFFICIENT_EVIDENCE], ids=lambda r: r.name)
def test_a_tide_prediction_never_relaxes_a_more_severe_result(outcome: Recommendation) -> None:
    items = [_item(quality=DataQuality.VALIDATED), _tide()]
    result = _engine(
        {C.WAVES_SWELL, C.TIDES}, rules=[_Fixed(outcome)], degraded_may_support_go=True
    ).assess(PLAN, items, NOW)
    assert result.recommendation is outcome


@pytest.mark.parametrize(
    "kind",
    [DataKind.MODEL, DataKind.FORECAST, DataKind.OBSERVATION],
    ids=lambda k: k.name,
)
def test_a_sub_daily_degraded_tide_item_that_is_not_a_prediction_is_still_held(
    kind: DataKind,
) -> None:
    hourly = _item(category=C.TIDES, kind=kind, quality=DataQuality.DEGRADED, item_id="t")
    items = [_item(quality=DataQuality.VALIDATED), hourly]
    result = _engine(
        {C.WAVES_SWELL, C.TIDES}, rules=[_Fixed(R.GO)], degraded_may_support_go=True
    ).assess(PLAN, items, NOW)
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.rationale.endswith("missing for: tides")
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE
    assert result.rests_on_degraded_evidence is False


def test_a_tide_prediction_over_half_the_window_does_not_free_a_tide_model_over_the_rest() -> None:
    half = START + H / 2
    prediction = _tide("p").model_copy(update={"valid_at": START - H, "valid_until": half})
    model = _item(
        category=C.TIDES,
        kind=DataKind.MODEL,
        quality=DataQuality.DEGRADED,
        item_id="m",
        valid_at=half,
        valid_until=START + H,
    )
    items = [_item(quality=DataQuality.VALIDATED), prediction, model]
    result = _engine(
        {C.WAVES_SWELL, C.TIDES}, rules=[_Fixed(R.GO)], degraded_may_support_go=True
    ).assess(PLAN, items, NOW)
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.rationale.endswith("missing for: tides")
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE


# --- j. a daily or longer item does not stand in for time-resolved evidence -------------------

DAY = timedelta(days=1)


def _daily(item_id: str = "d", *, category: DataCategory = C.WAVES_SWELL) -> EvidenceItem:
    return _degraded(item_id, category=category, valid_at=START - H, valid_until=START - H + DAY)


def test_the_coarse_span_is_one_day() -> None:
    assert timedelta(days=1) == COARSE_SPAN


@pytest.mark.parametrize("outcome", [R.GO, R.CAUTION], ids=lambda r: r.name)
def test_degraded_daily_cover_alone_holds_go_and_caution_with_the_policy_on(
    outcome: Recommendation,
) -> None:
    result = _engine(rules=[_Fixed(outcome)], degraded_may_support_go=True).assess(
        PLAN, [_daily()], NOW
    )
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.outcome is R.INSUFFICIENT_EVIDENCE
    assert guard.rationale.endswith("missing for: waves_swell")
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE
    assert result.rests_on_degraded_evidence is False


def test_an_item_just_under_a_day_is_time_resolved_and_exactly_a_day_is_coarse() -> None:
    engine = _engine(degraded_may_support_go=True)
    under = _degraded(valid_at=START - H, valid_until=START - H + DAY - timedelta(seconds=1))
    assert engine.assess(PLAN, [under], NOW).recommendation is R.GO
    exact = _degraded(valid_at=START - H, valid_until=START - H + DAY)
    assert engine.assess(PLAN, [exact], NOW).recommendation is R.INSUFFICIENT_EVIDENCE


def test_a_time_resolved_degraded_item_beside_a_daily_one_lets_the_category_through() -> None:
    """Control: the hold is about cover, not about a daily item being present."""
    result = _engine(degraded_may_support_go=True).assess(
        PLAN, [_daily("d1"), _degraded("d2")], NOW
    )
    assert QUALITY_GUARD not in _ids(result)
    assert result.recommendation is R.GO
    assert result.rests_on_degraded_evidence is True


def test_a_daily_item_does_not_fill_a_hole_in_time_resolved_cover() -> None:
    half = _degraded("d2", valid_until=START + H / 2)
    result = _engine(degraded_may_support_go=True).assess(PLAN, [_daily("d1"), half], NOW)
    assert QUALITY_GUARD in _ids(result)
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE


def test_daily_cover_names_only_the_coarse_category() -> None:
    items = [_daily("w", category=C.WIND), _degraded("v")]
    result = _engine({C.WAVES_SWELL, C.WIND}, degraded_may_support_go=True).assess(PLAN, items, NOW)
    (guard,) = [r for r in result.rule_results if r.rule_id == QUALITY_GUARD]
    assert guard.rationale.endswith("missing for: wind")


def test_a_daily_item_beside_validated_cover_is_not_held() -> None:
    """The hold applies only to a category without VALIDATED cover of the window."""
    items = [_daily("d1"), _item(quality=DataQuality.VALIDATED, item_id="v")]
    result = _engine(degraded_may_support_go=True).assess(PLAN, items, NOW)
    assert QUALITY_GUARD not in _ids(result)
    assert result.recommendation is R.GO


def test_daily_cover_never_relaxes_a_more_severe_result() -> None:
    for outcome in (R.NO_GO, R.INSUFFICIENT_EVIDENCE):
        result = _engine(rules=[_Fixed(outcome)], degraded_may_support_go=True).assess(
            PLAN, [_daily()], NOW
        )
        assert result.recommendation is outcome


def test_daily_cover_is_held_by_the_quality_guard_with_the_policy_off_too() -> None:
    result = _engine(degraded_may_support_go=False).assess(PLAN, [_daily()], NOW)
    assert QUALITY_GUARD in _ids(result)
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE
