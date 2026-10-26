"""The Supervise stage (ADR 0025 item 2): code-only checks, their failure conditions, and what
the pipeline does with a failed check.

Two classes of failure. `reconcile_tighten_only` and `severity_monotonic` are invariants: a
breach raises `SupervisorInvariantError` with a fixed message and nothing is stored. The other
three (`citations_valid`, `prompt_version_pinned`, `required_categories_present`) tighten: a
`supervisor.<check>` INSUFFICIENT EVIDENCE rule result is appended and `reconcile` runs again,
so `final_recommendation` still comes only from `reconcile` (AGENTS.md section 5).

Behaviour-neutral on current code: every check passes on every reachable path because the
upstream guard (the runner's id check, `reconcile`, the engine's policy) already holds. The
pipeline-level tests therefore remove the upstream guard (monkeypatch, never src) to prove the
Supervise stage catches what the guard would have. Offline: FakeProvider, fixed clock.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError, model_validator
from tests.agent import scripted_llm as sl

from divesafe.agents import PROMPT_VERSION
from divesafe.api.schemas import AssessmentView
from divesafe.domain import (
    INVARIANT_CHECKS,
    SUPERVISOR_RULE_PREFIX,
    AssessmentRecord,
    DataCategory,
    Recommendation,
    RuleResult,
    SupervisorCheck,
    SupervisorVerdict,
    severity,
    supervisor_rule_id,
)
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.orchestration.supervisor import SupervisorInvariantError, supervise
from divesafe.risk import EvidencePolicy, Reconciliation, RiskRulesEngine, reconcile
from divesafe.risk.engine import RESERVED_RULE_PREFIXES

pytestmark = pytest.mark.safety

R = Recommendation
C = SupervisorCheck
ALL_CHECKS = tuple(C)
TIGHTENING_CHECKS = tuple(c for c in C if c not in INVARIANT_CHECKS)
INVARIANT_MESSAGE = "supervisor invariant breached; the assessment was not recorded"
NOTICE = "agent output reflects the earlier result"
PINNED = f"fake/fake-model; prompts={PROMPT_VERSION}"
EVIDENCE = ("ev-1", "ev-2", "ev-3")


def consistent(deterministic: R = R.CAUTION, proposed: R | None = R.NO_GO) -> Reconciliation:
    return reconcile(deterministic, proposed)


def run_checks(**overrides: Any) -> Any:
    """`supervise` on a consistent input where every check passes; `overrides` break one."""
    arguments: dict[str, Any] = {
        "reconciled": consistent(),
        "proposal_evidence_ids": ("ev-1",),
        "finding_evidence_ids": ("ev-2",),
        "scenario_evidence_ids": ("ev-3", "ev-1"),
        "evidence_ids": EVIDENCE,
        "model_version": PINNED,
        "provider_ran": True,
        "missing_required_categories": (),
    }
    arguments.update(overrides)
    return supervise(**arguments)


# --- supervise(): the unit ----------------------------------------------------------------------


def test_all_five_checks_pass_on_a_consistent_input_and_no_rule_result_is_produced() -> None:
    supervision = run_checks()
    assert supervision.passed == ALL_CHECKS
    assert supervision.failed == ()
    assert supervision.rule_results == ()


@pytest.mark.parametrize("deterministic", list(R))
@pytest.mark.parametrize("proposed", [*R, None])
def test_every_reconcile_output_passes_the_invariant_checks(
    deterministic: R, proposed: R | None
) -> None:
    supervision = run_checks(reconciled=reconcile(deterministic, proposed))
    assert set(INVARIANT_CHECKS) <= set(supervision.passed)


@pytest.mark.parametrize(
    "argument",
    ["proposal_evidence_ids", "finding_evidence_ids", "scenario_evidence_ids"],
)
def test_citations_valid_fails_on_an_unknown_id_with_exactly_one_rule_result(
    argument: str,
) -> None:
    supervision = run_checks(**{argument: ("ev-1", "not-an-evidence-id")})
    assert supervision.failed == (C.CITATIONS_VALID,)
    assert C.CITATIONS_VALID not in supervision.passed
    assert len(supervision.rule_results) == 1
    (result,) = supervision.rule_results
    assert result.rule_id == "supervisor.citations_valid" == supervisor_rule_id(C.CITATIONS_VALID)
    assert result.outcome is R.INSUFFICIENT_EVIDENCE
    assert result.evidence_ids == ()  # never cites the bad id back


def test_citations_valid_fails_on_a_withheld_id_because_only_evaluated_ids_are_known() -> None:
    # A withheld item's id is not in `evidence_ids` (the evaluated items); citing it fails.
    supervision = run_checks(
        evidence_ids=("ev-1", "ev-2", "ev-3"), scenario_evidence_ids=("ev-withheld",)
    )
    assert supervision.failed == (C.CITATIONS_VALID,)
    assert [r.rule_id for r in supervision.rule_results] == ["supervisor.citations_valid"]


def test_citations_valid_passes_when_nothing_is_cited() -> None:
    supervision = run_checks(
        proposal_evidence_ids=(), finding_evidence_ids=(), scenario_evidence_ids=()
    )
    assert C.CITATIONS_VALID in supervision.passed


@pytest.mark.parametrize(
    "model_version",
    [None, "fake/fake-model", "fake/fake-model; prompts=", "fake/fake-model; prompts=0000-00-00.0"],
)
def test_prompt_version_pinned_fails_when_a_provider_ran_without_the_pin(
    model_version: str | None,
) -> None:
    supervision = run_checks(model_version=model_version, provider_ran=True)
    assert supervision.failed == (C.PROMPT_VERSION_PINNED,)
    assert [r.rule_id for r in supervision.rule_results] == ["supervisor.prompt_version_pinned"]
    assert supervision.rule_results[0].outcome is R.INSUFFICIENT_EVIDENCE


def test_prompt_version_pinned_is_not_applied_when_no_provider_ran() -> None:
    for model_version in (None, "fake/fake-model"):
        supervision = run_checks(model_version=model_version, provider_ran=False)
        assert supervision.failed == ()
        assert C.PROMPT_VERSION_PINNED in supervision.passed


def test_prompt_version_pinned_checks_the_current_prompt_version() -> None:
    assert PROMPT_VERSION in PINNED
    assert run_checks(model_version=PINNED, provider_ran=True).failed == ()


@pytest.mark.parametrize("deterministic", [R.GO, R.CAUTION])
def test_required_categories_present_fails_when_a_category_is_missing_below_insufficient(
    deterministic: R,
) -> None:
    supervision = run_checks(
        reconciled=reconcile(deterministic, None),
        missing_required_categories=(DataCategory.WAVES_SWELL,),
    )
    assert supervision.failed == (C.REQUIRED_CATEGORIES_PRESENT,)
    assert [r.rule_id for r in supervision.rule_results] == [
        "supervisor.required_categories_present"
    ]


@pytest.mark.parametrize("deterministic", [R.INSUFFICIENT_EVIDENCE, R.NO_GO])
def test_required_categories_present_passes_when_the_rules_already_fail_closed(
    deterministic: R,
) -> None:
    supervision = run_checks(
        reconciled=reconcile(deterministic, None),
        missing_required_categories=(DataCategory.WAVES_SWELL,),
    )
    assert supervision.failed == ()


@pytest.mark.parametrize("deterministic", list(R))
def test_required_categories_present_passes_when_nothing_is_missing(deterministic: R) -> None:
    supervision = run_checks(
        reconciled=reconcile(deterministic, None), missing_required_categories=()
    )
    assert C.REQUIRED_CATEGORIES_PRESENT in supervision.passed


def test_several_failed_checks_give_one_rule_result_each_in_check_order() -> None:
    supervision = run_checks(
        proposal_evidence_ids=("ghost",),
        model_version=None,
        reconciled=reconcile(R.GO, None),
        missing_required_categories=(DataCategory.CURRENTS,),
    )
    assert supervision.failed == TIGHTENING_CHECKS
    assert set(supervision.passed) == set(INVARIANT_CHECKS)
    assert [r.rule_id for r in supervision.rule_results] == [
        supervisor_rule_id(c) for c in TIGHTENING_CHECKS
    ]
    assert all(r.outcome is R.INSUFFICIENT_EVIDENCE for r in supervision.rule_results)


# --- invariants: raise, store nothing -----------------------------------------------------------


def inconsistent(
    final: R, deterministic: R, proposed: R | None, downgrade: bool = False
) -> Reconciliation:
    """A Reconciliation `reconcile` would never produce. Built by hand on purpose."""
    return Reconciliation(final, deterministic, proposed, downgrade)


@pytest.mark.parametrize(
    "reconciled",
    [
        inconsistent(R.GO, R.NO_GO, None),  # final below deterministic
        inconsistent(R.CAUTION, R.CAUTION, R.NO_GO),  # final below the proposal
        inconsistent(R.GO, R.GO, R.GO, downgrade=True),  # downgrade flag without a downgrade
        inconsistent(R.NO_GO, R.NO_GO, R.GO, downgrade=False),  # downgrade not flagged
        inconsistent(R.GO, R.CAUTION, R.GO, downgrade=True),  # flagged, but relaxed anyway
        inconsistent(R.CAUTION, R.INSUFFICIENT_EVIDENCE, R.CAUTION, downgrade=True),
    ],
)
def test_an_inconsistent_reconciliation_raises_the_fixed_invariant_error(
    reconciled: Reconciliation,
) -> None:
    with pytest.raises(SupervisorInvariantError) as caught:
        run_checks(reconciled=reconciled)
    assert str(caught.value) == INVARIANT_MESSAGE
    assert isinstance(caught.value, RuntimeError)


def test_the_invariant_error_carries_no_input_text() -> None:
    assert str(SupervisorInvariantError()) == INVARIANT_MESSAGE
    assert SupervisorInvariantError.__init__.__code__.co_argcount == 1  # no message argument


def test_an_invariant_breach_is_raised_even_when_a_tightening_check_would_also_fail() -> None:
    with pytest.raises(SupervisorInvariantError):
        run_checks(reconciled=inconsistent(R.GO, R.NO_GO, None), proposal_evidence_ids=("ghost",))


# --- property: Supervise never makes `final` less severe than Propose's -------------------------


@pytest.mark.parametrize("deterministic", list(R))
@pytest.mark.parametrize("proposed", [*R, None])
def test_re_reconciling_after_a_tightening_check_never_relaxes_the_first_final(
    deterministic: R, proposed: R | None
) -> None:
    first = reconcile(deterministic, proposed)
    # A failed tightening check appends an INSUFFICIENT EVIDENCE rule result, so the new
    # deterministic result is the most severe of the old one and INSUFFICIENT EVIDENCE.
    tightened_deterministic = max((deterministic, R.INSUFFICIENT_EVIDENCE), key=severity)
    second = reconcile(tightened_deterministic, proposed)
    assert severity(second.final) >= severity(first.final)
    assert severity(second.final) >= severity(R.INSUFFICIENT_EVIDENCE)
    assert second.proposed is first.proposed  # the proposal is unchanged
    # The second reconciliation satisfies the invariants too (Supervise could run on it).
    assert set(INVARIANT_CHECKS) <= set(run_checks(reconciled=second).passed)


def test_every_pair_of_the_four_recommendations_keeps_the_severity_order() -> None:
    order = (R.GO, R.CAUTION, R.INSUFFICIENT_EVIDENCE, R.NO_GO)
    for lower, higher in itertools.combinations(order, 2):
        assert severity(lower) < severity(higher)


# --- the verdict and the record refuse inconsistent shapes --------------------------------------


def test_supervisor_verdict_refuses_duplicate_checks() -> None:
    with pytest.raises(ValidationError, match="at most once"):
        SupervisorVerdict(passed=(C.CITATIONS_VALID, C.CITATIONS_VALID))
    with pytest.raises(ValidationError, match="at most once"):
        SupervisorVerdict(passed=(C.CITATIONS_VALID,), failed=(C.CITATIONS_VALID,))


@pytest.mark.parametrize("check", sorted(INVARIANT_CHECKS))
def test_supervisor_verdict_refuses_a_failed_invariant_check(check: C) -> None:
    with pytest.raises(ValidationError, match="invariant check cannot be recorded as failed"):
        SupervisorVerdict(passed=(), failed=(check,))


def test_supervisor_verdict_refuses_tightened_to_without_a_failed_check() -> None:
    with pytest.raises(ValidationError, match="tightened_to requires a failed check"):
        SupervisorVerdict(passed=ALL_CHECKS, tightened_to=R.INSUFFICIENT_EVIDENCE)


def test_supervisor_verdict_accepts_the_consistent_shapes() -> None:
    assert SupervisorVerdict(passed=ALL_CHECKS).failed == ()
    verdict = SupervisorVerdict(
        passed=tuple(c for c in ALL_CHECKS if c is not C.CITATIONS_VALID),
        failed=(C.CITATIONS_VALID,),
        tightened_to=R.INSUFFICIENT_EVIDENCE,
    )
    assert verdict.tightened_to is R.INSUFFICIENT_EVIDENCE


def test_supervisor_verdict_is_a_top_level_defaulted_record_field() -> None:
    """ADR 0012's replace rule: a new field at the top level, defaulting to None, so records
    stored before the stage existed still load and are replaced whole."""
    field = AssessmentRecord.model_fields["supervisor_verdict"]
    assert field.default is None
    assert not field.is_required()
    record = sl.run_pipeline(sl.fake())
    old = record.model_dump(mode="json")
    del old["supervisor_verdict"]
    loaded = AssessmentRecord.model_validate(old)
    assert loaded.supervisor_verdict is None
    assert loaded == record.model_copy(update={"supervisor_verdict": None})


def supervisor_result(check: C) -> dict[str, Any]:
    return RuleResult(
        rule_id=supervisor_rule_id(check), outcome=R.INSUFFICIENT_EVIDENCE, rationale="x"
    ).model_dump(mode="json")


def passing_record() -> dict[str, Any]:
    record = sl.run_pipeline(sl.fake())
    assert record.supervisor_verdict is not None
    assert record.supervisor_verdict.failed == ()
    return record.model_dump(mode="json")


def refuses(document: dict[str, Any], fragment: str) -> bool:
    try:
        AssessmentRecord.model_validate(document)
    except ValidationError as exc:
        return fragment in str(exc)
    return False


def test_record_refuses_a_failed_check_without_its_supervisor_rule_result() -> None:
    base = passing_record()
    verdict = {
        "passed": [c.value for c in ALL_CHECKS if c is not C.CITATIONS_VALID],
        "failed": [C.CITATIONS_VALID.value],
        "tightened_to": None,
    }
    assert refuses(
        {**base, "supervisor_verdict": verdict},
        "supervisor rule results must match the verdict's failed checks",
    )


def test_record_refuses_an_orphan_supervisor_rule_result() -> None:
    base = passing_record()
    with_orphan = {
        **base,
        "rule_results": [*base["rule_results"], supervisor_result(C.CITATIONS_VALID)],
    }
    assert refuses(with_orphan, "supervisor rule results must match the verdict's failed checks")
    without_verdict = {**with_orphan, "supervisor_verdict": None}
    assert refuses(
        without_verdict, "supervisor rule results must match the verdict's failed checks"
    )


def test_record_refuses_a_duplicated_supervisor_rule_result() -> None:
    base = passing_record()
    verdict = {
        "passed": [c.value for c in ALL_CHECKS if c is not C.CITATIONS_VALID],
        "failed": [C.CITATIONS_VALID.value],
        "tightened_to": None,
    }
    twice = {
        **base,
        "supervisor_verdict": verdict,
        "rule_results": [
            *base["rule_results"],
            supervisor_result(C.CITATIONS_VALID),
            supervisor_result(C.CITATIONS_VALID),
        ],
    }
    assert refuses(twice, "each supervisor rule result must appear once")


def test_record_refuses_a_supervisor_rule_result_that_is_not_insufficient_evidence() -> None:
    base = passing_record()
    verdict = {
        "passed": [c.value for c in ALL_CHECKS if c is not C.CITATIONS_VALID],
        "failed": [C.CITATIONS_VALID.value],
        "tightened_to": None,
    }
    relaxed = {**supervisor_result(C.CITATIONS_VALID), "outcome": "GO"}
    assert refuses(
        {**base, "supervisor_verdict": verdict, "rule_results": [*base["rule_results"], relaxed]},
        "a supervisor rule result must be INSUFFICIENT EVIDENCE",
    )


def test_record_refuses_tightened_to_that_is_not_the_final_recommendation() -> None:
    base = passing_record()
    assert base["final_recommendation"] == "INSUFFICIENT EVIDENCE"
    verdict = {
        "passed": [c.value for c in ALL_CHECKS if c is not C.CITATIONS_VALID],
        "failed": [C.CITATIONS_VALID.value],
        "tightened_to": "NO-GO",
    }
    document = {
        **base,
        "supervisor_verdict": verdict,
        "rule_results": [*base["rule_results"], supervisor_result(C.CITATIONS_VALID)],
    }
    assert refuses(document, "tightened_to must be the final recommendation")
    # Control: the same document with the matching `tightened_to` is accepted.
    verdict["tightened_to"] = "INSUFFICIENT EVIDENCE"
    assert AssessmentRecord.model_validate(document).supervisor_verdict is not None


# --- the pipeline on current code ---------------------------------------------------------------


def test_the_scripted_run_passes_all_five_checks_with_no_tightening() -> None:
    record = sl.run_pipeline(sl.fake())
    verdict = record.supervisor_verdict
    assert verdict is not None
    assert verdict.passed == ALL_CHECKS
    assert verdict.failed == ()
    assert verdict.tightened_to is None
    assert not any(r.rule_id.startswith("supervisor.") for r in record.rule_results)
    assert record.model_version == PINNED


def test_a_run_without_a_provider_passes_all_five_checks() -> None:
    record = asyncio.run(
        pipeline_module.assess_dive(
            assessment_id="s-1",
            plan=sl.PLAN,
            site=sl.TIOMAN_ISLAND,
            connectors=[],
            engine=sl.engine(with_warning_rule=False),
            now=sl.NOW,
        )
    )
    assert record.model_version is None
    assert record.supervisor_verdict is not None
    assert record.supervisor_verdict.passed == ALL_CHECKS


# --- teeth: remove the upstream guard and watch Supervise tighten -------------------------------


class _RecordWithoutTheIdCheck(AssessmentRecord):
    """`AssessmentRecord` with its own unknown-cited-id refusal removed and only the supervisor
    shape check kept. An unknown cited id is refused twice upstream of Supervise (the runner and
    the record validator); both are lifted here so the verdict's tightening can be observed on a
    record. Test-only; the real record keeps both guards."""

    @model_validator(mode="after")
    def _check_consistency(self) -> AssessmentRecord:
        self._check_supervisor()
        return self


def _lift_upstream_id_guards(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """After the real Reason stage, add a ghost id to the proposal, a finding or a scenario
    (`where`), as if the runner's id check had let it through; and record with the id check
    lifted so the only thing left to refuse it is Supervise."""
    original = pipeline_module.STAGE_WORK[pipeline_module.StageName.REASON]

    async def reason_with_a_ghost(run: Any) -> Any:
        outcome = await original(run)
        assert run.proposed is not None and run.findings and run.scenarios  # controls
        if where == "proposal":
            run.proposal_ids = (*run.proposal_ids, "ghost-evidence-id")
        elif where == "finding":
            first = run.findings[0]
            run.findings = (
                first.model_copy(update={"evidence_ids": (*first.evidence_ids, "ghost")}),
                *run.findings[1:],
            )
        else:
            first = run.scenarios[0]
            run.scenarios = (
                first.model_copy(update={"evidence_ids": (*first.evidence_ids, "ghost")}),
                *run.scenarios[1:],
            )
        return outcome

    monkeypatch.setitem(
        pipeline_module.STAGE_WORK, pipeline_module.StageName.REASON, reason_with_a_ghost
    )
    monkeypatch.setattr(pipeline_module, "AssessmentRecord", _RecordWithoutTheIdCheck)


def test_the_real_record_refuses_an_unknown_cited_id_before_supervise_matters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the lifted guard: with the real record, the ghost id is refused outright."""
    _lift_upstream_id_guards(monkeypatch, "proposal")
    monkeypatch.setattr(pipeline_module, "AssessmentRecord", AssessmentRecord)
    with pytest.raises(ValidationError, match="cited evidence ids not present"):
        sl.run_pipeline(sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False)


