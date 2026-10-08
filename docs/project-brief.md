# Project brief

## Purpose

DiveSafe AI assesses environmental conditions for a planned recreational dive and produces an
explainable recommendation with its evidence, risk factors and uncertainty.

Recommendations: `GO`, `CAUTION`, `NO-GO`, `INSUFFICIENT EVIDENCE`.

## Inputs

Weather forecasts, wind, waves/swell, ocean currents, tides and tidal currents, sea
temperature, marine warnings, historical observations, dive-site characteristics, local
diving guidance, and the user's dive plan (site, time, duration, depth).

## Principles

- **Human decides.** The system advises; the diver, dive master or dive leader decides.
- **Safety hierarchy.** Deterministic rules > validated data > risk model > agent reasoning >
  LLM explanation. See [architecture.md](architecture.md).
- **No manufactured certainty.** Insufficient or contradictory evidence yields
  `INSUFFICIENT EVIDENCE`.
- **Explainable and auditable.** Every recommendation cites evidence; every decision is recorded.

## Non-goals

- Not an autonomous safety authority, dive computer, or decompression planner.
- Not a replacement for training, local knowledge, briefings or the dive leader's judgment.
- No unrestricted chain-of-thought exposure; reasoning is summarized with evidence references.
- Not a general-purpose chatbot.

## Success criteria (capstone)

1. Correct, auditable enforcement of the safety hierarchy (verified by `tests/safety`).
2. Evidence-grounded recommendations with transparent uncertainty.
3. Complete human-in-the-loop record, including overrides and post-dive actuals.
4. Measurable evaluation (see [evaluation-strategy.md](evaluation-strategy.md)).
5. Switchable LLM provider with identical safety behavior.

## Phases

1. **Foundation** (current): architecture, rules, safety core, skeleton.
2. Data connectors and evidence/provenance service.
3. Rules definitions with cited sources; risk model baseline.
4. RAG and site knowledge.
5. Agents, orchestrator, bounded ToT.
6. Human decision interface, feedback and learning.
7. Evaluation and hardening.
