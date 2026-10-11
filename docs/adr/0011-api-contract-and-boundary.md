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
- `GET /v1/sites` lists every registered site (viewer role), added for the web UI (ADR 0017).
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

### Retry keys (`Idempotency-Key`)

- Optional on `POST /v1/assessments` and `POST /v1/assessments/{id}/decision`; 16 to 128 characters
  of letters, digits, `.`, `_`, `-`, otherwise 422 and nothing is stored.
- **Create:** the assessment id is derived from the caller and the key (SHA-256, 32 hex), so the
  repository's unique id is what prevents a second record. A repeat with the same plan and decider
  returns the stored record with 200 and `Idempotent-Replay: true`; a repeat with a different plan
  or decider is 422. Keys are per actor. No new storage, no migration. The stored record is
  returned as it was, not re-assessed, so its evidence is the original evidence; for that reason a
  repeat is only honoured within 10 minutes of creation (`REPLAY_WINDOW`), after which it is 409
  and a new assessment (new key) is needed. A caller without the viewer role gets the record as
  it was at creation, with any later decision or actual conditions removed, so a repeat is not a
  way to read them. Two concurrent first requests may both assess before one is stored; the other
  then gets the stored record (duplicate connector work, one record). Do not reuse actor names:
  the id is derived from the actor and the key, and keys are not secret from a viewer.
- **Decision:** a repeat is reported (200, `Idempotent-Replay: true`) only if a well-formed key is
  sent and the record already holds a decision by the same actor with the same outcome and the same
  reason. The key is a flag, not bound to the first request (binding it would need a schema change
  to a write-once record), so the same decision with any key is reported. Anything else, and any
  repeat without a key, is still 409. A decision is never changed or re-recorded.
- The id is not secret; the actor is part of it, so another person's key reaches nothing of yours.

## Consequences

- A frontend can be built against the OpenAPI schema (served outside production).
- Missing evidence is never rendered as safe: it appears as `evidence_issues` and
  `INSUFFICIENT EVIDENCE`.

## Not solved

- Evidence `value` has no size cap of its own (it relies on connector caps); no pagination.
- Per-assessment ownership (roles exist, ADR 0013, but any viewer reads any assessment), pagination and history
  listing, site search, async jobs.
