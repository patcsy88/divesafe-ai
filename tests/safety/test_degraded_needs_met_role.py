"""Owner condition (2026-11-21, ADR 0008 Amendment 1): `DIVESAFE_DEGRADED_MAY_SUPPORT_GO` is
refused until MET's tighten-only role is built and reviewed, and GO and CAUTION stay unreachable
until a change that makes them reachable is reviewed. All values are SYNTHETIC."""

from __future__ import annotations

import ast
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import NOW
from tests.safety.test_degraded_support_policy import (
    ENVIRONMENTS,
    MARK_PATH,
    MARK_SHA,
    PROD_PLAN,
    STATE_LOGGER,
    WARNING_TEXT,
    _pinned_ruleset,
    _settings,
)
from tests.unit.test_ruleset_loader import tioman

from divesafe.api.state import build_engine
from divesafe.config import Settings
from divesafe.config import settings as settings_module
from divesafe.domain import (
    FACTOR_METRICS,
    RANGE_CHECK,
    SUPPORTED_METRICS,
    THRESHOLD_FACTORS,
    UNIT_CHECK,
    DataCategory,
    DataKind,
    DataQuality,
    DivePlan,
    EvidenceItem,
    Recommendation,
    RiskFactorKind,
    TransformationStep,
)

pytestmark = pytest.mark.safety
R = Recommendation
K = RiskFactorKind
SRC = Path(__file__).parents[2] / "src/divesafe"
MET_ROLE = (
    "DIVESAFE_DEGRADED_MAY_SUPPORT_GO is refused until MET's tighten-only role is built and "
    "reviewed (ADR 0008 Amendment 1, owner 2026-11-21)"
)
# A past window, so VALIDATED observations (never valid after retrieval) can cover all of it.
PAST_START = NOW - 3 * timedelta(hours=1)
PAST_PLAN = DivePlan(
    site_id=PROD_PLAN.site_id, planned_start=PAST_START, planned_duration_minutes=30, max_depth_m=10
)
NAME = "MET_TIGHTEN_ONLY_ROLE_BUILT"
UNENCODABLE = {K.TIDAL_CURRENT, K.WEATHER, K.FORECAST_UNCERTAINTY, K.SITE_CONSTRAINT}


# --- a. the gate is closed in src --------------------------------------------------------------


def _bound(target: ast.expr) -> list[str]:
    """Every name a target binds, through tuples, lists, starred and subscripted names."""
    if isinstance(target, ast.Tuple | ast.List):
        return [n for e in target.elts for n in _bound(e)]
    if isinstance(target, ast.Starred):
        return _bound(target.value)
    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
        return [str(target.slice.value)]
    if isinstance(target, ast.Name):
        return [target.id]
    return [target.attr] if isinstance(target, ast.Attribute) else []


def test_the_gate_is_closed_in_src_and_assigned_only_once() -> None:
    """Only the PR that builds and reviews MET's role may change this (owner, 2026-11-21). A
    tripwire, not a control: review is the control. Any store, import alias or string naming the
    constant counts, so unpacking, walrus, `for`, `with`, `globals()[...]`, `setattr`, `mock.patch`
    strings, `__dict__.update(NAME=...)` and `global` all turn this red. A name built at run time
    (concatenation, f-string, `join`) is not seen; review is the control."""
    assert settings_module.MET_TIGHTEN_ONLY_ROLE_BUILT is False
    found: list[tuple[str, object]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        nodes = list(ast.walk(ast.parse(path.read_text("utf8"))))
        recorded: set[int] = set()  # targets of assignments already recorded with their value
        for node in nodes:
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(NAME in _bound(t) for t in targets):
                    value = node.value
                    found.append((rel, value.value if isinstance(value, ast.Constant) else value))
                    recorded.update(id(n) for t in targets for n in ast.walk(t))
        for node in nodes:
            stored = isinstance(node, ast.Name | ast.Attribute | ast.Subscript) and not isinstance(
                node.ctx, ast.Load
            )
            if stored and id(node) not in recorded and NAME in _bound(node):
                found.append((rel, "store"))
            elif isinstance(node, ast.alias) and NAME in (node.name, node.asname):
                found.append((rel, "alias"))
            elif isinstance(node, ast.keyword) and node.arg == NAME:
                found.append((rel, "keyword"))
            elif isinstance(node, ast.Global | ast.Nonlocal) and NAME in node.names:
                found.append((rel, "global"))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and NAME in node.value
            ):
                found.append((rel, "string"))
    assert found == [("config/settings.py", False)]


# --- b. Settings refuses the flag --------------------------------------------------------------


@ENVIRONMENTS
@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_the_environment_variable_is_refused_even_with_a_pinned_ruleset(
    env: dict[str, Any], value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVESAFE_DEGRADED_MAY_SUPPORT_GO", value)
    monkeypatch.setenv("DIVESAFE_RULESET_PATH", str(MARK_PATH))
    monkeypatch.setenv("DIVESAFE_RULESET_SHA256", MARK_SHA)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **env)
    assert [e["msg"] for e in caught.value.errors()] == [f"Value error, {MET_ROLE}"]
    assert MARK_SHA not in str(caught.value) and str(MARK_PATH) not in str(caught.value)


@ENVIRONMENTS
def test_settings_refuse_the_flag_even_with_a_pinned_ruleset(env: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            degraded_may_support_go=True,
            ruleset_path=MARK_PATH,
            ruleset_sha256=MARK_SHA,
            **env,
        )
    assert [e["msg"] for e in caught.value.errors()] == [f"Value error, {MET_ROLE}"]
    off = Settings(_env_file=None, ruleset_path=MARK_PATH, ruleset_sha256=MARK_SHA, **env)
    assert off.degraded_may_support_go is False  # control: only the flag is refused


