"""The per-stage trace on the record (ADR 0025 item 3).

The trace is an audit of WHEN each stage ran, HOW it ended, WHICH evidence it used and WHICH
fixed codes describe its problems. It is not a reasoning trace: no prompts, no model text, no
provider error text, nothing copied from `evidence_issues` or `agent_issues`. Every code comes
from a structured source (`connector_outcomes`, `task_outcomes`, rule ids, the reconciliation
flag, the verdict), never from a string meant for people.

Failure paths first: what the trace refuses, then what the pipeline writes. Offline: fixed
clocks, FakeProvider, in-memory connectors.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from tests.agent import scripted_llm as sl
from tests.safety.test_evidence_conflict import _Source, waves, wind
from tests.safety.test_evidence_withheld import fresh
from tests.safety.test_supervisor import (
    SUPERVISOR_NOTICE,
    _lift_upstream_id_guards,
    _record_with_failed_check,
)

from divesafe.agents import run_agent_stage
from divesafe.agents.specialists import SPECIALISTS
from divesafe.agents.stage import RISK_TASK, TaskOutcome
from divesafe.agents.tot import evaluate_scenarios, evaluate_scenarios_with_outcomes
from divesafe.data import TIOMAN_ISLAND, FetchResult
from divesafe.data.errors import ConnectorError
from divesafe.domain import (
    AssessmentRecord,
    Recommendation,
    ScenarioKind,
    StageName,
    StageOutcome,
    SupervisorCheck,
)
from divesafe.domain.stages import (
    ISSUE_CODE_PATTERN,
    MAX_CONNECTORS,
    MAX_EVIDENCE_ITEMS,
    MAX_ISSUE_CODE_CHARS,
    MAX_STAGE_ISSUES,
    OVERFLOW,
    QUALIFIER_PATTERN,
    STAGE_ORDER,
    TRACE_ISSUE_KINDS,
    UNNAMED_QUALIFIER,
    StageTrace,
    bound_issues,
    issue_code,
    unnamed_qualifier,
)
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.orchestration.graph import STAGES
from divesafe.risk import EvidencePolicy, RiskRulesEngine
from divesafe.services.evidence import ConnectorOutcome, connector_outcomes

pytestmark = pytest.mark.safety

R = Recommendation
N = StageName
O = StageOutcome  # noqa: E741
NOW = sl.NOW
LATER = NOW + timedelta(minutes=1)
CODE = re.compile(ISSUE_CODE_PATTERN)
MARKER = "SECRET-MARKER http://evil.example"


def trace(**overrides: Any) -> StageTrace:
    fields: dict[str, Any] = {
        "name": N.OBSERVE,
        "outcome": O.COMPLETED,
        "next_stage": N.RETRIEVE,
        "started_at": NOW,
        "finished_at": NOW,
    }
    fields.update(overrides)
    return StageTrace(**fields)


def refuses(fragment: str, **overrides: Any) -> bool:
    try:
        trace(**overrides)
    except ValidationError as exc:
        return fragment in str(exc)
    return False


# --- 1. StageTrace validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "observe.connector_failed:Bad Name",  # space
        "observe.connector_failed:<script>",  # angle bracket
        "observe.connector_failed:a\nb",  # newline
        "observe.connector_failed:" + "a" * 41,  # qualifier over 40
        "Observe.connector_failed",  # upper case
        "observe",  # no kind
        "observe.connector-failed",  # hyphen in kind
        "observe.connector_failed:",  # empty qualifier
        "observe.connector_failed:x:y",  # two qualifiers
        "observe.connector_failed: name",
        "http://evil.example",
        "",
    ],
)
def test_an_issue_code_that_does_not_match_the_pattern_is_refused(code: str) -> None:
    assert not CODE.match(code)
    with pytest.raises(ValidationError):
        trace(issues=(code,))


def test_an_issue_code_over_the_length_cap_is_refused() -> None:
    # Shape-valid under the pattern would need a 40-char qualifier; use a long kind instead and
    # check both the cap and that the pattern alone would not have saved it.
    long_kind = "observe." + "k" * (MAX_ISSUE_CODE_CHARS - len("observe.") + 1)
    assert len(long_kind) == MAX_ISSUE_CODE_CHARS + 1
    assert CODE.match(long_kind)  # the pattern accepts it; only the cap refuses
    with pytest.raises(ValidationError):
        trace(issues=(long_kind,))


def test_a_code_from_another_stage_or_an_unknown_kind_is_refused() -> None:
    assert refuses("own stage's prefix", issues=("reason.timed_out",))
    assert refuses("fixed codes", issues=("observe.made_up",))
    assert refuses("fixed codes", issues=("observe.connector_failed",))  # needs a qualifier
    assert refuses("fixed codes", issues=("observe.no_evidence:x",))  # takes none
    assert refuses("overflow takes no qualifier", issues=("observe.overflow:x",))
    assert trace(issues=("observe.overflow",)).issues == ("observe.overflow",)


def test_duplicate_issue_codes_are_refused() -> None:
    assert refuses("unique", issues=("observe.no_evidence", "observe.no_evidence"))


def test_more_than_max_stage_issues_is_refused() -> None:
    codes = tuple(f"observe.connector_failed:c{i}" for i in range(MAX_STAGE_ISSUES + 1))
    with pytest.raises(ValidationError):
        trace(issues=codes)
    assert len(trace(issues=codes[:MAX_STAGE_ISSUES]).issues) == MAX_STAGE_ISSUES


def test_finished_before_started_is_refused() -> None:
    assert refuses("finished_at is before started_at", started_at=LATER, finished_at=NOW)
    assert trace(started_at=NOW, finished_at=LATER).finished_at == LATER


def test_more_than_max_evidence_items_ids_is_refused() -> None:
    ids = tuple(f"ev-{i}" for i in range(MAX_EVIDENCE_ITEMS + 1))
    with pytest.raises(ValidationError):
        trace(evidence_ids=ids)
    assert len(trace(evidence_ids=ids[:MAX_EVIDENCE_ITEMS]).evidence_ids) == MAX_EVIDENCE_ITEMS
    assert refuses("unique", evidence_ids=("a", "a"))


def test_an_extra_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        trace(prompt="never")
    with pytest.raises(ValidationError):
        trace(text=MARKER)


def test_trace_fields_are_fixed_enums_and_times_only() -> None:
    assert set(StageTrace.model_fields) == {
        "name",
        "outcome",
        "next_stage",
        "started_at",
        "finished_at",
        "evidence_ids",
        "issues",
        "duration_ms",
    }
    with pytest.raises(ValidationError):
        trace(outcome="exploded")
    with pytest.raises(ValidationError):
        trace(next_stage="elsewhere")


def test_bound_issues_dedups_keeps_order_and_appends_overflow_past_the_bound() -> None:
    assert bound_issues(["observe.no_evidence", "observe.no_evidence"], N.OBSERVE) == (
        "observe.no_evidence",
    )
    assert bound_issues(["observe.b", "observe.a", "observe.b"], N.OBSERVE) == (
        "observe.b",
        "observe.a",
    )
    codes = [f"observe.connector_failed:c{i}" for i in range(MAX_STAGE_ISSUES + 5)]
    bounded = bound_issues(codes + codes, N.OBSERVE)  # duplicates are dropped first
    assert len(bounded) == MAX_STAGE_ISSUES
    assert bounded[:-1] == tuple(codes[: MAX_STAGE_ISSUES - 1])
    assert bounded[-1] == "observe.overflow"
    exact = bound_issues(codes[:MAX_STAGE_ISSUES], N.OBSERVE)
    assert exact == tuple(codes[:MAX_STAGE_ISSUES]) and "observe.overflow" not in exact
    assert trace(issues=bounded).issues == bounded  # the bounded list is storable


def test_issue_code_refuses_a_kind_the_stage_does_not_have() -> None:
    with pytest.raises(ValueError, match="not an issue kind"):
        issue_code(N.OBSERVE, "timed_out")
    with pytest.raises(ValueError, match="not an issue kind"):
        issue_code(N.GATE, "anything")
    with pytest.raises(ValueError, match="qualifier mismatch"):
        issue_code(N.OBSERVE, "connector_failed")
    with pytest.raises(ValueError, match="qualifier mismatch"):
        issue_code(N.OBSERVE, "no_evidence", "x")
    with pytest.raises(ValueError, match="overflow takes no qualifier"):
        issue_code(N.OBSERVE, OVERFLOW, "x")


@pytest.mark.parametrize(
    "qualifier",
    ["bad name", "<script>", "a\nb", "x" * 60, "", MARKER, "Upper", "dots.too", "ünïcode"],
)
def test_issue_code_turns_a_bad_qualifier_into_unnamed_never_free_text(qualifier: str) -> None:
    code = issue_code(N.OBSERVE, "connector_failed", qualifier)
    assert code == f"observe.connector_failed:{UNNAMED_QUALIFIER}"
    assert qualifier not in code or qualifier == ""
    assert CODE.match(code)


def test_issue_code_keeps_a_registered_name() -> None:
    assert issue_code(N.OBSERVE, "connector_failed", "open-meteo_marine2") == (
        "observe.connector_failed:open-meteo_marine2"
    )
    assert issue_code(N.OBSERVE, "connector_failed", "x" * 40).endswith(":" + "x" * 40)


# --- 2. Record validation -----------------------------------------------------------------------


def chain(
    *names: StageName, kept: Sequence[str] = (), **per_stage: dict[str, Any]
) -> tuple[StageTrace, ...]:
    """Traces for `names` in the given order with a correct next_stage chain. The observe entry
    lists `kept` (the record's evidence ids) unless overridden, as the record demands."""
    out = []
    for index, name in enumerate(names):
        following = names[index + 1] if index + 1 < len(names) else None
        fields: dict[str, Any] = {"evidence_ids": tuple(kept)} if name is N.OBSERVE else {}
        fields.update(per_stage.get(name.value, {}))
        out.append(trace(name=name, next_stage=following, outcome=O.COMPLETED, **fields))
    return tuple(out)


def kept_ids(document: dict[str, Any]) -> tuple[str, ...]:
    return tuple(e["id"] for e in document["evidence"])


def record_doc() -> dict[str, Any]:
    return sl.run_pipeline(sl.fake()).model_dump(mode="json")


def with_stages(base: dict[str, Any], stages: Sequence[StageTrace]) -> dict[str, Any]:
    return {**base, "stages": [t.model_dump(mode="json") for t in stages]}


def record_refuses(document: dict[str, Any], fragment: str) -> bool:
    try:
        AssessmentRecord.model_validate(document)
    except ValidationError as exc:
        return fragment in str(exc)
    return False


def test_two_traces_for_one_stage_are_refused() -> None:
    base = record_doc()
    kept = kept_ids(base)
    twice = (*chain(N.OBSERVE, N.RETRIEVE, kept=kept)[:1], *chain(N.OBSERVE, N.RETRIEVE, kept=kept))
    assert record_refuses(with_stages(base, twice), "at most one stage trace per stage")


def test_traces_out_of_graph_order_are_refused() -> None:
    base = record_doc()
    kept = kept_ids(base)
    assert record_refuses(with_stages(base, chain(N.RETRIEVE, N.OBSERVE, kept=kept)), "graph order")
    assert record_refuses(with_stages(base, chain(N.GATE, N.OBSERVE, kept=kept)), "graph order")


def test_a_broken_next_stage_chain_is_refused() -> None:
    base = record_doc()
    kept = kept_ids(base)
    broken = (
        # skips the retrieve entry that follows
        trace(name=N.OBSERVE, next_stage=N.ASSESS, evidence_ids=kept),
        trace(name=N.RETRIEVE, next_stage=None),
    )
    assert record_refuses(with_stages(base, broken), "next_stage")
    pointing_back = (trace(name=N.OBSERVE, next_stage=N.OBSERVE, evidence_ids=kept),)
    assert record_refuses(with_stages(base, pointing_back), "next_stage")


def test_the_last_trace_must_have_next_stage_none() -> None:
    base = record_doc()
    kept = kept_ids(base)
    dangling = (trace(name=N.OBSERVE, next_stage=N.RETRIEVE, evidence_ids=kept),)
    assert record_refuses(with_stages(base, dangling), "next_stage")
    ok = AssessmentRecord.model_validate(with_stages(base, chain(N.OBSERVE, kept=kept)))
    assert ok.stages[-1].next_stage is None
    full = AssessmentRecord.model_validate(with_stages(base, chain(*STAGE_ORDER, kept=kept)))
    assert [t.next_stage for t in full.stages] == [*STAGE_ORDER[1:], None]


def test_a_gap_in_the_chain_is_allowed_only_when_next_stage_says_so() -> None:
    """A trace list may omit stages (older partial records) but each entry must name the next
    entry; naming the graph's next stage when that stage has no entry is a broken chain."""
    base = record_doc()
    kept = kept_ids(base)
    assert AssessmentRecord.model_validate(with_stages(base, chain(N.OBSERVE, N.GATE, kept=kept)))
    assert record_refuses(
        with_stages(
            base,
            (
                trace(name=N.OBSERVE, next_stage=N.RETRIEVE, evidence_ids=kept),
                trace(name=N.GATE, next_stage=None),
            ),
        ),
        "next_stage",
    )


def test_a_trace_evidence_id_not_in_the_record_is_refused() -> None:
    base = record_doc()
    kept = kept_ids(base)
    ghost = chain(N.OBSERVE, observe={"evidence_ids": (*kept, "ghost-id")})
    assert record_refuses(with_stages(base, ghost), "cited evidence ids not present")
    in_reason = chain(N.OBSERVE, N.REASON, kept=kept, reason={"evidence_ids": ("ghost-id",)})
    assert record_refuses(with_stages(base, in_reason), "cited evidence ids not present")
    fine = chain(N.OBSERVE, N.REASON, kept=kept, reason={"evidence_ids": (kept[0],)})
    loaded = AssessmentRecord.model_validate(with_stages(base, fine))
    assert loaded.stages[0].evidence_ids == kept
    assert loaded.stages[1].evidence_ids == (kept[0],)


def test_a_withheld_id_in_a_trace_is_refused() -> None:
    record = _conflict_record(sl.fake())
    assert record.withheld_evidence  # control
    held = record.withheld_evidence[0].id
    assert held not in {e.id for e in record.evidence}
    base = record.model_dump(mode="json")
    kept = kept_ids(base)
    forged = chain(N.OBSERVE, observe={"evidence_ids": (*kept, held)})
    assert record_refuses(with_stages(base, forged), "cited evidence ids not present")
    in_reason = chain(N.OBSERVE, N.REASON, kept=kept, reason={"evidence_ids": (held,)})
    assert record_refuses(with_stages(base, in_reason), "cited evidence ids not present")


def test_stages_is_the_last_top_level_field_defaulted_to_empty_with_the_graph_bound() -> None:
    """ADR 0012's replace rule: a new defaulted top-level field, last, so old rows load and
    `_without_new_defaults` can omit it."""
    fields = list(AssessmentRecord.model_fields)
    assert fields[-1] == "stages"
    field = AssessmentRecord.model_fields["stages"]
    assert field.default == ()
    assert not field.is_required()
    assert any(getattr(m, "max_length", None) == len(STAGE_ORDER) for m in field.metadata)
    base = record_doc()
    assert len(base["stages"]) == len(STAGE_ORDER)
    del base["stages"]
    assert AssessmentRecord.model_validate(base).stages == ()
    too_many = [*chain(*STAGE_ORDER, kept=kept_ids(base)), trace(name=N.GATE, next_stage=None)]
    with pytest.raises(ValidationError):
        AssessmentRecord.model_validate(with_stages(record_doc(), too_many))


def test_the_graph_agrees_with_the_domain_order() -> None:
    assert tuple(s.name for s in STAGES) == STAGE_ORDER


# --- 3. The code list ---------------------------------------------------------------------------


def every_code() -> dict[StageName, list[str]]:
    """Every code each stage can emit today, from the registered names."""
    qualifiers: dict[str, list[str]] = {
        "connector": [f"c{i}" for i in range(MAX_CONNECTORS)],
        "specialist": [s.name for s in SPECIALISTS],
        "scenario kind": [k.value for k in ScenarioKind],
        "supervisor check": [c.value for c in SupervisorCheck],
    }
    out: dict[StageName, list[str]] = {}
    for stage, kinds in TRACE_ISSUE_KINDS.items():
        codes = [issue_code(stage, OVERFLOW)]
        for kind, takes in kinds.items():
            if takes is None:
                codes.append(issue_code(stage, kind))
            else:
                codes.extend(issue_code(stage, kind, q) for q in qualifiers[takes])
        out[stage] = codes
    return out


def test_every_kind_produces_a_code_matching_the_pattern_and_storable() -> None:
    for stage, codes in every_code().items():
        for code in codes:
            assert CODE.match(code), code
            assert len(code) <= MAX_ISSUE_CODE_CHARS
            assert code.startswith(f"{stage.value}.")
        assert len(set(codes)) == len(codes)
        assert trace(name=stage, next_stage=None, issues=tuple(codes[:MAX_STAGE_ISSUES]))


def test_every_stage_has_kinds_and_the_qualifier_meanings_are_the_registered_ones() -> None:
    assert set(TRACE_ISSUE_KINDS) == set(STAGE_ORDER)
    meanings = {m for kinds in TRACE_ISSUE_KINDS.values() for m in kinds.values() if m}
    assert meanings == {"connector", "specialist", "scenario kind", "supervisor check"}


def test_the_enumerated_maximum_per_stage_fits_the_bound_so_overflow_is_unreachable() -> None:
    maxima = {stage: len(codes) - 1 for stage, codes in every_code().items()}  # minus overflow
    assert maxima[N.OBSERVE] == 3 * MAX_CONNECTORS + 2
    assert maxima[N.REASON] == 2 + len(SPECIALISTS) + len(ScenarioKind) + 2
    assert maxima[N.REJUDGE] == 3
    assert maxima[N.SUPERVISE] == len(SupervisorCheck) + 1
    assert max(maxima.values()) <= MAX_STAGE_ISSUES
    assert maxima[N.OBSERVE] == MAX_STAGE_ISSUES  # the bound is set from the widest stage
    # A single Observe run emits at most one code per connector, so the practical maximum is
    # even lower; the `overflow` branch is only reachable by a future change.
    assert MAX_CONNECTORS + 2 < MAX_STAGE_ISSUES


def test_max_connectors_covers_every_connector_registrable_today() -> None:
    from divesafe.api import state as state_module

    source = inspect.getsource(state_module.build_default_state)
    registrable = source.count("Connector(")
    assert registrable == 6  # the four always-on plus MET and PHN (opt-in); update with the code
    assert registrable <= MAX_CONNECTORS


# --- 4. Never copied text -----------------------------------------------------------------------


class _Failing:
    name = "failing"

    async def fetch(self, *args: object) -> FetchResult:
        raise ConnectorError(f"upstream said {MARKER}")


class _WithIssue:
    name = "with-issue"

    async def fetch(self, *args: object) -> FetchResult:
        return FetchResult((fresh(waves(1)),), issues=(f"waves: {MARKER}",))


class _Stuck:
    name = "stuck"

    async def fetch(self, *args: object) -> FetchResult:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


class _BadName:
    name = f"Bad Name {MARKER}"

    async def fetch(self, *args: object) -> FetchResult:
        raise ConnectorError("x")


def _engine() -> RiskRulesEngine:
    return RiskRulesEngine(
        [sl._GoRule()],
        EvidencePolicy(
            frozenset({waves(1).category}), timedelta(hours=1), degraded_may_support_go=True
        ),
        "synthetic",
        required_factors=frozenset(),
    )


def _assess(
    connectors: Sequence[Any],
    provider: Any = None,
    *,
    clock: Any = None,
    engine: RiskRulesEngine | None = None,
    **extra: Any,
) -> AssessmentRecord:
    if provider is not None and clock is None:
        clock = lambda: NOW  # noqa: E731
    return asyncio.run(
        pipeline_module.assess_dive(
            assessment_id="t-1",
            plan=sl.PLAN,
            site=TIOMAN_ISLAND,
            connectors=connectors,
            engine=engine or _engine(),
            now=NOW,
            provider=provider,
            clock=clock,
            **extra,
        )
    )


def stages_json(record: AssessmentRecord) -> str:
    return json.dumps([t.model_dump(mode="json") for t in record.stages])


def codes_of(record: AssessmentRecord) -> dict[StageName, tuple[str, ...]]:
    return {t.name: t.issues for t in record.stages}


def test_connector_and_provider_text_never_reaches_the_trace() -> None:
    marker_provider = sl.FakeProvider(
        sl.scripted(
            overrides={
                "specialist:ocean_conditions": f"garbage {MARKER}",
                "scenario:marginal": f"garbage {MARKER}",
                "risk_assessment": f"garbage {MARKER}",
            }
        )
    )
    record = _assess([_Failing(), _WithIssue(), _BadName()], marker_provider)

    # Controls: the human-facing lists still carry the connector text and the agent issues.
    assert any(MARKER in issue for issue in record.evidence_issues)
    assert any("specialist:ocean_conditions" in issue for issue in record.agent_issues)
    assert any("risk_assessment" in issue for issue in record.agent_issues)

    dumped = stages_json(record)
    assert MARKER not in dumped
    assert "SECRET" not in dumped and "evil.example" not in dumped and "http" not in dumped
    assert "garbage" not in dumped and "upstream" not in dumped
    # No free text at all: every string in the trace is an enum value, an id, a time or a code.
    for entry in record.stages:
        for code in entry.issues:
            assert CODE.match(code)

    assert codes_of(record)[N.OBSERVE] == (
        "observe.connector_failed:failing",
        "observe.connector_issue:with-issue",
        f"observe.connector_failed:{UNNAMED_QUALIFIER}_1",  # the bad name is never written
    )
    assert codes_of(record)[N.ASSESS] == ("assess.connector_issues_rule",)
    assert codes_of(record)[N.REASON] == (
        "reason.specialist_failed:ocean_conditions",
        "reason.scenario_failed:marginal",
        "reason.proposal_failed",
    )
    assert codes_of(record)[N.RETRIEVE] == ("retrieve.not_configured",)
    assert codes_of(record)[N.REJUDGE] == ()
    assert codes_of(record)[N.PROPOSE] == ()
    assert codes_of(record)[N.SUPERVISE] == ()
    assert codes_of(record)[N.GATE] == ()


def test_the_trace_never_reads_the_issue_strings() -> None:
    """Source-level: the pipeline's code builders never touch `issues`/`agent_issues` text."""
    source = inspect.getsource(pipeline_module._trace_codes)
    assert ".issues" not in source
    assert "agent_issues" not in source
    assert "evidence_issues" not in source
    assert "rationale" not in source and "explanation" not in source


# --- 5. Pipeline trace content ------------------------------------------------------------------


def test_an_all_completed_run_has_eight_traces_in_order_with_the_right_chain() -> None:
    record = sl.run_pipeline(sl.fake())
    assert [t.name for t in record.stages] == list(STAGE_ORDER)
    assert [t.next_stage for t in record.stages] == [*STAGE_ORDER[1:], None]
    outcomes = {t.name: t.outcome for t in record.stages}
    assert outcomes == {
        **dict.fromkeys(STAGE_ORDER, O.COMPLETED),
        N.RETRIEVE: O.NOT_CONFIGURED,
    }
    assert codes_of(record)[N.RETRIEVE] == ("retrieve.not_configured",)
    assert codes_of(record)[N.OBSERVE] == ()
    assert codes_of(record)[N.REASON] == ()


def test_without_a_provider_reason_is_skipped_with_its_code() -> None:
    record = _assess([_WithIssue()])
    reason = next(t for t in record.stages if t.name is N.REASON)
    assert reason.outcome is O.SKIPPED
    assert reason.issues == ("reason.skipped_no_provider",)
    assert reason.evidence_ids == ()
    assert reason.next_stage is N.REJUDGE


def test_a_slow_provider_gives_reason_timed_out_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Slow:
        name, model, external = "slow", "m", False

        async def complete(self, request: object) -> object:
            await asyncio.sleep(2)
            raise AssertionError("unreachable")

    monkeypatch.setattr("divesafe.agents.runner.STAGE_TIMEOUT_SECONDS", 0.05)
    record = sl.run_pipeline(_Slow())
    reason = next(t for t in record.stages if t.name is N.REASON)
    assert reason.outcome is O.TIMED_OUT
    assert "reason.timed_out" in reason.issues
    assert reason.next_stage is N.REJUDGE
    assert record.stages[-1].name is N.GATE


def _conflict_record(provider: Any = None) -> AssessmentRecord:
    secret = 424242
    return _assess(
        [
            _Source("a", fresh(waves(1)), fresh(wind(secret))),
            _Source("b", fresh(wind(secret + 1))),
        ],
        provider,
    )


def test_observe_evidence_ids_are_the_kept_ids_and_never_a_withheld_one() -> None:
    record = _conflict_record()
    observe = record.stages[0]
    assert observe.name is N.OBSERVE
    assert observe.evidence_ids == tuple(e.id for e in record.evidence) == ("ev-waves_swell",)
    assert "ev-wind" in {e.id for e in record.withheld_evidence}
    assert "ev-wind" not in stages_json(record)
    assert "observe.evidence_conflict" in observe.issues
    assert "424242" not in stages_json(record)


def test_evidence_conflict_code_is_absent_without_a_conflict() -> None:
    record = _assess([_Source("a", fresh(waves(1)), fresh(wind(1))), _Source("b", fresh(wind(1)))])
    assert "observe.evidence_conflict" not in record.stages[0].issues
    assert record.stages[0].evidence_ids == ("ev-waves_swell", "ev-wind")


def test_no_evidence_code_when_nothing_was_fetched() -> None:
    record = _assess([_Failing()])
    assert record.stages[0].issues == ("observe.connector_failed:failing", "observe.no_evidence")
    assert record.stages[0].evidence_ids == ()


def test_a_connector_timeout_gets_its_own_code() -> None:
    record = _assess([_Stuck(), _WithIssue()], connector_timeout_seconds=0.05)
    assert record.stages[0].issues == (
        "observe.connector_timed_out:stuck",
        "observe.connector_issue:with-issue",
    )


def test_supervise_carries_the_tightened_code_on_a_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lift_upstream_id_guards(monkeypatch, "proposal")
    record = sl.run_pipeline(
        sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False
    )
    assert SUPERVISOR_NOTICE in record.agent_issues  # control: the tightening happened
    supervise = next(t for t in record.stages if t.name is N.SUPERVISE)
    # GO rules, CAUTION proposal: the first reconcile accepted it; the tightening to INSUFFICIENT
    # EVIDENCE made the second reconcile discard it, so the downgrade code sits on this entry.
    assert supervise.issues == (
        "supervise.tightened:citations_valid",
        "supervise.llm_attempted_downgrade",
    )
    assert record.llm_attempted_downgrade is True
    assert next(t for t in record.stages if t.name is N.PROPOSE).issues == ()
    assert supervise.evidence_ids == ()


def test_supervise_has_no_code_when_every_check_passed() -> None:
    record = sl.run_pipeline(sl.fake())
    assert next(t for t in record.stages if t.name is N.SUPERVISE).issues == ()


def test_rejudge_carries_window_started_when_the_start_passes_during_the_run() -> None:
    after_start = sl.START + timedelta(minutes=1)
    clean = _Source("a", fresh(waves(1)))
    record = _assess([clean], sl.fake(), clock=lambda: after_start)
    rejudge = next(t for t in record.stages if t.name is N.REJUDGE)
    assert rejudge.issues == (
        "rejudge.window_started",
        "rejudge.result_changed",
        "rejudge.rules_added",
    )
    assert record.deterministic_recommendation is R.INSUFFICIENT_EVIDENCE  # GO before
    assert rejudge.finished_at == after_start
    # Already INSUFFICIENT EVIDENCE from a connector issue: the window code, no change code.
    unchanged = _assess([_WithIssue()], sl.fake(), clock=lambda: after_start)
    assert next(t for t in unchanged.stages if t.name is N.REJUDGE).issues == (
        "rejudge.window_started",
        "rejudge.rules_added",
    )


def test_rejudge_has_no_code_when_nothing_changed() -> None:
    record = sl.run_pipeline(sl.fake())
    assert next(t for t in record.stages if t.name is N.REJUDGE).issues == ()


def test_propose_carries_the_downgrade_code_when_the_proposal_is_lower() -> None:
    record = sl.run_pipeline(sl.fake(recommendation="GO"))  # rules: INSUFFICIENT EVIDENCE
    assert record.llm_attempted_downgrade is True
    assert next(t for t in record.stages if t.name is N.PROPOSE).issues == (
        "propose.llm_attempted_downgrade",
    )
    higher = sl.run_pipeline(sl.fake(recommendation="NO-GO"))
    assert next(t for t in higher.stages if t.name is N.PROPOSE).issues == ()


def test_assess_and_reason_evidence_ids_are_the_cited_ids_present_in_the_evidence() -> None:
    record = sl.run_pipeline(sl.fake())
    known = {e.id for e in record.evidence}
    by_name = {t.name: t for t in record.stages}
    rule_cited = {i for r in record.rule_results for i in r.evidence_ids} & known
    assert set(by_name[N.ASSESS].evidence_ids) == rule_cited
    assert set(by_name[N.REJUDGE].evidence_ids) == rule_cited
    agent_cited = {i for f in record.findings for i in f.evidence_ids}
    agent_cited |= {i for s in record.scenarios for i in s.evidence_ids}
    agent_cited |= set(record.proposal_evidence_ids)
    assert agent_cited  # control
    assert set(by_name[N.REASON].evidence_ids) == agent_cited & known
    for name in (N.RETRIEVE, N.PROPOSE, N.SUPERVISE, N.GATE):
        assert by_name[name].evidence_ids == ()
    # Every trace id is a kept id, in evidence order, without duplicates.
    order = [e.id for e in record.evidence]
    for entry in record.stages:
        assert list(entry.evidence_ids) == [i for i in order if i in entry.evidence_ids]


def test_assess_carries_unevaluated_factors_when_the_engine_reports_any() -> None:
    record = sl.run_pipeline(sl.fake(), risk_engine=sl.engine(with_warning_rule=False))
    assess = next(t for t in record.stages if t.name is N.ASSESS)
    assert ("assess.unevaluated_factors" in assess.issues) == bool(record.unevaluated_factors)


# --- 6. Timestamps ------------------------------------------------------------------------------


def test_without_a_clock_every_trace_is_stamped_now() -> None:
    record = _assess([_WithIssue()])
    assert len(record.stages) == len(STAGE_ORDER)
    for entry in record.stages:
        assert entry.started_at == entry.finished_at == NOW
    assert record.created_at == NOW


def test_with_a_clock_stages_through_rejudge_carry_now_and_later_ones_the_completed_time() -> None:
    reads: list[int] = []

    def clock() -> datetime:
        reads.append(1)
        return LATER

    record = _assess([_WithIssue()], sl.fake(), clock=clock)
    assert len(reads) == 1  # the existing contract: the clock is read exactly once
    by_name = {t.name: t for t in record.stages}
    for name in (N.OBSERVE, N.RETRIEVE, N.ASSESS, N.REASON):
        assert by_name[name].started_at == by_name[name].finished_at == NOW
    assert by_name[N.REJUDGE].started_at == NOW
    assert by_name[N.REJUDGE].finished_at == LATER
    for name in (N.PROPOSE, N.SUPERVISE, N.GATE):
        assert by_name[name].started_at == by_name[name].finished_at == LATER
    assert record.created_at == LATER


def test_a_clock_that_goes_backwards_never_yields_finished_before_started() -> None:
    record = _assess([_WithIssue()], sl.fake(), clock=lambda: NOW - timedelta(hours=1))
    for entry in record.stages:
        assert entry.started_at == entry.finished_at == NOW


# --- 7. Structured sources ----------------------------------------------------------------------


def test_connector_outcomes_labels_each_connector_from_the_structured_triple() -> None:
    ok = FetchResult((fresh(waves(1)),))
    with_issue = FetchResult((), issues=(f"x: {MARKER}",))
    labels = connector_outcomes(
        [
            ("a", ok, None),
            ("b", with_issue, None),
            ("c", None, f"ConnectorTransportError: {MARKER}"),
            ("d", None, "TimeoutError: no answer within the time allowed"),
            ("e", None, None),
        ]
    )
    assert labels == (
        ConnectorOutcome("a", "ok"),
        ConnectorOutcome("b", "issues"),
        ConnectorOutcome("c", "failed"),
        ConnectorOutcome("d", "timed_out"),
        ConnectorOutcome("e", "failed"),
    )
    assert MARKER not in repr(labels)


def test_a_connector_message_that_merely_mentions_timeout_is_not_a_timeout() -> None:
    labels = connector_outcomes([("c", None, "ConnectorResponseError: TimeoutError: fake")])
    assert labels == (ConnectorOutcome("c", "failed"),)


def test_task_outcomes_list_every_task_with_the_right_label_on_a_mixed_run() -> None:
    provider = sl.FakeProvider(
        sl.scripted(overrides={"specialist:weather": "garbage", "scenario:marginal": "garbage"})
    )
    record = sl.run_pipeline(provider)
    evidence = record.evidence
    stage = asyncio.run(
        run_agent_stage(provider, sl.PLAN, evidence, record.rule_results, R.INSUFFICIENT_EVIDENCE)
    )
    outcomes = {t.task: t.outcome for t in stage.task_outcomes}
    assert set(outcomes) == {s.name for s in SPECIALISTS} | {k.value for k in ScenarioKind} | {
        RISK_TASK
    }
    assert outcomes["weather"] == "failed"
    assert outcomes["marginal"] == "failed"
    assert outcomes[RISK_TASK] == "ok"
    assert all(outcomes[s.name] == "ok" for s in SPECIALISTS if s.name != "weather")
    assert all(outcomes[k.value] == "ok" for k in ScenarioKind if k.value != "marginal")
    assert all(t.outcome in ("ok", "failed", "timed_out", "skipped") for t in stage.task_outcomes)
    assert isinstance(stage.task_outcomes[0], TaskOutcome)


def test_task_outcomes_mark_the_proposal_skipped_with_no_evidence() -> None:
    provider = sl.fake()
    stage = asyncio.run(run_agent_stage(provider, sl.PLAN, (), (), R.INSUFFICIENT_EVIDENCE))
    outcomes = {t.task: t.outcome for t in stage.task_outcomes}
    assert outcomes[RISK_TASK] == "skipped"
    assert all(outcomes[k.value] == "skipped" for k in ScenarioKind)


def test_evaluate_scenarios_returns_what_the_outcome_variant_does_minus_the_labels() -> None:
    record = sl.run_pipeline(sl.fake())
    evidence, findings, rules = record.evidence, record.findings, record.rule_results
    plain = sl.FakeProvider(sl.scripted(overrides={"scenario:deteriorating": "garbage"}))
    labelled = sl.FakeProvider(sl.scripted(overrides={"scenario:deteriorating": "garbage"}))
    scenarios, issues = asyncio.run(evaluate_scenarios(plain, sl.PLAN, evidence, findings, rules))
    with_labels = asyncio.run(
        evaluate_scenarios_with_outcomes(labelled, sl.PLAN, evidence, findings, rules)
    )
    assert with_labels[0] == scenarios
    assert with_labels[1] == issues
    assert [k.value for k in ScenarioKind if k.value == "deteriorating"] == ["deteriorating"]
    assert dict(with_labels[2]) == {
        ScenarioKind.FAVOURABLE: "ok",
        ScenarioKind.MARGINAL: "ok",
        ScenarioKind.DETERIORATING: "failed",
    }
    assert len(issues) == 1 and "deteriorating" in issues[0]
    assert len(plain.requests) == len(labelled.requests) == len(ScenarioKind)


# --- 8. Logging ---------------------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_stage_finished_logs_carry_only_name_outcome_duration_and_a_count() -> None:
    sink = _Collect()
    logger = logging.getLogger("divesafe.orchestration.pipeline")
    logger.addHandler(sink)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        marker_provider = sl.FakeProvider(
            sl.scripted(overrides={"risk_assessment": f"garbage {MARKER}"})
        )
        _assess([_Failing(), _WithIssue()], marker_provider)
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous)

    finished = [r for r in sink.records if r.getMessage() == "stage finished"]
    assert [r.__dict__["stage"] for r in finished] == [n.value for n in STAGE_ORDER]
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    for entry in finished:
        extras = {k: v for k, v in entry.__dict__.items() if k not in standard}
        assert set(extras) == {"assessment_id", "stage", "outcome", "duration_ms", "issues"}
        assert extras["assessment_id"] == "t-1"
        assert isinstance(extras["duration_ms"], int) and extras["duration_ms"] >= 0
        assert isinstance(extras["issues"], int)
        assert extras["outcome"] in {o.value for o in O}
    everything = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in sink.records)
    assert MARKER not in everything
    assert "garbage" not in everything
    assert "upstream" not in everything


