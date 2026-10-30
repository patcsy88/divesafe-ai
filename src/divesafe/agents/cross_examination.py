"""Second-round cross-examination of specialist findings (ADR 0025 item 5).

One extra call per specialist, after the five first-round calls. The specialist reads its PEERS'
findings (never its own) and may only ADD questions, returned in `conflicting_signals` of a
`FindingOutput`. Round one is never changed: round-two output is a separate `Finding`, validated
on its own. Each call may cite only evidence ids in the evidence list that are in its own
categories or were cited by a peer. Exactly one round; nothing recurses.

Peer findings are LLM output: they enter the prompt only inside `<untrusted_data>` through
`neutralize`, each with a code-set `source_agent` and `llm_generated: true`. Self-estimates are
dropped by `agent_view`. A total size cap is applied to the raw JSON BEFORE neutralising; an
over-cap input skips the call (never trims). A failure yields no second-round finding for that
specialist and a fixed issue string; nothing from the reply is copied into issues or logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from divesafe.agents.prompts import (
    agent_view,
    build_user_message,
    evidence_payload,
    warning_note,
)
from divesafe.agents.runner import MAX_EVIDENCE_ITEMS, AgentError, call_structured
from divesafe.agents.schemas import FindingOutput
from divesafe.agents.specialists import SPECIALISTS, SpecialistSpec, relevant_evidence
from divesafe.domain import DivePlan, EvidenceItem, Finding
from divesafe.models import LLMProvider

logger = logging.getLogger(__name__)

# Total size of the peer-finding JSON, in characters, before neutralising. Over this the call is
# skipped; the input is never trimmed. A capacity bound, not a safety threshold.
MAX_PEER_CHARS = 20_000
PEER_TASK_PREFIX = "cross_examination:"
CROSS_EXAMINATION_TOKEN = "; cross_examination=on"
ROUND_TWO_SUMMARY = "Second-round questions for the peer findings."

CrossOutcomeKind = Literal["ok", "failed", "timed_out", "skipped", "rejected"]


@dataclass(frozen=True)
class CrossOutcome:
    """How one specialist's second-round call ended: a fixed label, no text. `specialist` is a
    name set in code (`SPECIALISTS`)."""

    specialist: str
    outcome: CrossOutcomeKind


def cross_examination_token(enabled: bool) -> str:
    """The fixed model-version suffix when the round is on; empty when off."""
    return CROSS_EXAMINATION_TOKEN if enabled else ""


def issue_for(specialist: str, outcome: CrossOutcomeKind) -> str:
    """The fixed agent issue for a second-round failure. `outcome` is one of the fixed labels."""
    return f"cross_examination:{specialist}:{outcome}"


def _peer_payload(
    own: SpecialistSpec, findings: Sequence[Finding], peers_evidence: Sequence[str]
) -> list[dict[str, Any]]:
    """Peer findings only, each marked with its source and as LLM output. Self-estimates are
    dropped by `agent_view`."""
    peers = [f for f in findings if f.agent != own.name and not f.no_evidence]
    views = agent_view(peers)
    return [
        {**view, "source_agent": f.agent, "llm_generated": True}
        for f, view in zip(peers, views, strict=True)
    ] + [{"peer_cited_evidence_ids": sorted(set(peers_evidence))}]


def allowed_ids_for(
    own: SpecialistSpec, evidence: Sequence[EvidenceItem], findings: Sequence[Finding]
) -> frozenset[str]:
    """Own categories' evidence ids plus peer-cited ids that are in the evidence list."""
    own_ids = {e.id for e in relevant_evidence(own, evidence)}
    known = {e.id for e in evidence}
    peer_ids = {i for f in findings if f.agent != own.name for i in f.evidence_ids if i in known}
    return frozenset(own_ids | peer_ids)


