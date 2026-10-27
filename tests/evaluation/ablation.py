"""Ablation harness (ADR 0025 item 4): the adversarial cases under five variants.

Variants: `full`, `no_specialists`, `no_scenarios`, `no_proposal` (provider present, one agent
group switched off), and `rules_only` (no provider). The switches (`specialists`, `scenarios`,
`proposal`) are keyword-only parameters of `run_agent_stage` in `agents/stage.py`, added for this
harness. `assess_dive` and `orchestration/pipeline.py` never pass them. This harness reaches them
only through `switched()`, a TEST-ONLY wrapper monkeypatched into the pipeline module. All cases
are SYNTHETIC and labelled.

Every case lands in exactly one of two outcomes:
- `RAN`: a record was stored. Its final recommendation is checked against the label.
- `REFUSED`: Supervise refused to store a record (`SupervisorInvariantError`). Nothing is stored,
  so there is no final recommendation. The refusal is reported as a fixed reason and is never
  counted as a false GO. Any other exception escapes.

`false_go` counts only RAN runs whose final recommendation is GO while the label is not GO.
Refusals are reported separately and are never folded into it.

Latency is recorded only (never gated). Runs are deterministic offline, so repeats should match;
live variance is not measured here.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from tests.evaluation.adversarial import CASES, Case, provider_for, run_case

from divesafe.domain import AssessmentRecord, Recommendation
from divesafe.orchestration import pipeline as pipeline_module
from divesafe.orchestration.supervisor import SupervisorInvariantError

# Upper bound on provider calls for `full`: 5 specialists + 3 scenarios + 1 proposal (ADR 0007).
# The safety gate checks this literal against the source counts.
CALL_CAP = 9
VARIANTS: dict[str, dict[str, bool] | None] = {
    "full": {},
    "no_specialists": {"specialists": False},
    "no_scenarios": {"scenarios": False},
    "no_proposal": {"proposal": False},
    "rules_only": None,  # no provider
}
RAN = "ran"
REFUSED = "refused"
# Fixed text. The exception's own message is never copied into a result.
REFUSAL_REASON = "supervisor invariant breached; nothing was stored"


@contextlib.contextmanager
def switched(flags: dict[str, bool]) -> Iterator[None]:
    """Test-only seam: pass the ablation flags to the real `run_agent_stage`. Restores on exit."""
    original = pipeline_module.run_agent_stage

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await original(*args, **kwargs, **flags)

    pipeline_module.run_agent_stage = wrapper  # type: ignore[assignment]
    try:
        yield
    finally:
        pipeline_module.run_agent_stage = original  # type: ignore[assignment]


def _citations_valid(record: AssessmentRecord) -> bool:
    known = {e.id for e in record.evidence}
    cited = set(record.proposal_evidence_ids)
    cited |= {i for f in record.findings for i in f.evidence_ids}
    cited |= {i for s in record.scenarios for i in s.evidence_ids}
    return cited <= known


def is_false_go(outcome: str, final: Recommendation | None, expected: Recommendation) -> bool:
    """The one false-GO rule: a stored record whose final outcome is GO against a non-GO label."""
    return outcome == RAN and final is Recommendation.GO and expected is not Recommendation.GO


@dataclass(frozen=True)
class CaseRun:
    case: str
    variant: str
    outcome: str  # RAN or REFUSED
    stored: bool  # True only for RAN: a record reached storage
    final: Recommendation | None  # None for a refused run
    proposed: Recommendation | None  # the agent proposal as recorded; None if no record
    expected: Recommendation
    false_go: bool
    abstention_correct: bool  # always False for a refused run (no outcome was produced)
    citations_valid: bool  # True for a refused run: nothing was stored, so nothing is cited
    downgrade_attempted: bool  # a less severe proposal was discarded (llm_attempted_downgrade)
    provider_calls: int
    latency_ms: float  # recorded only, never gated
    refusal_reason: str = ""


def run_variant(case: Case, variant: str) -> CaseRun:
    flags = VARIANTS[variant]
    provider = None if flags is None else provider_for(case)
    started = time.perf_counter()
    try:
        if flags is None:
            record = run_case(case, None)
        else:
            with switched(flags):
                record = run_case(case, provider)
    except SupervisorInvariantError:
        latency = (time.perf_counter() - started) * 1000
        calls = 0 if provider is None else len(provider.requests)
        return CaseRun(
            case=case.name,
            variant=variant,
            outcome=REFUSED,
            stored=False,
            final=None,
            proposed=None,
            expected=case.expected,
            false_go=is_false_go(REFUSED, None, case.expected),
            abstention_correct=False,
            citations_valid=True,
            downgrade_attempted=False,
            provider_calls=calls,
            latency_ms=latency,
            refusal_reason=REFUSAL_REASON,
        )
    latency = (time.perf_counter() - started) * 1000
    calls = 0 if provider is None else len(provider.requests)
    final = record.final_recommendation
    return CaseRun(
        case=case.name,
        variant=variant,
        outcome=RAN,
        stored=True,
        final=final,
        proposed=record.proposed_recommendation,
        expected=case.expected,
        false_go=is_false_go(RAN, final, case.expected),
        abstention_correct=final is case.expected,
        citations_valid=_citations_valid(record),
        downgrade_attempted=record.llm_attempted_downgrade,
        provider_calls=calls,
        latency_ms=latency,
    )


def outcome_counts(results: Iterable[CaseRun]) -> dict[str, int]:
    """Per-variant counts, reported separately: ran, refused and false_go."""
    rows = list(results)
    return {
        "ran": sum(r.outcome == RAN for r in rows),
        "refused": sum(r.outcome == REFUSED for r in rows),
        "false_go": sum(r.false_go for r in rows),
    }


def run_all(repeats: int = 3) -> dict[str, list[list[CaseRun]]]:
    """variant -> repeat -> one CaseRun per case."""
    return {
        variant: [[run_variant(c, variant) for c in CASES] for _ in range(repeats)]
        for variant in VARIANTS
    }


def summarise(runs: dict[str, list[list[CaseRun]]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for variant, repeats in runs.items():
        flat = [r for rep in repeats for r in rep]
        counts = [sum(r.provider_calls for r in rep) for rep in repeats]
        outcomes = [tuple((r.outcome, r.final) for r in rep) for rep in repeats]
        out[variant] = {
            **outcome_counts(flat),
            "abstention_correct": f"{sum(r.abstention_correct for r in flat)}/{len(flat)}",
            "citations_valid": f"{sum(r.citations_valid for r in flat)}/{len(flat)}",
            "max_calls_per_case": max(r.provider_calls for r in flat),
            "calls_per_repeat": counts,
            "outcome_variance_across_repeats": len(set(outcomes)) > 1,
            "latency_ms_median": sorted(r.latency_ms for r in flat)[len(flat) // 2],
        }
    return out
