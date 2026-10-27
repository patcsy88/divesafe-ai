"""Adversarial false-GO gate (ADR 0025 item 4). Synthetic, labelled cases; offline.

False-GO must be 0 for every variant on every case. The mutation check relaxes `reconcile` in the
pipeline path in memory (no file under src/ is edited) and expects this file to fail.

Switch guard: an `ast` walk over `src/divesafe` (os.walk). It flags any call to `run_agent_stage`
or `_stage` (a Name, an Attribute, or an alias bound to either) that passes a keyword named
`specialists`, `scenarios`, `proposal`, any `with_*`, or `**` (keyword.arg None). The defining
module `agents/stage.py` is exempt only for calls inside its own `run_agent_stage` body, where the
switches are forwarded. Names `run_agent_stage` and `_stage` may be referenced only in
`agents/stage.py`, `agents/__init__.py` and `orchestration/pipeline.py`. Out of scope: `getattr`
and other dynamic lookups, which a static walk cannot see. Parameter and keyword checks cover
`assess_dive`, `api/` and `config/`; `Settings` is checked at runtime.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest
from tests.evaluation.ablation import (
    CALL_CAP,
    RAN,
    REFUSAL_REASON,
    REFUSED,
    VARIANTS,
    CaseRun,
    is_false_go,
    outcome_counts,
    run_all,
    run_variant,
)
from tests.evaluation.adversarial import CASES, Case, provider_for, run_case

from divesafe.agents.specialists import SPECIALISTS
from divesafe.config.settings import Settings
from divesafe.domain import Recommendation, ScenarioKind
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.orchestration.supervisor import SupervisorInvariantError
from divesafe.risk import Reconciliation
from divesafe.risk.engine import reconcile as real_reconcile

pytestmark = pytest.mark.safety

R = Recommendation
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "divesafe"
SWITCHES = frozenset({"specialists", "scenarios", "proposal"})
TARGETS = frozenset({"run_agent_stage", "_stage"})
OWNERS = frozenset({"agents/stage.py", "agents/__init__.py", "orchestration/pipeline.py"})
DEFINER = "agents/stage.py"
FunctionNode = (ast.FunctionDef, ast.AsyncFunctionDef)


def _is_switch(name: str) -> bool:
    return name in SWITCHES or name.startswith("with_")


def _case(name: str) -> Case:
    return next(c for c in CASES if c.name == name)


@pytest.fixture(scope="module")
def runs() -> dict[str, list[list[CaseRun]]]:
    return run_all(repeats=3)


# --- the AST walker ---------------------------------------------------------------------------


def _aliases(tree: ast.Module) -> set[str]:
    """The target names plus any name bound to them by import or plain assignment (fixed point)."""
    names = set(TARGETS)
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            new: set[str] = set()
            if isinstance(node, ast.ImportFrom):
                new = {a.asname or a.name for a in node.names if a.name in names}
            elif isinstance(node, ast.Assign):
                v = node.value
                ident = v.id if isinstance(v, ast.Name) else getattr(v, "attr", None)
                if ident in names:
                    new = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if not new <= names:
                names |= new
                grew = True
    return names


def _call_target(call: ast.Call, names: set[str]) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id in names
    if isinstance(f, ast.Attribute):
        return f.attr in names
    return False


def _stage_call_violations(label: str, tree: ast.Module) -> list[str]:
    names = _aliases(tree)
    exempt: set[int] = set()
    if label == DEFINER:
        for node in tree.body:
            if isinstance(node, FunctionNode) and node.name == "run_agent_stage":
                exempt.update(id(n) for n in ast.walk(node))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in exempt and _call_target(node, names):
            for kw in node.keywords:
                if kw.arg is None or _is_switch(kw.arg):
                    out.append(f"{label}:{node.lineno} passes {kw.arg or '**'}")
    return out


def _reference_violations(label: str, tree: ast.Module) -> list[str]:
    if label in OWNERS:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        ident: str | None = None
        if isinstance(node, ast.Name):
            ident = node.id
        elif isinstance(node, ast.Attribute):
            ident = node.attr
        elif isinstance(node, ast.alias):
            ident = node.name
        if ident in TARGETS:
            out.append(f"{label}:{getattr(node, 'lineno', 0)} references {ident}")
    return out


def _mention_violations(label: str, tree: ast.Module) -> list[str]:
    """A switch as a keyword argument or a function parameter (not a bare attribute read)."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg and _is_switch(node.arg):
            out.append(f"{label}:{node.lineno} keyword {node.arg}")
        if isinstance(node, ast.arg) and _is_switch(node.arg):
            out.append(f"{label}:{node.lineno} parameter {node.arg}")
    return out