# --- 9. Reviewer fixes: numbered unnamed qualifiers, the two new codes, the record cross-checks --


@pytest.mark.parametrize(
    "name",
    ["failing\n", "open-meteo\n", "a\n\n", "\n", "name\r\n", "x" * 40 + "\n"],
)
def test_a_trailing_newline_qualifier_becomes_unnamed_not_a_match(name: str) -> None:
    """`re.match` with `$` accepts a trailing newline; `fullmatch` does not. Such a name must
    be replaced, never written and never crash."""
    assert not QUALIFIER_PATTERN.fullmatch(name)
    assert issue_code(N.OBSERVE, "connector_failed", name) == (
        f"observe.connector_failed:{UNNAMED_QUALIFIER}"
    )
    seen: list[str] = []
    assert unnamed_qualifier(name, seen) == f"{UNNAMED_QUALIFIER}_1"
    assert "\n" not in unnamed_qualifier(name, seen)
    assert CODE.match(issue_code(N.OBSERVE, "connector_failed", unnamed_qualifier(name, seen)))


def test_distinct_bad_connector_names_are_numbered_and_both_kept() -> None:
    seen: list[str] = []
    first = unnamed_qualifier(f"Bad One {MARKER}", seen)
    second = unnamed_qualifier("Bad Two <b>", seen)
    again = unnamed_qualifier(f"Bad One {MARKER}", seen)
    good = unnamed_qualifier("open-meteo_marine", seen)
    assert (first, second, again, good) == (
        "unnamed_1",
        "unnamed_2",
        "unnamed_1",
        "open-meteo_marine",
    )
    codes = [issue_code(N.OBSERVE, "connector_failed", q) for q in (first, second, again)]
    assert bound_issues(codes, N.OBSERVE) == (
        "observe.connector_failed:unnamed_1",
        "observe.connector_failed:unnamed_2",
    )
    # The numbering is capped at MAX_CONNECTORS so the stage bound still holds.
    many: list[str] = []
    labels = [unnamed_qualifier(f"bad {i}", many) for i in range(MAX_CONNECTORS + 3)]
    assert labels[MAX_CONNECTORS - 1] == f"{UNNAMED_QUALIFIER}_{MAX_CONNECTORS}"
    assert set(labels[MAX_CONNECTORS:]) == {f"{UNNAMED_QUALIFIER}_{MAX_CONNECTORS}"}
    assert all(CODE.match(issue_code(N.OBSERVE, "connector_failed", q)) for q in labels)


