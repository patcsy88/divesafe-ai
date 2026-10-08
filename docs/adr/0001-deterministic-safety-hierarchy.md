# ADR 0001: Deterministic safety hierarchy

- Status: Accepted
- Date: 2026-10-09

## Context

An LLM-driven system can sound confident while being wrong. Dive recommendations can
influence real-world safety decisions, so model output must not be able to weaken a safety
constraint.

## Decision

Rank layers: deterministic rules > validated data > risk model > agent reasoning > LLM
explanation. Order severity as `GO < CAUTION < INSUFFICIENT EVIDENCE < NO-GO`. The final
recommendation is produced only by `reconcile(deterministic, proposed)`, which returns the more
severe input. Missing or stale required evidence produces `INSUFFICIENT EVIDENCE`. The engine
ships no numeric thresholds; rules are versioned and sourced.

## Consequences

- The system can be more conservative than a human expects; humans can override with a
  recorded rationale.
- Agents add explanation and caution, not permission.
- Safety behavior is testable without any LLM (`tests/safety`).
- A reviewed ruleset is required before useful `GO` outcomes can be produced.

## Alternatives rejected

- LLM as final arbiter with rules as hints: unsafe.
- Weighted score blending rules and LLM opinion: lets a confident LLM dilute a hard constraint.
