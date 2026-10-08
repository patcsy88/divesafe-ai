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

__all__ = [
    "AlreadyDecidedError",
    "DecisionRequiredError",
    "InvalidPlanError",
    "UnsafeConfigurationError",
    "assess_dive",
    "decide",
    "report_actual_conditions",
]
