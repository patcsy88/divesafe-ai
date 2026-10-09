# Agent design

## Principles

- Agents are **specialists**: each interprets one evidence domain and returns a structured
  `Finding` (summary, risk factors, conflicting signals, confidence, evidence IDs).
- Agents **do not fetch data** (connectors do) and **do not decide** (`reconcile` does).
- Agents call the LLM only through `LLMProvider`. Prompts request structured output validated
  by Pydantic; invalid output is rejected, never repaired by guessing.
- Every output must cite at least one valid `EvidenceItem` ID from the evidence it was given;
  an output with no valid citation is rejected whole. Claims are not individually bound to ids,
  and invented numbers in prose are not detected, so prose is untrusted (ADR 0007).
- Agents are stateless; all state lives in the orchestrator's run context and the audit record.

## Agents

| Agent | Evidence domain | Output |
| --- | --- | --- |
| Site Intelligence | Site characteristics, hazards, local guidance, history (via RAG) | Site-specific risk factors and caveats |
| Weather | Weather, wind, warnings | Trend and hazards over the dive window |
| Ocean Conditions | Waves/swell, sea temperature | Surface and in-water conditions |
| Tide/Current | Tides, tidal and ocean currents | Slack/flow timing and current hazards |
| Risk Assessment | All findings plus rules result | Proposed recommendation, confidence, uncertainty |
| Prediction | Historical observations and forecasts | Expected trend and forecast reliability |

## Orchestrator

The orchestrator owns the lifecycle (Observe, Retrieve, Diagnose/Reason, Assess Risk,
Propose, Human Gate, Validate, Learn):

1. Resolves required evidence from the rules engine's `EvidencePolicy` and calls connectors.
2. Runs domain agents concurrently on the validated evidence.
3. Runs the bounded scenario evaluation ([tot-design.md](tot-design.md)).
4. Runs the rules engine, then the Risk Assessment agent, then `reconcile`.
5. Persists an `AssessmentRecord` and **stops at the human gate**. It never marks an
   assessment as final or acted on without a recorded `HumanDecision`.

## Failure behavior

| Failure | Result |
| --- | --- |
| Connector error or stale data | Required category missing, so `INSUFFICIENT EVIDENCE` |
| Agent output fails validation | Finding discarded; absence is reported, not guessed |
| LLM unavailable | Deterministic result and evidence summary returned without prose |
| Agents disagree | Conflict surfaced in the record; confidence lowered; severity never lowered |

## Testing

`tests/agent` uses `FakeProvider` with scripted outputs. Required cases: hostile or
contradictory LLM output cannot lower severity; uncited claims are dropped; provider swap does
not change the final recommendation for identical deterministic inputs.

## Canonical input (ADR 0010)

Agents read marine and wind evidence as canonical, unit-bearing conditions with lineage, the
engine's judged quality and the connector's limitations, not provider-shaped payloads. They are
told that agreement corroborates only between independent items (both lineages known, nothing
shared). Free-text notices keep their raw text inside the untrusted block.

## Implementation status (2026-10-09)

Implemented in `divesafe.agents` and wired into `assess_dive(provider=...)`; see
[ADR 0007](adr/0007-agent-and-prompt-layer.md) for bounds, validation and the prompt layer.

- Specialists: `weather`, `ocean_conditions`, `tide_current`, `site_intelligence`, `prediction`.
  Evidence categories per specialist are in `agents/specialists.py`. `site_intelligence` always
  reports "no evidence" until RAG and site data exist, and `tide_current` only sees currents
  until a verified tide source exists.
- The Risk Assessment agent returns a proposal that `reconcile` can only tighten.
- Not implemented: RAG retrieval, a statistical Prediction model, real provider adapters, the
  Learn stage, and re-running agents after a human decision.
- Verified only with a scripted fake LLM. Behaviour with a real model (instruction-following,
  injection resistance, JSON reliability, cost, latency) is unmeasured.
