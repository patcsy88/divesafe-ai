# ADR 0013: Roles and per-actor rate limits

- Status: Accepted
- Date: 2026-10-10

## Decision

- **Three explicit roles that do not imply each other:** `viewer` (read assessments, evidence,
  sites), `assessor` (create assessments), `decider` (record the human decision and actual
  conditions). A key lists its roles; an empty or unknown role list is refused at startup. A
  person who must do several things holds several roles. A `decider` must also be a `viewer`
  (refused at startup otherwise), so nobody decides on evidence they cannot read. Two keys may not
  share an actor name (case-insensitive), so audit attribution and counters stay per person; duplicate
  key hashes are refused. A 403 or 429 says nothing was recorded and the assessment is still pending.
- Config format changed (breaking): `DIVESAFE_API_KEY_HASHES` maps a sha256 digest to
  `{"actor": "...", "roles": [...]}`. The old digest-to-name form is refused with a clear error
  rather than guessed to mean "all roles".
- Dev authentication grants every role (nothing there is authenticated; production refuses it).
- Order of checks: authenticate (401), role (403), rate limit (429). Denied and unauthenticated
  requests are not counted, so they cannot exhaust a real user's allowance.
- **Rate limits** are a per-actor sliding window per class: `create` (10/min), `decide` (30/min),
  `read` (120/min), configurable with `DIVESAFE_RATE_LIMIT_*_PER_MINUTE`. A limited request returns
  429 with `Retry-After` and changes nothing. Expired counters are evicted; in api_key mode counters are bounded by the configured actors,
  but in dev mode (actor from a header) they are not.
- The defaults are operational guesses to protect the upstream Open-Meteo quota and the service,
  **not safety limits**; tune them from real use.

## Not solved

- **One actor may hold assessor and decider** and approve their own assessment; separation of
  duties is not enforced.
- A decider can decide any assessment and decisions are write-once, so a wrong or unqualified
  decider can lock one (including an override to GO) before the right leader acts.
- Bodies are parsed before authentication (FastAPI), so malformed JSON gets 422 before 401.
- Forbidden and unauthenticated requests are not rate limited and each writes a log line.
- Production with no keys configured starts and rejects everything (a warning, not a refusal).
- **No per-assessment ownership.** Any `viewer` reads any assessment; any `decider` may decide any
  assessment. Roles do not tell who is qualified to take a dive decision.
- **Limits are per process.** With several workers each keeps its own counters, so the effective
  limit is multiplied; a shared store (for example PostgreSQL or Redis) is needed first.
- **No limit before authentication**, so unauthenticated floods are bounded only by the body-size
  limit and the host. Put a reverse proxy or gateway in front for per-IP limits.
- No single-flight de-duplication of identical create requests; the connector cache and the
  Open-Meteo quota guard still protect the upstream.
- No key rotation, expiry or revocation beyond editing the configuration and restarting.