def _src_trees() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for dirpath, _dirs, files in os.walk(SRC):
        for name in files:
            if name.endswith(".py"):
                path = Path(dirpath, name)
                trees[path.relative_to(SRC).as_posix()] = ast.parse(path.read_text())
    return trees


def _scan(label: str, text: str) -> list[str]:
    tree = ast.parse(text)
    return _stage_call_violations(label, tree) + _reference_violations(label, tree)


def test_walker_finds_src_modules_and_the_owner_files() -> None:
    trees = _src_trees()
    assert "agents/stage.py" in trees
    assert "orchestration/pipeline.py" in trees
    assert len(trees) > 20


def test_no_src_call_to_run_agent_stage_or_stage_passes_a_switch() -> None:
    offenders = [v for label, t in _src_trees().items() for v in _stage_call_violations(label, t)]
    assert offenders == []


def test_switch_names_are_referenced_only_in_owner_modules() -> None:
    offenders = [v for label, t in _src_trees().items() for v in _reference_violations(label, t)]
    assert offenders == []


def test_api_and_config_never_take_or_pass_a_switch() -> None:
    offenders: list[str] = []
    for label, tree in _src_trees().items():
        if label.startswith(("api/", "config/")):
            offenders.extend(_mention_violations(label, tree))
    assert offenders == []


def test_assess_dive_has_no_switch_parameter() -> None:
    """`scenarios` is caller-supplied `Scenario` data on `assess_dive`, not a switch, so it is
    allowed. A switch is a `specialists` / `proposal` / `with_*` name or any bool-default flag."""
    params = inspect.signature(pipeline_module.assess_dive).parameters
    assert not {p for p in params if p in {"specialists", "proposal"} or p.startswith("with_")}
    assert not [p for p, v in params.items() if isinstance(v.default, bool)]
    assert params["scenarios"].default == ()  # caller data, not a bool switch


def test_settings_has_no_switch_field() -> None:
    assert not {f for f in Settings.model_fields if _is_switch(f)}


