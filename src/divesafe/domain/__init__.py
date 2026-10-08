"""Core domain types shared by every layer. Depends on nothing else in the package.

Vocabulary: `Evidence` is `EvidenceItem`; `RiskFactor` is `RuleResult`; `PostDiveObservation`
is `ActualConditions`. The aliases keep the project's concept names without duplicate types.
"""

from divesafe.domain.assessment import ConfidenceAssessment, RiskAssessment, unevaluated_factors
from divesafe.domain.conditions import (
    OceanConditions,
    TidalConditions,
    WaveConditions,
    WeatherConditions,
)
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
from divesafe.domain.observations import (
    EnvironmentalObservation,
    MarineWarning,
    observation_from_evidence,
)
from divesafe.domain.provenance import (
    NOT_MEASURED,
    DataKind,
    DataQuality,
    GeoPoint,
    TransformationStep,
)
from divesafe.domain.risk_types import THRESHOLD_FACTORS, RiskFactorKind, ThresholdStatus
from divesafe.domain.site import DiveSite, SiteConstraint

Evidence = EvidenceItem
RiskFactor = RuleResult
PostDiveObservation = ActualConditions

__all__ = [
    "NOT_MEASURED",
    "THRESHOLD_FACTORS",
    "ActualConditions",
    "AssessmentRecord",
    "ConfidenceAssessment",
    "DataCategory",
    "DataKind",
    "DataQuality",
    "DivePlan",
    "DiveSite",
    "EnvironmentalObservation",
    "Evidence",
    "EvidenceItem",
    "Finding",
    "GeoPoint",
    "HumanDecision",
    "MarineWarning",
    "OceanConditions",
    "PostDiveObservation",
    "Recommendation",
    "RiskAssessment",
    "RiskFactor",
    "RiskFactorKind",
    "RuleResult",
    "Scenario",
    "ScenarioKind",
    "SiteConstraint",
    "ThresholdStatus",
    "TidalConditions",
    "TransformationStep",
    "WaveConditions",
    "WeatherConditions",
    "most_severe",
    "observation_from_evidence",
    "severity",
    "unevaluated_factors",
]
