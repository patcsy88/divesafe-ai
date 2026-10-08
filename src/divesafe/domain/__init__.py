"""Core domain types shared by every layer. Depends on nothing else in the package."""

from divesafe.domain.models import (
    ActualConditions,
    AssessmentRecord,
    DataCategory,
    DivePlan,
    EvidenceItem,
    Finding,
    HumanDecision,
    Recommendation,
    RuleResult,
    Scenario,
    ScenarioKind,
    most_severe,
    severity,
)

__all__ = [
    "ActualConditions",
    "AssessmentRecord",
    "DataCategory",
    "DivePlan",
    "EvidenceItem",
    "Finding",
    "HumanDecision",
    "Recommendation",
    "RuleResult",
    "Scenario",
    "ScenarioKind",
    "most_severe",
    "severity",
]