@pytest.mark.parametrize("where", ["proposal", "finding", "scenario"])
def test_an_unknown_cited_id_is_caught_by_supervise_and_tightened(
    where: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lift_upstream_id_guards(monkeypatch, where)
    record = sl.run_pipeline(
        sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False
    )
    # Would have been GO/CAUTION/CAUTION on current code (see the control below).
    verdict = record.supervisor_verdict
    assert verdict is not None
    assert verdict.failed == (C.CITATIONS_VALID,)
    assert C.CITATIONS_VALID not in verdict.passed
    assert [r.rule_id for r in record.rule_results if r.rule_id.startswith("supervisor.")] == [
        "supervisor.citations_valid"
    ]
    assert severity(record.deterministic_recommendation) >= severity(R.INSUFFICIENT_EVIDENCE)
    assert severity(record.final_recommendation) >= severity(R.INSUFFICIENT_EVIDENCE)
    assert verdict.tightened_to is record.final_recommendation
    assert any(NOTICE in issue for issue in record.agent_issues)
    assert record.proposed_recommendation is R.CAUTION  # the proposal itself is unchanged
    assert record.llm_attempted_downgrade is True  # CAUTION is now below the rules


def test_control_the_same_run_without_the_ghost_is_caution_with_every_check_passed() -> None:
    record = sl.run_pipeline(
        sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False
    )
    assert record.deterministic_recommendation is R.GO
    assert record.final_recommendation is R.CAUTION
    assert record.supervisor_verdict is not None
    assert record.supervisor_verdict.failed == ()
    assert not any(NOTICE in issue for issue in record.agent_issues)


def test_a_model_version_without_the_prompt_pin_is_caught_by_supervise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pipeline_module.STAGE_WORK[pipeline_module.StageName.REASON]

    async def unpinned(run: Any) -> Any:
        outcome = await original(run)
        run.model_version = "fake/fake-model"
        return outcome

    monkeypatch.setitem(pipeline_module.STAGE_WORK, pipeline_module.StageName.REASON, unpinned)
    record = sl.run_pipeline(
        sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False
    )
    verdict = record.supervisor_verdict
    assert verdict is not None
    assert verdict.failed == (C.PROMPT_VERSION_PINNED,)
    assert verdict.tightened_to is R.INSUFFICIENT_EVIDENCE is record.final_recommendation
    assert any(r.rule_id == "supervisor.prompt_version_pinned" for r in record.rule_results)
    assert any(NOTICE in issue for issue in record.agent_issues)


def test_a_missing_required_category_the_engine_let_through_is_caught_by_supervise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove the engine's policy judgement (no connectors, so no evidence at all) by replacing
    the Assess work with one that reports GO; Supervise must fail the category check."""

    async def assess_go_on_nothing(run: Any) -> Any:
        run.rule_results = [RuleResult(rule_id="synthetic.go", outcome=R.GO, rationale="x")]
        run.deterministic = R.GO
        run.ruleset_version = "synthetic"
        return pipeline_module.StageOutcome.COMPLETED

    monkeypatch.setitem(
        pipeline_module.STAGE_WORK, pipeline_module.StageName.ASSESS, assess_go_on_nothing
    )
    record = asyncio.run(
        pipeline_module.assess_dive(
            assessment_id="s-2",
            plan=sl.PLAN,
            site=sl.TIOMAN_ISLAND,
            connectors=[],
            engine=sl.engine(with_warning_rule=False),
            now=sl.NOW,
        )
    )
    verdict = record.supervisor_verdict
    assert verdict is not None
    assert verdict.failed == (C.REQUIRED_CATEGORIES_PRESENT,)
    assert record.final_recommendation is R.INSUFFICIENT_EVIDENCE
    assert verdict.tightened_to is R.INSUFFICIENT_EVIDENCE
    assert any(r.rule_id == "supervisor.required_categories_present" for r in record.rule_results)


def test_a_reconcile_that_relaxes_the_result_raises_and_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `reconcile` ever produced a final less severe than the rules, Supervise raises the
    invariant error and no record is built or returned."""
    gate_ran: list[bool] = []
    original_gate = pipeline_module.STAGE_WORK[pipeline_module.StageName.GATE]

    def relaxing_reconcile(deterministic: R, proposed: R | None) -> Reconciliation:
        real = reconcile(deterministic, proposed)
        return Reconciliation(R.GO, real.deterministic, real.proposed, real.llm_attempted_downgrade)

    async def gate(run: Any) -> Any:
        gate_ran.append(True)
        return await original_gate(run)

    monkeypatch.setattr(pipeline_module, "reconcile", relaxing_reconcile)
    monkeypatch.setitem(pipeline_module.STAGE_WORK, pipeline_module.StageName.GATE, gate)
    with pytest.raises(SupervisorInvariantError) as caught:
        sl.run_pipeline(sl.fake(recommendation="NO-GO"))  # rules: INSUFFICIENT EVIDENCE
    assert str(caught.value) == INVARIANT_MESSAGE
    assert gate_ran == []  # nothing was recorded


def test_a_reconcile_that_mislabels_the_downgrade_flag_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unflagged_reconcile(deterministic: R, proposed: R | None) -> Reconciliation:
        real = reconcile(deterministic, proposed)
        return Reconciliation(real.final, real.deterministic, real.proposed, False)

    monkeypatch.setattr(pipeline_module, "reconcile", unflagged_reconcile)
    with pytest.raises(SupervisorInvariantError):
        # Proposal GO below the rules' INSUFFICIENT EVIDENCE: a downgrade that is not flagged.
        sl.run_pipeline(sl.fake(recommendation="GO"))


def test_supervise_is_code_only_and_never_calls_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every provider request is made before Supervise starts; the stage holds no provider."""
    provider = sl.fake()
    seen_at_supervise: list[int] = []
    original = pipeline_module.STAGE_WORK[pipeline_module.StageName.SUPERVISE]

    async def counting(run: Any) -> Any:
        before = len(provider.requests)
        outcome = await original(run)
        seen_at_supervise.append(len(provider.requests) - before)
        return outcome

    monkeypatch.setitem(pipeline_module.STAGE_WORK, pipeline_module.StageName.SUPERVISE, counting)
    sl.run_pipeline(provider)
    assert provider.requests  # control: the Reason stage did call it
    assert seen_at_supervise == [0]
    assert "provider" not in supervise.__code__.co_varnames


# --- reviewer fixes: reserved prefix, notices, the API note --------------------------------------

SUPERVISOR_NOTICE = (
    "the supervisor tightened the result after the agent stage; agent output reflects the "
    "earlier result"
)
REJUDGED_NOTICE = (
    "the rules were re-judged after the agent stage; agent output reflects the earlier result"
)


class _ConfiguredRule:
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id

    def evaluate(self, plan: Any, evidence: Any) -> RuleResult:
        return RuleResult(rule_id=self.rule_id, outcome=R.GO, rationale="synthetic")


@pytest.mark.parametrize("rule_id", ["supervisor.x", "SUPERVISOR.citations_valid", "supervisor."])
def test_a_configured_rule_may_not_use_the_supervisor_prefix(rule_id: str) -> None:
    """Only the Supervise stage emits `supervisor.<check>`; a configured rule that forged one
    would make the record's verdict check see a failed check that never happened."""
    with pytest.raises(ValueError, match="reserved prefix"):
        RiskRulesEngine(
            [_ConfiguredRule(rule_id)],
            EvidencePolicy(sl.LIVE, timedelta(hours=1)),
            "synthetic",
            required_factors=frozenset(),
        )


def test_the_supervisor_prefix_is_in_the_reserved_list_exactly_as_the_domain_spells_it() -> None:
    assert SUPERVISOR_RULE_PREFIX in RESERVED_RULE_PREFIXES
    assert supervisor_rule_id(C.CITATIONS_VALID).startswith(RESERVED_RULE_PREFIXES)


def test_a_control_rule_id_without_a_reserved_prefix_is_accepted() -> None:
    engine = RiskRulesEngine(
        [_ConfiguredRule("synthetic.supervisor_lookalike")],
        EvidencePolicy(sl.LIVE, timedelta(hours=1)),
        "synthetic",
        required_factors=frozenset(),
    )
    assert engine.policy.required_categories == sl.LIVE


def test_the_pipeline_notices_are_the_fixed_strings() -> None:
    assert pipeline_module._SUPERVISOR_NOTICE == SUPERVISOR_NOTICE
    assert pipeline_module._REJUDGED_NOTICE == REJUDGED_NOTICE
    assert SUPERVISOR_NOTICE != REJUDGED_NOTICE
    assert NOTICE in SUPERVISOR_NOTICE and NOTICE in REJUDGED_NOTICE


class _GoThenCaution:
    """Synthetic: GO on the first judgement, CAUTION on every later one, so Rejudge (the engine
    again at a later clock) changes the deterministic result from GO to CAUTION."""

    rule_id = "synthetic.go_then_caution"
    citation = "test fixture (synthetic, not a real safety source)"

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, plan: Any, evidence: Any) -> RuleResult:
        self.calls += 1
        outcome = R.GO if self.calls == 1 else R.CAUTION
        return RuleResult(
            rule_id=self.rule_id,
            outcome=outcome,
            rationale="synthetic",
            evidence_ids=tuple(e.id for e in evidence),
        )