async def cross_examine_one(
    provider: LLMProvider,
    spec: SpecialistSpec,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    findings: Sequence[Finding],
) -> Finding:
    """One second-round call. Raises `AgentError` (with a fixed kind) on any refusal."""
    own_items = relevant_evidence(spec, evidence)
    if not own_items or len(own_items) > MAX_EVIDENCE_ITEMS:
        raise AgentError(f"{spec.name}: no own evidence for the second round", kind="skipped")
    peers = [f for f in findings if f.agent != spec.name and not f.no_evidence]
    if not peers:
        raise AgentError(f"{spec.name}: no peer findings", kind="skipped")

    peer_cited = [i for f in peers for i in f.evidence_ids]
    payload = _peer_payload(spec, findings, peer_cited)
    if len(json.dumps(payload, sort_keys=True, default=str)) > MAX_PEER_CHARS:
        raise AgentError(f"{spec.name}: peer findings over the size cap", kind="skipped")

    allowed = allowed_ids_for(spec, evidence, findings)
    message = build_user_message(
        task=f"{PEER_TASK_PREFIX}{spec.name}",
        instructions=(
            f"You are the {spec.name} specialist, cross-examining the peer findings below. "
            "Add only questions or conflicting signals that the peers' findings raise against "
            "the evidence. You cannot change your own earlier finding. Do not recommend GO, "
            "CAUTION or NO-GO. Cite only evidence ids in ALLOWED_EVIDENCE_IDS."
            + warning_note(evidence)
        ),
        plan=plan,
        allowed_ids=sorted(allowed),
        untrusted={
            "evidence": evidence_payload(own_items),
            "peer_findings": payload,
        },
        schema=FindingOutput.model_json_schema(),
    )
    out = await call_structured(
        provider,
        task=f"{PEER_TASK_PREFIX}{spec.name}",
        user_message=message,
        output=FindingOutput,
        allowed_ids=allowed,
    )
    if out.risk_factors:
        # Round two adds questions only; a reply with risk factors is refused, never trimmed.
        raise AgentError(f"{spec.name}: second-round reply carried risk factors", kind="rejected")
    return Finding(
        agent=spec.name,
        # The model's summary text is never stored or shown; only the code-set one below is.
        summary=ROUND_TWO_SUMMARY,
        risk_factors=(),
        conflicting_signals=out.conflicting_signals,
        evidence_ids=out.evidence_ids,
        confidence=None,
    )


async def run_cross_examination(
    provider: LLMProvider,
    plan: DivePlan,
    evidence: Sequence[EvidenceItem],
    findings: Sequence[Finding],
    timeout: float,
) -> tuple[tuple[Finding, ...], tuple[CrossOutcome, ...], tuple[str, ...]]:
    """Run the second round for every specialist, in parallel. Each call is bounded by its own
    call timeout inside `call_structured`, so a slow call times out alone (`timed_out`) and the
    others complete. The stage deadline in `run_agent_stage` is the outer bound: if it fires, the
    whole stage is cut off there, as for round one. Returns (second-round findings, one outcome per
    specialist, fixed issues)."""
    del timeout  # the stage deadline bounds the round; kept in the signature for the caller
    results: list[object] = list(
        await asyncio.gather(
            *(cross_examine_one(provider, spec, plan, evidence, findings) for spec in SPECIALISTS),
            return_exceptions=True,
        )
    )
    added: list[Finding] = []
    outcomes: list[CrossOutcome] = []
    issues: list[str] = []
    for spec, result in zip(SPECIALISTS, results, strict=True):
        if isinstance(result, Finding):
            added.append(result)
            outcomes.append(CrossOutcome(spec.name, "ok"))
        elif isinstance(result, AgentError):
            outcome = _label(result.kind)
            outcomes.append(CrossOutcome(spec.name, outcome))
            issues.append(issue_for(spec.name, outcome))
        elif isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result  # cancellation and interpreter exits must propagate
        else:
            outcomes.append(CrossOutcome(spec.name, "failed"))
            issues.append(issue_for(spec.name, "failed"))
    logger.info(
        "cross-examination finished",
        extra={"ok": len(added), "issues": len(issues)},
    )
    return tuple(added), tuple(outcomes), tuple(issues)


def _label(kind: str) -> CrossOutcomeKind:
    if kind in ("failed", "timed_out", "rejected", "skipped"):
        return kind  # type: ignore[return-value]
    return "failed"