class _BadName2:
    name = "Another Bad <name>"

    async def fetch(self, *args: object) -> FetchResult:
        raise ConnectorError("y")


def test_two_bad_connector_names_in_one_run_get_unnamed_1_and_unnamed_2() -> None:
    record = _assess([_BadName(), _BadName2(), _WithIssue()])
    assert record.stages[0].issues == (
        "observe.connector_failed:unnamed_1",
        "observe.connector_failed:unnamed_2",
        "observe.connector_issue:with-issue",
    )
    assert "Bad" not in stages_json(record) and "<" not in stages_json(record)


def test_every_registered_connector_name_matches_the_qualifier_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enumerated from the real factory with every opt-in connector switched on."""
    from divesafe.api.state import build_default_state
    from divesafe.config import Settings

    monkeypatch.setenv("DIVESAFE_MET_API_TOKEN", "tok-synthetic-not-a-real-token")
    monkeypatch.setenv("DIVESAFE_PHN_ETIDE_ENABLED", "true")
    monkeypatch.delenv("DIVESAFE_ENVIRONMENT", raising=False)
    state = build_default_state(Settings(_env_file=None))  # type: ignore[call-arg]
    names = [c.name for c in state.connectors]
    assert len(names) == 6 and len(set(names)) == 6  # update with the registry
    assert len(names) <= MAX_CONNECTORS
    for name in names:
        assert QUALIFIER_PATTERN.fullmatch(name), name
        assert issue_code(N.OBSERVE, "connector_failed", name).endswith(f":{name}")


def test_rejudge_rules_added_appears_when_a_rule_is_appended_without_a_change() -> None:
    """Already INSUFFICIENT EVIDENCE from a connector issue: the window rule is appended, the
    most severe result does not move, so `rules_added` without `result_changed`."""
    after_start = sl.START + timedelta(minutes=1)
    record = _assess([_WithIssue()], sl.fake(), clock=lambda: after_start)
    assert record.deterministic_recommendation is R.INSUFFICIENT_EVIDENCE
    rejudge = next(t for t in record.stages if t.name is N.REJUDGE)
    assert "rejudge.rules_added" in rejudge.issues
    assert "rejudge.result_changed" not in rejudge.issues
    assert any(r.rule_id == "assessment.window_started" for r in record.rule_results)
    # No rule appended, no code: a clock before the start leaves the list as it was.
    before_start = sl.START - timedelta(hours=1)
    quiet = _assess([_WithIssue()], sl.fake(), clock=lambda: before_start)
    assert next(t for t in quiet.stages if t.name is N.REJUDGE).issues == ()


def test_supervise_downgrade_code_appears_iff_the_second_reconcile_set_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_name = {t.name: t for t in sl.run_pipeline(sl.fake(recommendation="GO")).stages}
    # First reconcile flagged it (GO vs INSUFFICIENT EVIDENCE): propose carries the code, not
    # supervise, even though the flag is set on the record.
    assert by_name[N.PROPOSE].issues == ("propose.llm_attempted_downgrade",)
    assert by_name[N.SUPERVISE].issues == ()
    # Second reconcile set it (CAUTION accepted first, then tightened): supervise carries it.
    _lift_upstream_id_guards(monkeypatch, "finding")
    record = sl.run_pipeline(
        sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False
    )
    by_name = {t.name: t for t in record.stages}
    assert record.llm_attempted_downgrade is True
    assert by_name[N.PROPOSE].issues == ()
    assert "supervise.llm_attempted_downgrade" in by_name[N.SUPERVISE].issues
    # Neither reconcile flagged it: no code anywhere.
    clean = sl.run_pipeline(sl.fake(recommendation="NO-GO"))
    assert clean.llm_attempted_downgrade is False
    assert not any("llm_attempted_downgrade" in c for t in clean.stages for c in t.issues)


def _tightened_record() -> dict[str, Any]:
    """A validator-clean record with one failed check (`citations_valid`), its supervisor rule
    result, the tightened code and the supervise downgrade code (hand-built, since a real
    tightened run carries the ghost id the record refuses first)."""
    record = _record_with_failed_check(SupervisorCheck.CITATIONS_VALID)
    document = record.model_dump(mode="json")
    supervise = next(e for e in document["stages"] if e["name"] == "supervise")
    assert supervise["issues"] == [
        "supervise.tightened:citations_valid",
        "supervise.llm_attempted_downgrade",
    ]
    assert AssessmentRecord.model_validate(document).llm_attempted_downgrade is True  # control
    return document


def _with_supervise_issues(document: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    stages = [
        {**e, "issues": issues} if e["name"] == "supervise" else e for e in document["stages"]
    ]
    return {**document, "stages": stages}


def test_record_refuses_a_tightened_code_whose_check_the_verdict_did_not_fail() -> None:
    fragment = "supervise.tightened codes must match the verdict's failed checks"
    base = _tightened_record()
    extra = _with_supervise_issues(
        base,
        [
            "supervise.tightened:citations_valid",
            "supervise.tightened:prompt_version_pinned",
            "supervise.llm_attempted_downgrade",
        ],
    )
    assert record_refuses(extra, fragment)
    wrong = _with_supervise_issues(
        base, ["supervise.tightened:prompt_version_pinned", "supervise.llm_attempted_downgrade"]
    )
    assert record_refuses(wrong, fragment)
    # A passing record with a tightened code and no failed check.
    passing = record_doc()
    assert record_refuses(
        _with_supervise_issues(passing, ["supervise.tightened:citations_valid"]), fragment
    )


def test_record_refuses_a_failed_check_without_its_tightened_code() -> None:
    base = _tightened_record()
    missing = _with_supervise_issues(base, ["supervise.llm_attempted_downgrade"])
    assert record_refuses(missing, "supervise.tightened codes must match the verdict's failed")


def test_record_refuses_a_downgrade_flag_without_a_code_and_the_reverse() -> None:
    fragment = "a downgrade code must appear exactly when a downgrade was flagged"
    # Flagged by the first reconcile; drop the propose code.
    flagged = sl.run_pipeline(sl.fake(recommendation="GO")).model_dump(mode="json")
    assert flagged["llm_attempted_downgrade"] is True
    no_code = {
        **flagged,
        "stages": [{**e, "issues": []} if e["name"] == "propose" else e for e in flagged["stages"]],
    }
    assert record_refuses(no_code, fragment)
    # Flagged by the second reconcile; drop the supervise code (tightened code kept).
    tightened = _tightened_record()
    assert record_refuses(
        _with_supervise_issues(tightened, ["supervise.tightened:citations_valid"]), fragment
    )
    # Not flagged; add a code on propose, then on supervise.
    clean = sl.run_pipeline(sl.fake(recommendation="NO-GO")).model_dump(mode="json")
    assert clean["llm_attempted_downgrade"] is False
    for stage in ("propose", "supervise"):
        forged = {
            **clean,
            "stages": [
                {**e, "issues": [f"{stage}.llm_attempted_downgrade"]} if e["name"] == stage else e
                for e in clean["stages"]
            ],
        }
        assert record_refuses(forged, fragment), stage


def test_record_refuses_an_observe_trace_whose_ids_are_not_the_kept_ids() -> None:
    fragment = "the observe trace must list the record's kept evidence ids"
    base = record_doc()
    kept = kept_ids(base)
    assert len(kept) > 1
    variants = {
        "empty": (),
        "subset": kept[:-1],
        "reordered": (kept[1], kept[0], *kept[2:]),
    }
    for label, ids in variants.items():
        doc = {
            **base,
            "stages": [
                {**e, "evidence_ids": list(ids)} if e["name"] == "observe" else e
                for e in base["stages"]
            ],
        }
        assert record_refuses(doc, fragment), label
    assert AssessmentRecord.model_validate(base).stages[0].evidence_ids == kept


def test_a_negative_duration_is_refused_and_the_pipeline_writes_a_non_negative_one() -> None:
    with pytest.raises(ValidationError):
        trace(duration_ms=-1)
    assert trace(duration_ms=0).duration_ms == 0
    assert trace().duration_ms == 0
    with pytest.raises(ValidationError):
        trace(duration_ms=1.5)
    record = sl.run_pipeline(sl.fake())
    assert all(isinstance(t.duration_ms, int) and t.duration_ms >= 0 for t in record.stages)


def test_the_runner_bound_is_the_domain_bound() -> None:
    from divesafe.agents import runner

    assert runner.MAX_EVIDENCE_ITEMS is MAX_EVIDENCE_ITEMS
    assert MAX_EVIDENCE_ITEMS == 120


def test_task_outcome_kinds_have_no_timed_out_label() -> None:
    """A task cut off by the stage deadline is absent from `task_outcomes`; the stage itself is
    `timed_out`. No per-task label claims a timing the code does not measure."""
    from typing import get_args

    from divesafe.agents.stage import TaskOutcomeKind

    assert set(get_args(TaskOutcomeKind)) == {"ok", "failed", "skipped"}
    with pytest.raises(ValueError, match="not an issue kind"):
        issue_code(N.REASON, "specialist_timed_out", "weather")
