# ADR 0011: API contract and the UI boundary

- Status: Accepted
- Date: 2026-10-10

## Context

A web interface (likely Next.js) is planned after the core is stable. It must depend only on the
HTTP API, never on agents, rules or the RAG pipeline, and must not duplicate risk logic.

## Decision

- The contract is the `/v1` API (authenticated except `/health`). The prefix stays `/v1`, not
  `/api/v1`: a reverse proxy can add a path prefix without a code change.
- Added read-only endpoints: `GET /v1/assessments/{id}/evidence` (items with provenance, lineage,
  the engine's effective quality and age at assessment, plus source issues) and
  `GET /v1/sites/{id}` (the site as registered; no hazards are held, and the response says absence
  of a listed hazard is not absence of hazards).
- Not added yet: `GET /v1/sites/search` (one registered site; search needs verified site data) and
  an asynchronous job model with `assessment_status` (assessments run inline; add it when a real
  LLM adapter makes them slow).
- Responses keep the recommendation (`GO`, `CAUTION`, `NO-GO`, `INSUFFICIENT EVIDENCE`) distinct
  from evidence quality. There is no `risk_level` and no `confidence` label: `confidence` stays
  null until a documented method exists, and a risk level needs definitions from the dive
  professional.
- The browser never talks to a model provider; LLM credentials stay server-side.
- LangGraph is not adopted: no capability need yet (checkpointing, retries, long jobs). Revisit
  with an ADR when one appears.

- The evidence view lists only what was retrieved and says so (`evidence_note`); it also returns
  `evidence_issues` and `unevaluated_factors`. `effective_quality` is the quality an item earned
  from its own checks, not a statement that it is fresh or covers the dive window. Age is since
  retrieval at assessment time; future-dated items are flagged, not hidden.
- Third-party text (`value`, warning text) must be rendered by clients as plain text only.
- `/v1/*` responses carry `Cache-Control: no-store`.

## Consequences

- A frontend can be built against the OpenAPI schema (served outside production).
- Missing evidence is never rendered as safe: it appears as `evidence_issues` and
  `INSUFFICIENT EVIDENCE`.

## Not solved

- Evidence `value` has no size cap of its own (it relies on connector caps); no pagination.
- Roles/authorization (any authenticated actor can read any assessment), pagination and history
  listing, site search, async jobs.