def test_the_message_is_the_one_settings_and_build_engine_use() -> None:
    assert settings_module.DEGRADED_NEEDS_MET_ROLE == MET_ROLE


# --- c. build_engine repeats the check ---------------------------------------------------------


@pytest.mark.parametrize("how", ["model_copy", "model_construct"])
def test_build_engine_refuses_the_flag_that_bypassed_settings(
    how: str, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned ruleset with a definition: every other start-up condition for the flag holds."""
    path, sha = _pinned_ruleset(tmp_path, tioman())
    base = _settings(path, sha, on=False)
    if how == "model_copy":
        bypassed = base.model_copy(update={"degraded_may_support_go": True})
    else:
        bypassed = Settings.model_construct(**{**dict(base), "degraded_may_support_go": True})
    assert bypassed.degraded_may_support_go is True  # the validator really was skipped
    caplog.set_level(logging.DEBUG, logger=STATE_LOGGER)
    with pytest.raises(RuntimeError) as caught:
        build_engine(bypassed, now=NOW)
    assert str(caught.value) == MET_ROLE
    messages = [r.getMessage() for r in caplog.records]
    assert WARNING_TEXT not in messages and not any("DEGRADED" in m for m in messages)

    # control: the same settings start once the role is built, so the refusal above is this gate
    monkeypatch.setattr(settings_module, "MET_TIGHTEN_ONLY_ROLE_BUILT", True)
    engine = build_engine(bypassed, now=NOW)
    assert engine is not None and engine.policy.degraded_may_support_go is True


# --- d. GO and CAUTION stay unreachable --------------------------------------------------------


def test_four_threshold_factors_have_no_encodable_metric() -> None:
    """A metric for one of these turns this red: the owner condition requires review first."""
    assert set(THRESHOLD_FACTORS) - set(FACTOR_METRICS) == UNENCODABLE
    assert set(FACTOR_METRICS) <= set(THRESHOLD_FACTORS)


def _go_band(factor: RiskFactorKind, metric: str) -> dict[str, Any]:
    unit = SUPPORTED_METRICS[metric].unit
    return tioman(
        id=f"synthetic.{factor.value}",
        factor=factor.value,
        metric=metric,
        unit=unit,
        no_go_when={"comparison": ">", "value": 30.0},
        caution_when={"comparison": ">", "value": 20.0},
        go_when={"comparison": "<=", "value": 20.0},
        forecast_margin=0.0,
    )


def _validated(category: DataCategory, variables: dict[str, float]) -> EvidenceItem:
    return EvidenceItem(
        id=f"ev-{category.value}",
        category=category,
        source="synthetic",
        retrieved_at=NOW,
        valid_at=PAST_START,
        valid_until=PAST_START + timedelta(minutes=PAST_PLAN.planned_duration_minutes),
        is_forecast=False,
        data_kind=DataKind.OBSERVATION,
        quality=DataQuality.VALIDATED,
        transformations=(TransformationStep(step=UNIT_CHECK), TransformationStep(step=RANGE_CHECK)),
        value={
            "variables": variables,
            "units": {m: SUPPORTED_METRICS[m].unit for m in variables},
        },
    )


def _all_validated() -> list[EvidenceItem]:
    by_category: dict[DataCategory, dict[str, float]] = {c: {} for c in DataCategory}
    for metrics in FACTOR_METRICS.values():
        for m in metrics:
            by_category[SUPPORTED_METRICS[m].category][m] = 1.0  # inside every GO band
    return [_validated(c, v) for c, v in by_category.items()]


@pytest.mark.parametrize("on", [False, True], ids=["flag_off", "flag_on_if_the_role_existed"])
def test_a_go_band_for_every_encodable_factor_and_validated_evidence_still_is_not_go(
    on: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production wiring with the most permissive ruleset that can be written today."""
    if on:
        monkeypatch.setattr(settings_module, "MET_TIGHTEN_ONLY_ROLE_BUILT", True)
    definitions = [_go_band(f, m) for f, ms in FACTOR_METRICS.items() for m in sorted(ms)]
    engine = build_engine(_settings(*_pinned_ruleset(tmp_path, *definitions), on=on), now=NOW)
    assert engine is not None
    result = engine.assess(PAST_PLAN, _all_validated(), NOW)
    outcomes = {r.rule_id: r.outcome for r in result.rule_results}

    # controls: every signed-off definition returned GO on VALIDATED evidence
    assert {f"definition.synthetic.{f.value}" for f in FACTOR_METRICS} <= set(outcomes)
    assert all(outcomes[f"definition.synthetic.{f.value}"] is R.GO for f in FACTOR_METRICS)
    assert result.rests_on_degraded_evidence is False

    assert set(result.unevaluated_factors) == UNENCODABLE
    placeholders = {i for i in outcomes if i.startswith("placeholder.")}
    assert placeholders == {f"placeholder.{f.value}" for f in UNENCODABLE}
    assert all(outcomes[i] is R.INSUFFICIENT_EVIDENCE for i in placeholders)
    # the placeholders are the only blockers (a list, so a duplicated rule id cannot hide one)
    assert sorted(r.rule_id for r in result.rule_results if r.outcome is not R.GO) == sorted(
        placeholders
    )
    assert result.recommendation is R.INSUFFICIENT_EVIDENCE
