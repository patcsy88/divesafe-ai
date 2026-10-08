"""Provenance vocabulary: where a fact came from and how it was handled.

Every environmental observation used in an assessment must be traceable to its source,
location, kind (observation / forecast / model / prediction / notice / knowledge), retrieval
time, validity window, quality and transformation history. `EvidenceItem` carries these.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from divesafe.domain.base import Frozen


class DataKind(StrEnum):
    """What a value IS. Never present one kind as another."""

    OBSERVATION = "observation"  # measured
    FORECAST = "forecast"  # agency forecast of a future state
    MODEL = "model"  # numerical model output, including hours already past
    PREDICTION = "prediction"  # astronomical or statistical prediction (e.g. tide tables)
    NOTICE = "notice"  # official warning or advisory text
    KNOWLEDGE = "knowledge"  # curated document (RAG): context, never live conditions


# Kinds that describe a state that has not been measured.
NOT_MEASURED: frozenset[DataKind] = frozenset(
    {DataKind.FORECAST, DataKind.MODEL, DataKind.PREDICTION}
)


class DataQuality(StrEnum):
    """Nobody has assessed quality by default. A connector must not claim more than it checked."""

    UNASSESSED = "unassessed"
    VALIDATED = "validated"  # passed documented range, unit and completeness checks
    DEGRADED = "degraded"  # usable but with a known limitation (see quality_notes)
    REJECTED = "rejected"  # failed validation; must not support a recommendation


class GeoPoint(Frozen):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    description: str | None = None


class TransformationStep(Frozen):
    """One step applied between the provider's payload and the stored value."""

    step: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=500)
