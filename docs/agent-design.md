# Agent design

## Principles

- Agents are **specialists**: each interprets one evidence domain and returns a structured
  `Finding` (summary, risk factors, conflicting signals, confidence, evidence IDs).
- Agents **do not fetch data** (connectors do) and **do not decide** (`reconcile` does).
- Agents call the LLM only through `LLMProvider`. Prompts request structured output validated
  by Pydantic; invalid output is rejected, never repaired by guessing.
- Every claim must cite `EvidenceItem` IDs. An uncited claim is dropped by validation.
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