def _run_with_a_later_clock(provider: Any) -> AssessmentRecord:
    """Like `sl.run_pipeline(..., warnings=[], with_warning_rule=False)` but Rejudge runs one
    minute on (still before the dive start, evidence still fresh) with `_GoThenCaution`."""
    engine = RiskRulesEngine(
        [_GoThenCaution()],
        EvidencePolicy(sl.LIVE, timedelta(hours=1), degraded_may_support_go=True),
        "synthetic",
        required_factors=frozenset(),
    )
    return asyncio.run(
        pipeline_module.assess_dive(
            assessment_id="agent-1",
            plan=sl.PLAN,
            site=sl.TIOMAN_ISLAND,
            connectors=[
                sl.OpenMeteoMarineConnector(sl._Getter(sl.MARINE)),
                sl.DataGovMyWarningConnector(sl._Getter([])),
            ],
            engine=engine,
            now=sl.NOW,
            provider=provider,
            clock=lambda: sl.NOW + timedelta(minutes=1),
        )
    )


def test_a_supervise_tightening_adds_its_own_notice_exactly_once_and_not_the_rejudge_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lift_upstream_id_guards(monkeypatch, "proposal")
    record = sl.run_pipeline(
        sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False
    )
    assert record.supervisor_verdict is not None
    assert record.supervisor_verdict.tightened_to is R.INSUFFICIENT_EVIDENCE
    assert record.agent_issues.count(SUPERVISOR_NOTICE) == 1
    assert REJUDGED_NOTICE not in record.agent_issues  # the clock did not move


