"""Agent orchestrator: the stage graph (`graph.STAGES`: Observe, Retrieve, Assess, Reason,
Rejudge, Propose, Supervise, Gate) walked by `assess_dive`, and the human gate. Design:
docs/architecture.md, docs/agent-design.md and ADR 0025.

Retrieve is a placeholder until RAG stage 1 (ADR 0022).
"""

from divesafe.orchestration.graph import STAGES, Stage, next_stage, walk
from divesafe.orchestration.human_gate import (
    AlreadyDecidedError,
    DecisionRequiredError,
    NotAssignedDeciderError,
    check_assigned,
    decide,
    report_actual_conditions,
)
from divesafe.orchestration.pipeline import (
    InvalidPlanError,
    SiteMismatchError,
    StartPassedError,
    UnsafeConfigurationError,
    assess_dive,
)
from divesafe.orchestration.ruleset import (
    LoadedRuleset,
    Refusal,
    RulesetError,
    load_ruleset,
    load_ruleset_bytes,
    read_ruleset_file,
)
from divesafe.orchestration.supervisor import SupervisorInvariantError, supervise

__all__ = [
    "STAGES",
    "Stage",
    "SupervisorInvariantError",
    "next_stage",
    "supervise",
    "walk",
    "LoadedRuleset",
    "Refusal",
    "RulesetError",
    "load_ruleset",
    "load_ruleset_bytes",
    "read_ruleset_file",
    "AlreadyDecidedError",
    "NotAssignedDeciderError",
    "check_assigned",
    "DecisionRequiredError",
    "InvalidPlanError",
    "SiteMismatchError",
    "StartPassedError",
    "UnsafeConfigurationError",
    "assess_dive",
    "decide",
    "report_actual_conditions",
]