def test_run_agent_stage_switches_exist_and_default_on() -> None:
    from divesafe.agents import run_agent_stage

    sig = inspect.signature(run_agent_stage).parameters
    for name in SWITCHES:
        assert sig[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig[name].default is True


def test_call_cap_matches_the_source_counts() -> None:
    # There is no pipeline or agents constant to import, so the literal is checked against
    # the counts in source: specialists, scenario kinds, one proposal (ADR 0007).
    assert len(SPECIALISTS) + len(ScenarioKind) + 1 == CALL_CAP


# Failure tests: the walker must flag these snippets.


@pytest.mark.parametrize(
    "snippet",
    [
        "run_agent_stage(p, plan, ev, rr, d, specialists=False)",
        "run_agent_stage(p, plan, ev, rr, d, scenarios=False)",
        "run_agent_stage(p, plan, ev, rr, d, proposal=False)",
        "_stage(p, plan, ev, rr, d, i, f, s, t, with_proposal=False)",
        "_stage(p, plan, ev, rr, d, i, f, s, t, with_specialists=False)",
        "run_agent_stage(p, plan, ev, rr, d, **flags)",
        "f = run_agent_stage\nf(p, plan, ev, rr, d, scenarios=False)",
        "import divesafe.agents.stage as m\nm.run_agent_stage(p, plan, ev, rr, d, proposal=0)",
        "from divesafe.agents.stage import run_agent_stage as go\n"
        "go(p, plan, ev, rr, d, specialists=0)",
    ],
)
def test_walker_flags_a_switch_passed_to_a_stage_call(snippet: str) -> None:
    assert _stage_call_violations("orchestration/pipeline.py", ast.parse(snippet))


@pytest.mark.parametrize(
    "snippet",
    [
        "run_agent_stage(p, plan, ev, rr, d)",
        "run_agent_stage(p, plan, ev, rr, d, timeout=1)",
        "assess(p, scenarios=scenario_data)",  # not a stage call: out of this rule's scope
    ],
)
def test_walker_does_not_flag_calls_without_a_switch(snippet: str) -> None:
    assert _stage_call_violations("orchestration/pipeline.py", ast.parse(snippet)) == []


def test_walker_flags_a_stage_name_referenced_outside_its_owners() -> None:
    assert _scan("orchestration/supervisor.py", "from divesafe.agents import run_agent_stage")
    assert _scan("api/routes.py", "x = run_agent_stage")
    assert _scan("orchestration/pipeline.py", "x = run_agent_stage") == []


def test_walker_flags_a_switch_parameter_in_api_or_config() -> None:
    assert _mention_violations("api/x.py", ast.parse("def f(*, proposal=True): pass"))
    assert _mention_violations("config/x.py", ast.parse("f(with_scenarios=False)"))
    assert _mention_violations("api/x.py", ast.parse("record.scenarios")) == []


def test_walker_exempts_only_the_forwarding_call_inside_run_agent_stage() -> None:
    forwarding = (
        "async def run_agent_stage(p, *, specialists=True):\n"
        "    await _stage(p, with_specialists=specialists)\n"
    )
    assert _stage_call_violations(DEFINER, ast.parse(forwarding)) == []
    elsewhere = (
        "async def run_agent_stage(p, *, specialists=True):\n"
        "    pass\n"
        "def other(p):\n"
        "    _stage(p, with_specialists=False)\n"
    )
    assert _stage_call_violations(DEFINER, ast.parse(elsewhere))


# --- the gate ---------------------------------------------------------------------------------


def _assert_not_go(result: CaseRun) -> None:
    """Per-run gate. A refused run is never stored: it has no final, no proposal and no record,
    and it is never counted as a false GO. A run that ran must not be GO and must give the
    labelled outcome."""
    where = (result.variant, result.case)
    if result.outcome == REFUSED:
        assert result.stored is False, where
        assert result.final is None, where  # a refused run must never carry a final
        assert result.proposed is None, where
        assert result.refusal_reason == REFUSAL_REASON, where
        assert result.false_go is False, where  # refusals are counted apart from false GO
        return
    assert result.outcome == RAN, where
    assert result.stored is True, where
    assert result.final is not R.GO, (*where, result.final)
    assert result.false_go is False, where
    assert result.abstention_correct, (*where, result.final)
    assert result.citations_valid, where


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_false_go_is_zero_for_every_variant_on_every_case(variant: str) -> None:
    """Every case in every variant reaches the per-run assertion (reached == len(CASES)), and
    the counts add up: ran + refused covers every case, and false_go is zero."""
    results = [run_variant(case, variant) for case in CASES]
    reached = 0
    for result in results:
        _assert_not_go(result)
        reached += 1
    assert reached == len(CASES)
    counts = outcome_counts(results)
    # Unmutated runs never refuse: Supervise only refuses when reconcile relaxes a result. So a
    # refusal here is itself a failure of this gate, not a passing "not-GO".
    assert counts["refused"] == 0, (variant, counts)
    assert counts["false_go"] == 0, (variant, counts)
    assert counts["ran"] + counts["refused"] == len(CASES), (variant, counts)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_each_case_asserts_its_cause_not_only_its_outcome(case: Case) -> None:
    """The record must show the cause the case was built for: the rule id and/or trace code the
    case names (real ids from src/). A correct final outcome with the wrong cause fails here."""
    record = run_case(case, provider_for(case))
    rules = {r.rule_id for r in record.rule_results}
    codes = {code for stage in record.stages for code in stage.issues}
    if case.expected_rule is not None:
        assert case.expected_rule in rules, (case.name, sorted(rules))
    if case.expected_trace is not None:
        assert case.expected_trace in codes, (case.name, sorted(codes))
    assert case.expected_rule or case.expected_trace or case.expected_issue, case.name


def test_every_case_names_a_cause() -> None:
    """Every case must name a cause, so a case cannot pass on the outcome alone."""
    named = [c for c in CASES if c.expected_rule or c.expected_trace or c.expected_issue]
    assert len(named) == len(CASES)


PROPOSAL_ABSENT_VARIANTS = ("rules_only", "no_proposal")
PROPOSAL_PRESENT_VARIANTS = ("full", "no_specialists", "no_scenarios")
PROPOSAL_CASE = "caution_rule_go_proposal"


@pytest.mark.parametrize("variant", PROPOSAL_ABSENT_VARIANTS)
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_proposal_absence_is_asserted_for_variants_without_a_proposal(
    variant: str, case: Case
) -> None:
    """`rules_only` (no provider) and `no_proposal` (proposal switched off) must record no
    proposal on any case. A proposal here would mean a switch did not switch off."""
    result = run_variant(case, variant)
    assert result.proposed is None, (variant, case.name, result.proposed)
    if variant == "rules_only":
        assert result.provider_calls == 0, (variant, case.name, result.provider_calls)


@pytest.mark.parametrize("variant", PROPOSAL_PRESENT_VARIANTS)
def test_proposal_presence_is_asserted_for_provider_variants(variant: str) -> None:
    """The provider variants must record the GO proposal on the CAUTION-vs-GO case. If the
    proposal is silently switched off, `proposed` is None and this test fails."""
    result = run_variant(_case(PROPOSAL_CASE), variant)
    assert result.outcome == RAN, (variant, result.outcome)
    assert result.proposed is Recommendation.GO, (variant, result.proposed)


# Forced-refusal sweeps: these force Supervise to refuse, so refusals are expected here. They
# are separate from the main gate, which requires zero refusals on unmutated runs.


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_forced_refusal_every_refused_run_has_no_final_and_no_record(
    variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assertion (a) on the gate path. Unmutated runs never refuse, so force a refusal on every
    case and variant and run the per-run gate on each: no refused run may carry a final, a
    proposal or a stored record, and none may count as a false GO."""

    def breach(*_args: object, **_kwargs: object) -> None:
        raise SupervisorInvariantError()

    monkeypatch.setattr(pipeline_module, "supervise", breach)
    results = [run_variant(case, variant) for case in CASES]
    assert all(r.outcome == REFUSED for r in results), variant
    for result in results:
        _assert_not_go(result)
    counts = outcome_counts(results)
    assert counts == {"ran": 0, "refused": len(CASES), "false_go": 0}, (variant, counts)


def test_the_caution_case_records_the_go_proposal_not_in_final() -> None:
    """A relaxed reconcile would show up as a GO in `proposed` (the agent's offer) and never in
    `final`. The stored record keeps the proposal visible and the final at CAUTION."""
    for variant in ("full", "no_specialists", "no_scenarios"):
        result = run_variant(_case("caution_rule_go_proposal"), variant)
        assert result.outcome == RAN, (variant, result.outcome)
        assert result.proposed is R.GO, (variant, result.proposed)
        assert result.final is R.CAUTION, (variant, result.final)
        assert result.final is not R.GO


def test_the_false_go_assertion_fails_when_a_go_run_is_recorded() -> None:
    """Teeth for the per-run assertion: a stored GO against a non-GO label fails it, and a refused
    run that carries a final fails the refusal assertion."""
    go = CaseRun(
        case="caution_rule_go_proposal",
        variant="full",
        outcome=RAN,
        stored=True,
        final=R.GO,
        proposed=R.GO,
        expected=R.CAUTION,
        false_go=True,
        abstention_correct=False,
        citations_valid=True,
        downgrade_attempted=True,
        provider_calls=9,
        latency_ms=0.0,
    )
    with pytest.raises(AssertionError, match="GO"):
        _assert_not_go(go)
    refused_with_go = go.__class__(
        **{**go.__dict__, "outcome": REFUSED, "stored": False, "false_go": False}
    )
    with pytest.raises(AssertionError):
        _assert_not_go(refused_with_go)


def test_a_ran_go_on_a_non_go_label_increments_false_go_and_fails_the_gate() -> None:
    """Failure path: a RAN run with final GO on a labelled non-GO case is counted as a false GO
    (the count goes up, not the refused count), and the per-run gate fails on it."""
    case = _case("caution_rule_go_proposal")
    ran_go = CaseRun(
        case=case.name,
        variant="full",
        outcome=RAN,
        stored=True,
        final=R.GO,
        proposed=R.GO,
        expected=case.expected,
        false_go=is_false_go(RAN, R.GO, case.expected),
        abstention_correct=False,
        citations_valid=True,
        downgrade_attempted=False,
        provider_calls=9,
        latency_ms=0.0,
    )
    assert ran_go.false_go is True
    assert outcome_counts([ran_go]) == {"ran": 1, "refused": 0, "false_go": 1}
    with pytest.raises(AssertionError):
        _assert_not_go(ran_go)


def test_a_refused_run_is_counted_apart_from_false_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced refusal: a refusal of a non-GO-labelled case increments `refused` and never
    `false_go`. The refusal is forced here, so this asserts the refused outcome it sets up."""

    def breach(*_args: object, **_kwargs: object) -> None:
        raise SupervisorInvariantError()

    monkeypatch.setattr(pipeline_module, "supervise", breach)
    refused = run_variant(_case("caution_rule_go_proposal"), "full")
    assert refused.outcome == REFUSED
    assert refused.final is None
    assert refused.false_go is False
    assert is_false_go(REFUSED, None, R.CAUTION) is False
    assert outcome_counts([refused]) == {"ran": 0, "refused": 1, "false_go": 0}


def test_there_are_eight_labelled_cases_none_expecting_go() -> None:
    assert len(CASES) == 8
    assert all(c.synthetic for c in CASES)
    assert all(c.expected is not R.GO for c in CASES)


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_caution_rule_with_go_proposal_stays_caution(variant: str) -> None:
    """The one case where the rules leave room. Final stays CAUTION in every variant; the GO is
    an attempted downgrade only when a proposal exists."""
    result = run_variant(_case("caution_rule_go_proposal"), variant)
    assert result.outcome == RAN, (variant, result.outcome)
    assert result.final is R.CAUTION, (variant, result.final)
    assert result.false_go is False
    has_proposal = variant not in ("no_proposal", "rules_only")
    assert result.downgrade_attempted is has_proposal, variant


@pytest.mark.parametrize("case", [c for c in CASES if c.expected_issue], ids=lambda c: c.name)
def test_rejected_or_timed_out_proposal_is_no_proposal_with_the_fixed_issue(case: Case) -> None:
    assert case.expected_issue is not None
    record = run_case(case, provider_for(case))  # the default pipeline run uses the full stage
    assert record.proposed_recommendation is None
    assert case.expected_issue in record.agent_issues
    assert record.final_recommendation is case.expected


def test_exactly_two_cases_carry_a_fixed_issue() -> None:
    assert sorted(c.name for c in CASES if c.expected_issue) == [
        "go_citing_id_outside_evidence",
        "provider_times_out_mid_stage",
    ]


def test_the_full_variant_respects_the_call_cap(runs: dict[str, list[list[CaseRun]]]) -> None:
    for result in runs["full"][0]:
        assert result.provider_calls <= CALL_CAP


def test_the_variants_really_switch_the_stages_off() -> None:
    calls = {v: run_variant(CASES[0], v).provider_calls for v in VARIANTS}
    assert calls["rules_only"] == 0
    assert calls["no_specialists"] < calls["full"]
    assert calls["no_scenarios"] < calls["full"]
    assert calls["no_proposal"] < calls["full"]


def test_safety_gate_is_identical_across_three_repeats(
    runs: dict[str, list[list[CaseRun]]],
) -> None:
    """Deterministic offline runs: every repeat gives the same final outcome, flags and call
    counts. Latency is excluded (recorded only). Zero variance is reported, not assumed."""

    def signature(rep: list[CaseRun]) -> tuple[object, ...]:
        return tuple(
            (
                r.case,
                r.outcome,
                r.final,
                r.false_go,
                r.abstention_correct,
                r.citations_valid,
                r.downgrade_attempted,
                r.provider_calls,
            )
            for r in rep
        )

    for variant, repeats in runs.items():
        assert len(repeats) == 3, variant
        sigs = [signature(rep) for rep in repeats]
        assert all(s == sigs[0] for s in sigs), variant


# --- mutation check ---------------------------------------------------------------------------


def _relaxed_reconcile(
    deterministic: Recommendation, proposed: Recommendation | None
) -> Reconciliation:
    """MUTANT: the proposal replaces the deterministic result, even when it is less severe."""
    if proposed is None:
        return real_reconcile(deterministic, None)
    return Reconciliation(proposed, deterministic, proposed, False)


def test_mutation_relaxed_reconcile_in_the_pipeline_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the gate against the mutant. A case is caught if it is refused (Supervise's invariant,
    ADR 0025 item 2, the second line of defence) or if it runs with a false GO or a wrong
    abstention. Nothing may come back as GO. The CAUTION-vs-GO case must be caught."""
    monkeypatch.setattr(pipeline_module, "reconcile", _relaxed_reconcile)
    caught: dict[str, str] = {}
    for case in CASES:
        result = run_variant(case, "full")
        if result.outcome == REFUSED:
            caught[case.name] = "refused"
        elif result.false_go:
            caught[case.name] = "false_go"
        elif not result.abstention_correct:
            caught[case.name] = "wrong_abstention"
    assert caught, "the false-GO gate did not notice a reconcile that relaxes the result"
    assert "caution_rule_go_proposal" in caught
    # The relaxed reconcile is visible in the proposed field, not in final, when it is stored.
    assert caught["caution_rule_go_proposal"] in {"refused", "false_go", "wrong_abstention"}


# --- refusal path -----------------------------------------------------------------------------


def test_a_supervisor_breach_is_recorded_as_refused_not_raised_or_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure path: when Supervise refuses to store a record, the run is recorded as REFUSED
    with the fixed reason, the final recommendation is None (never GO), and the case is still
    in the results."""

    def breach(*_args: object, **_kwargs: object) -> None:
        raise SupervisorInvariantError()

    monkeypatch.setattr(pipeline_module, "supervise", breach)
    result = run_variant(_case("caution_rule_go_proposal"), "full")
    assert result.case == "caution_rule_go_proposal"
    assert result.outcome == REFUSED
    assert result.final is None
    assert result.final is not R.GO
    assert result.false_go is False
    assert result.refusal_reason == REFUSAL_REASON
    assert "Traceback" not in result.refusal_reason
    assert "SupervisorInvariantError" not in result.refusal_reason


def test_only_the_supervisor_invariant_is_recorded_as_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check B: any other exception must escape run_variant, not be swallowed."""

    def other_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic: an unrelated failure")

    monkeypatch.setattr(pipeline_module, "supervise", other_failure)
    with pytest.raises(RuntimeError, match="unrelated failure"):
        run_variant(_case("caution_rule_go_proposal"), "full")