def test_a_rejudge_change_and_a_supervise_tightening_each_add_their_notice_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejudge changes the result (a later clock makes the evidence stale) and Supervise then
    tightens further on a failed check: both notices, each once, Rejudge's first."""
    _lift_upstream_id_guards(monkeypatch, "proposal")
    record = _run_with_a_later_clock(sl.fake(recommendation="CAUTION"))
    assert record.agent_issues.count(REJUDGED_NOTICE) == 1
    assert record.agent_issues.count(SUPERVISOR_NOTICE) == 1
    assert record.agent_issues.index(REJUDGED_NOTICE) < record.agent_issues.index(SUPERVISOR_NOTICE)
    assert record.supervisor_verdict is not None
    assert record.supervisor_verdict.failed == (C.CITATIONS_VALID,)
    assert record.supervisor_verdict.tightened_to is R.INSUFFICIENT_EVIDENCE


def test_a_rejudge_change_alone_adds_only_the_rejudge_notice() -> None:
    record = _run_with_a_later_clock(sl.fake(recommendation="CAUTION"))
    assert record.deterministic_recommendation is R.CAUTION  # control: Rejudge changed GO
    assert record.agent_issues.count(REJUDGED_NOTICE) == 1
    assert SUPERVISOR_NOTICE not in record.agent_issues
    assert record.supervisor_verdict is not None
    assert record.supervisor_verdict.failed == ()


