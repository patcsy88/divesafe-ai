"""Platform services: evidence/provenance, historical data, feedback/learning, persistence."""

from divesafe.services.evidence import (
    DuplicateEvidenceError,
    EvidenceSet,
    build_evidence_set,
    gather_evidence,
)
from divesafe.services.repository import (
    AssessmentRepository,
    ConflictError,
    InMemoryAssessmentRepository,
    InvalidSuccessorError,
    RecordExistsError,
    RecordNotFoundError,
    check_successor,
)

__all__ = [
    "AssessmentRepository",
    "ConflictError",
    "DuplicateEvidenceError",
    "EvidenceSet",
    "InMemoryAssessmentRepository",
    "InvalidSuccessorError",
    "RecordExistsError",
    "RecordNotFoundError",
    "build_evidence_set",
    "check_successor",
    "gather_evidence",
]
