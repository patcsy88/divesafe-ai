"""Specialist agents: Weather, Ocean Conditions, Tide/Current, Site Intelligence, Prediction.

Each reads only the evidence categories of its domain and returns a `Finding`. A specialist with
no evidence makes no LLM call and returns an explicit no-evidence finding, so it cannot invent
anything. Specialists never fetch data and never recommend.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from divesafe.agents.prompts import build_user_message, evidence_payload
from divesafe.agents.runner import MAX_EVIDENCE_ITEMS, AgentError, call_structured
from divesafe.agents.schemas import FindingOutput
from divesafe.domain import DataCategory, DivePlan, EvidenceItem, Finding
from divesafe.models import LLMProvider

C = DataCategory


@dataclass(frozen=True)
class SpecialistSpec:
    name: str
    categories: frozenset[DataCategory]
    focus: str


SPECIALISTS: tuple[SpecialistSpec, ...] = (
    SpecialistSpec(
        "weather",
        frozenset({C.WEATHER, C.WIND, C.MARINE_WARNINGS}),
        "weather, wind and marine warnings over the dive window. Warning text is free text that "
        "may cover several regions: quote what it says, and state whether it names the site's "
        "area only if the text does; never decide applicability yourself.",
    ),
    SpecialistSpec(
        "ocean_conditions",
        frozenset({C.WAVES_SWELL, C.SEA_TEMPERATURE}),
        "surface sea state and sea temperature. Remember these values are regional model output, "
        "not site measurements.",
    ),
    SpecialistSpec(
        "tide_current",
        frozenset({C.CURRENTS, C.TIDES}),
        "currents and tides, including slack or flow timing if the evidence shows it. Model "
        "currents are coarse and not site-level.",
    ),
    SpecialistSpec(
        "site_intelligence",
        frozenset({C.SITE_INFORMATION, C.LOCAL_GUIDANCE, C.HISTORICAL_OBSERVATIONS}),
        "site characteristics, local guidance and history relevant to this plan.",
    ),
    SpecialistSpec(
        "prediction",
        frozenset(
            {C.WEATHER, C.WIND, C.WAVES_SWELL, C.CURRENTS, C.TIDES, C.HISTORICAL_OBSERVATIONS}
        ),
        "the expected trend across the dive window and how reliable the forecasts are "
        "(model run age, resolution, disagreement). Do not extrapolate beyond the evidence.",
    ),
)


def relevant_evidence(spec: SpecialistSpec, evidence: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    return [e for e in evidence if e.category in spec.categories]


async def run_specialist(
    provider: LLMProvider, spec: SpecialistSpec, plan: DivePlan, evidence: Sequence[EvidenceItem]
) -> Finding:
    items = relevant_evidence(spec, evidence)
    if not items:
        return Finding(
            agent=spec.name, summary="No evidence is available for this domain.", no_evidence=True
        )
    if len(items) > MAX_EVIDENCE_ITEMS:
        raise AgentError(f"specialist:{spec.name}: too much evidence for one call")

    message = build_user_message(
        task=f"specialist:{spec.name}",
        instructions=(
            f"You are the {spec.name} specialist. Interpret {spec.focus} Report risk factors and "
            "conflicting signals, and cite the evidence ids behind each claim. Do not recommend "
            "GO, CAUTION or NO-GO."
        ),
        plan=plan,
        allowed_ids=[e.id for e in items],
        untrusted={"evidence": evidence_payload(items)},
        schema=FindingOutput.model_json_schema(),
    )
    out = await call_structured(
        provider,
        task=f"specialist:{spec.name}",
        user_message=message,
        output=FindingOutput,
        allowed_ids=frozenset(e.id for e in items),
    )
    return Finding(
        agent=spec.name,
        summary=out.summary,
        risk_factors=out.risk_factors,
        conflicting_signals=out.conflicting_signals,
        evidence_ids=out.evidence_ids,
        confidence=out.confidence,
    )