def _record_with_failed_check(check: C) -> AssessmentRecord:
    """A record built by hand: the matching `supervisor.<check>` rule result, the verdict, and
    `tightened_to` equal to the final. Passes the real record validator."""
    base = sl.run_pipeline(sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False)
    assert base.final_recommendation is R.CAUTION  # control: GO rules, CAUTION proposal
    verdict = SupervisorVerdict(
        passed=tuple(c for c in ALL_CHECKS if c is not check),
        failed=(check,),
        tightened_to=R.INSUFFICIENT_EVIDENCE,
    )
    return base.model_copy(
        update={
            "rule_results": (
                *base.rule_results,
                RuleResult(
                    rule_id=supervisor_rule_id(check),
                    outcome=R.INSUFFICIENT_EVIDENCE,
                    rationale="synthetic",
                ),
            ),
            "deterministic_recommendation": R.INSUFFICIENT_EVIDENCE,
            "final_recommendation": R.INSUFFICIENT_EVIDENCE,
            "llm_attempted_downgrade": True,
            "supervisor_verdict": verdict,
        }
    )


@pytest.mark.parametrize("check", TIGHTENING_CHECKS)
def test_the_api_outcome_note_names_the_failed_supervisor_check(check: C) -> None:
    record = AssessmentRecord.model_validate(_record_with_failed_check(check).model_dump())
    note = AssessmentView.of(record).outcome_note
    assert f" A supervisor check failed: {check.value}. The result was tightened." in note
    assert note.count("A supervisor check failed") == 1
    # Fixed wording from the enum value only; the rationale text is not echoed.
    assert "synthetic" not in note


