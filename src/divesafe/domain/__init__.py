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
from divesafe.domain.lineage import are_independent, can_corroborate
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
from divesafe.domain.quality import RANGE_CHECK, UNIT_CHECK, effective_quality
from divesafe.domain.risk_types import THRESHOLD_FACTORS, RiskFactorKind, ThresholdStatus
from divesafe.domain.rule_definition import (
    FACTOR_METRICS,
    SUPPORTED_METRICS,
    Comparison,
    Limit,
    Metric,
    Person,
    Review,
    RuleDefinition,
    RuleScope,
    SourceCitation,
    WorseWhen,
)
from divesafe.domain.site import DiveSite, SiteConstraint

Evidence = EvidenceItem
RiskFactor = RuleResult
PostDiveObservation = ActualConditions

__all__ = [
    "FACTOR_METRICS",
    "SUPPORTED_METRICS",
    "Comparison",
    "Limit",
    "Metric",
    "Person",
    "Review",
    "RuleDefinition",
    "RuleScope",
    "SourceCitation",
    "WorseWhen",
    "NOT_MEASURED",
    "RANGE_CHECK",
    "THRESHOLD_FACTORS",
    "UNIT_CHECK",
    "are_independent",
    "can_corroborate",
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
    "effective_quality",
    "observation_from_evidence",
    "severity",
    "unevaluated_factors",
]
