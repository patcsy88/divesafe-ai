# Tree-of-Thought design (bounded, auditable)

## Purpose

Explore how conditions might play out, so interacting factors and forecast uncertainty are
visible to the human. It is a structured comparison tool, **not** a safety determination.

## Bounds

- **Fixed breadth:** exactly three scenarios (`ScenarioKind`):
  - **A, favourable:** forecasts verify at the benign end of their uncertainty.
  - **B, marginal:** central expectation, with factors near limits.
  - **C, deteriorating:** forecasts verify at the adverse end, or timing slips.
- **Fixed depth:** one expansion level per scenario (evidence -> risk factors -> summary).
  No recursive search, no open-ended self-reflection loops.
- **Fixed budget:** one structured LLM call per scenario, validated against the `Scenario`
  schema. Failure to validate drops that scenario and is recorded.

## Inputs and outputs

Input: validated `EvidenceItem`s, forecast uncertainty and data freshness, and agent
findings. Output per scenario (`divesafe.domain.Scenario`):

- `summary` (short), `evidence_ids`, `risk_factors`, `conflicting_signals`, `confidence`.

Only these concise summaries and references are stored or shown. The model's private
reasoning trace is not requested, stored or exposed.

## Compared dimensions

Evidence, risk factors, conflicting signals, forecast uncertainty, data freshness,
confidence, and expected trend across the dive window.

## What ToT may and may not do

| May | May not |
| --- | --- |
| Surface interacting factors (e.g. wind against current) | Decide GO / NO-GO |
| Widen uncertainty, lower confidence | Lower severity from the rules result |
| Propose a more conservative recommendation | Introduce facts without an evidence ID |
| Highlight which data would resolve uncertainty | Override missing-evidence handling |

ToT output feeds the Risk Assessment agent as one input. The final recommendation is always
`reconcile(rules_result, proposal)`.

## Implementation status

Implemented in `divesafe.agents.tot`: three scenarios, one level, one validated call each; kind
assigned by code; an invalid scenario is dropped and reported; stored fields are the concise
`Scenario` only. The "scenario C is more severe than the rules" guardrail needs no code: scenarios
have no severity and only feed the risk agent, whose proposal `reconcile` can only tighten.

## Guardrails

Implemented: `Scenario.evidence_ids` must be non-empty and `AssessmentRecord` rejects cited IDs
that are not in the record's evidence. The rest are implemented with the orchestrator.

- A scenario that cites an unknown evidence ID is rejected.
- If scenario C is more severe than the deterministic result on rule-checkable facts, the
  rules engine decides; ToT only explains.
- Identical inputs and a fixed prompt version must produce stored, reproducible scenario
  records (prompt and model versions are recorded).
