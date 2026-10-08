# ADR 0006: API authentication and the assessment repository

- Status: Accepted (interim)
- Date: 2026-10-09

## Context

Overrides can relax a `NO-GO` (ADR 0004), so the identity recorded on a human decision must be
trustworthy. No database or Docker is available in the current development environment, so a
PostgreSQL adapter cannot yet be tested.

## Decision

**Authentication.** Every endpoint except `/health` requires a principal.
- `api_key` (default): `Authorization: Bearer <key>`; only sha256 hashes are configured
  (`DIVESAFE_API_KEY_HASHES`), compared with `hmac.compare_digest`. No keys configured means
  every request is rejected.
- `dev`: actor taken from `X-Dev-Actor`. Unauthenticated; Settings refuse it in production.
- The actor on a decision or on actual conditions always comes from the principal. Request
  bodies forbid unknown fields, so identity cannot be supplied by a client. The server clock
  supplies all timestamps.

**Repository.** `AssessmentRepository` (create, get, compare-and-set `replace`) is the storage
contract. `check_successor` allows only attaching a human decision once and then actual
conditions once; any other change is rejected. Adapters must pass
`tests/unit/test_repository_contract.py`. Only an in-memory adapter exists; a PostgreSQL adapter
(psycopg, with a unique id and a conditional update) needs its own ADR for the dependency and
must be verified against a real database before use.

**Provider limits.** `CachingRateLimitedGetter` caches responses for 60 s and enforces a
minimum interval per host; a request that would break it fails closed as a connector issue.

## Consequences

- Decisions are attributable to a configured actor, not to claimed free text.
- Records are lost on restart until the PostgreSQL adapter exists. The in-memory adapter is for
  development and tests only.
- API keys give an actor name but no roles; every authenticated actor can create, decide and
  report. Role separation (for example only qualified dive leaders may override) is not built.
- Rate limiting is per process; multiple workers would each allow their own quota.

## Hardening applied after review

- Request bodies are capped at 64 KiB before parsing and before authentication (413). Request
  validation errors return only the error location and message, never the submitted input.
  `observations` is limited in depth (6), node count (500) and size (20,000 characters).
- Only specific, expected failures become 4xx responses with fixed messages. Anything else is a
  logged 500 with the opaque body `{"detail": "internal error"}`.
- In `production` the app refuses to start with the in-memory repository and hides `/docs`,
  `/redoc` and `/openapi.json`. In every environment `/health` reports `storage`, and the app
  logs a warning when storage is non-durable or no decision age limit is set.
- Failed authentication is logged (path and client address only; never headers or keys).
- Actual conditions cannot be reported before the planned dive starts.

## Production gates (not yet met)

1. A tested PostgreSQL adapter (durable, single source of truth across workers).
2. Roles: at minimum a permission for overriding, and ownership or group scoping of records
   (GET by id currently relies on unguessable 128-bit ids only).
3. Per-client and per-IP throttling and a repository size cap (the provider-limit guard protects
   upstream quotas, not the server). Terminate TLS and apply security headers at a proxy.
4. Dev authentication is only blocked when `DIVESAFE_ENVIRONMENT=production`; set the
   environment explicitly in every deployment. API keys must be random 256-bit values; the
   decision record does not yet capture which key was used.
5. Free text (`rationale`, `observations`) is untrusted audit data; it must be delimited and not
   sent to an LLM provider unless needed.
