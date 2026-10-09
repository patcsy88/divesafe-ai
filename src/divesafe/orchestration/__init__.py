"""Agent orchestrator: Observe, Retrieve, Diagnose/Reason, Assess Risk, Propose, Human Gate,
Validate, Learn. Design: docs/architecture.md and docs/agent-design.md.

Implemented so far: evidence gathering, deterministic risk, reconciliation, and the human gate
(no LLM, RAG or agents yet).
"""

from divesafe.orchestration.human_gate import (
    AlreadyDecidedError,
    DecisionRequiredError,
    decide,
    report_actual_conditions,
)
from divesafe.orchestration.pipeline import InvalidPlanError, UnsafeConfigurationError, assess_dive
from divesafe.orchestration.ruleset import (
    LoadedRuleset,
    Refusal,
    RulesetError,
    load_ruleset,
    load_ruleset_bytes,
    read_ruleset_file,
)

__all__ = [
    "LoadedRuleset",
    "Refusal",
    "RulesetError",
    "load_ruleset",
    "load_ruleset_bytes",
    "read_ruleset_file",
    "AlreadyDecidedError",
    "DecisionRequiredError",
    "InvalidPlanError",
    "UnsafeConfigurationError",
    "assess_dive",
    "decide",
    "report_actual_conditions",
]