def test_the_api_outcome_note_has_no_supervisor_sentence_when_every_check_passed() -> None:
    record = sl.run_pipeline(
        sl.fake(recommendation="CAUTION"), warnings=[], with_warning_rule=False
    )
    assert "A supervisor check failed" not in AssessmentView.of(record).outcome_note


def test_the_api_outcome_note_lists_several_failed_checks_in_sorted_order() -> None:
    record = _record_with_failed_check(C.PROMPT_VERSION_PINNED)
    verdict = SupervisorVerdict(
        passed=tuple(INVARIANT_CHECKS & set(ALL_CHECKS)),
        failed=TIGHTENING_CHECKS,
        tightened_to=R.INSUFFICIENT_EVIDENCE,
    )
    more = record.model_copy(
        update={
            "rule_results": (
                *record.rule_results,
                *(
                    RuleResult(
                        rule_id=supervisor_rule_id(c),
                        outcome=R.INSUFFICIENT_EVIDENCE,
                        rationale="synthetic",
                    )
                    for c in TIGHTENING_CHECKS
                    if c is not C.PROMPT_VERSION_PINNED
                ),
            ),
            "supervisor_verdict": verdict,
        }
    )
    note = AssessmentView.of(AssessmentRecord.model_validate(more.model_dump())).outcome_note
    positions = [
        note.index(f"A supervisor check failed: {c.value}.") for c in sorted(TIGHTENING_CHECKS)
    ]
    assert positions == sorted(positions)
    assert note.count("A supervisor check failed") == len(TIGHTENING_CHECKS)
