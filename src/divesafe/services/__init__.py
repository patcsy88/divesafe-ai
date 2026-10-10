"""Platform services: evidence/provenance, historical data, feedback/learning, persistence."""

from divesafe.services.evidence import (
    DuplicateEvidenceError,
    EvidenceSet,
    build_evidence_set,
    gather_evidence,
)
from divesafe.services.postgres import PostgresAssessmentRepository
from divesafe.services.repository import (
    AssessmentRepository,
    ConflictError,
    InMemoryAssessmentRepository,
    InvalidSuccessorError,
    RecordExistsError,
    RecordNotFoundError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    UnstorableRecordError,
    check_successor,
)

__all__ = [
    "AssessmentRepository",
    "ConflictError",
    "DuplicateEvidenceError",
    "EvidenceSet",
    "InMemoryAssessmentRepository",
    "InvalidSuccessorError",
    "PostgresAssessmentRepository",
    "RecordExistsError",
    "RecordNotFoundError",
    "RepositoryIntegrityError",
    "RepositoryUnavailableError",
    "UnstorableRecordError",
    "build_evidence_set",
    "check_successor",
    "gather_evidence",
]
