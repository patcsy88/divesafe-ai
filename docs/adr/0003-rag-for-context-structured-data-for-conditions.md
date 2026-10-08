# ADR 0003: RAG for context, structured connectors for live conditions

- Status: Accepted
- Date: 2026-10-09

## Context

Retrieval over documents is good for site knowledge and guidance but unsuitable for current
conditions, which need typed, timestamped, validated values that rules can check.

## Decision

Live weather, ocean, tide, current and warning data come only from structured connectors
returning validated `EvidenceItem`s. RAG (PostgreSQL + pgvector) serves contextual knowledge:
site characteristics, hazards, regulations, best practice, historical summaries and interpretation
guidance. Retrieved text may interpret conditions but may never supply or override them, and it
is treated as untrusted input.

## Consequences

- Freshness and sufficiency checks apply to structured evidence deterministically.
- Retrieval quality problems cannot silently change live-condition values.
- Two ingestion paths (connectors and document corpus) must be maintained.
