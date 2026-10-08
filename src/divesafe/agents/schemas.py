"""Strict structured outputs. Unknown fields are rejected, so a model cannot smuggle a reasoning
trace or any other extra content into storage."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from divesafe.domain import Recommendation

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Rationale = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FindingOutput(_Out):
    summary: Summary
    risk_factors: tuple[ShortText, ...] = Field(default=(), max_length=10)
    conflicting_signals: tuple[ShortText, ...] = Field(default=(), max_length=10)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=120)
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Uncalibrated self-estimate, not a probability or a safety measure",
    )


class ScenarioOutput(_Out):
    summary: Summary
    risk_factors: tuple[ShortText, ...] = Field(default=(), max_length=10)
    conflicting_signals: tuple[ShortText, ...] = Field(default=(), max_length=10)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=120)
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Uncalibrated self-estimate, not a probability or a safety measure",
    )


class ProposalOutput(_Out):
    recommendation: Recommendation
    rationale: Rationale
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=120)
    uncertainties: tuple[ShortText, ...] = Field(default=(), max_length=10)
