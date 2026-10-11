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
- Order of checks: failed-authentication limit (429), authenticate (401), role (403), rate limit
  (429). Forbidden requests are not counted against the per-actor limits.
- **Failed-authentication limit** (`auth_failure`, `DIVESAFE_RATE_LIMIT_AUTH_FAILURES_PER_MINUTE`,
  default 10), counted per source, not per actor. Each attempt is reserved atomically before the
  key is looked at and given back if the key is right, so a parallel burst cannot pass a check
  that has not yet counted. A source at its limit gets 429 with `Retry-After` and its key is not
  examined, so it learns nothing about a guess. The price: every authenticated user behind the
  same address is refused too while it is blocked (up to a minute). `auth_failure` is deliberately
  not in `LIMIT_KEYS`, so no route can claim it.
- **Source** is the TCP peer, or, if the peer is listed in `DIVESAFE_TRUSTED_PROXIES`, the last
  `X-Forwarded-For` entry. Addresses are normalised (zone id dropped, IPv4-mapped unwrapped, IPv6
  grouped by /64). Run uvicorn with `--no-proxy-headers` (the Dockerfile does) so this is the only
  place that decides. List only proxies you control, never a shared gateway, and make sure the
  proxy overwrites or appends the header and the API is reachable only through it. A schema-invalid
  body with a bad key is still a counted failure, because authentication runs before validation.
- **Rate limits** are a per-actor sliding window per class: `create` (10/min), `decide` (30/min),
  `read` (120/min), configurable with `DIVESAFE_RATE_LIMIT_*_PER_MINUTE`. A limited request returns
  429 with `Retry-After` and changes nothing. Expired counters are evicted; in api_key mode counters are bounded by the configured actors,
  but in dev mode (actor from a header) they are not.
- The defaults are operational guesses to protect the upstream Open-Meteo quota and the service,
  **not safety limits**; tune them from real use.

## Not solved

- **One actor may hold assessor and decider** and approve their own assessment; separation of
  duties is not enforced.
- Superseded by ADR 0014: a decider can now decide only assessments assigned to them.
- Bodies are parsed before authentication (FastAPI), so malformed JSON gets 422 before 401.
- Forbidden requests are not rate limited and each writes a log line. A blocked source logs at
  debug, so a flood does not become log volume.
- Production with no keys configured is now refused at start-up (ADR 0014).
- Reads are not scoped: any `viewer` reads any assessment. Roles do not tell who is qualified to
  take a dive decision.
- **Limits are per process.** With several workers each keeps its own counters, so the effective
  limit is multiplied; a shared store (for example PostgreSQL or Redis) is needed first.
- Only failed authentication is limited before a key is accepted; other unauthenticated floods are
  bounded by the body-size limit and the host. The failure table is capped (oldest unblocked sources
  are shed first), so a flood of distinct sources can still push out other sources' counters.
  Without `DIVESAFE_TRUSTED_PROXIES` behind a proxy or the web server, everyone shares one bucket;
  production logs a warning at start-up. Failures share one table and cap with the other limits,
  so a flood of distinct sources can shed authenticated actors' counters (a separate table is a
  follow-up). Shared office or NAT addresses share a bucket too.
- No single-flight de-duplication of identical create requests; the connector cache and the
  Open-Meteo quota guard still protect the upstream.
- No key rotation, expiry or revocation beyond editing the configuration and restarting.
