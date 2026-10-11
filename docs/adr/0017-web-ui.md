# ADR 0017: Web UI (Next.js)

- Status: Accepted
- Date: 2026-10-11

## Context

ADR 0011 planned a web interface that depends only on the `/v1` API. The API takes the actor from
credentials, so the UI must not collapse people into one identity.

## Decision

- `web/` is a Next.js 16 + React 19 + TypeScript app with no other runtime dependency. Versions are
  pinned exactly; `npm audit --omit=dev` reported none at the time.
- **Sign-in is a personal API key.** The Next.js server seals it with AES-256-GCM (key derived by
  HKDF from `DIVESAFE_WEB_SESSION_SECRET`, at least 32 characters) into an httpOnly, SameSite=Strict
  cookie (Secure in production) that expires after 8 hours. Browser code never sees the key. The API
  still checks the key on every call; the cookie grants nothing by itself.
- Server actions and server components make every API call. `DIVESAFE_API_URL` must be https, or
  http on loopback. Redirects are refused, calls time out, and error text is mapped to fixed messages. Only 422 validation text is shown (truncated, plain text).
- **No risk logic.** The UI shows `final_recommendation` as returned. An unrecognised value is shown
  as INSUFFICIENT EVIDENCE, never as safe. PENDING HUMAN is shown prominently. Model-written text is
  rendered as plain text inside a box labelled unverified. Times need an explicit zone.
- A CSP, frame denial, `nosniff`, no referrer and `no-store` are set on every response.
- `web/tests/boundary.test.ts` fails if a client component imports a server module or reads the
  environment, if `NEXT_PUBLIC` is used, if provider names or limit-like comparisons appear, or if
  the code logs or injects HTML. Seven guards were mutation-checked in an earlier pass.

## Pages

Sign in; sites list (`GET /v1/sites`, added to the API, viewer role); run an assessment; the result
with the decision form (assigned decider only, enforced by the API); the evidence view; the
actual-conditions form (shown once the planned start has passed).

- **The UI never decides what an override is.** The decision form lists outcomes most severe first,
  preselects nothing, and shows a prominent warning when the choice is less severe than the system's.
  That comparison is display only. The API derives and records the override, and requires a reason.
- The form shows the system recommendation, rules alone, the count of source issues and unevaluated
  factors, and links to the evidence before the choice.
- On a 409 from a write, the action goes to the record only after a fresh read shows it was recorded.
- Evidence items are labelled by `data_kind`: only an observation is "measured". Notices and
  knowledge are "text, not a measurement". Quality tags are not sufficiency, and the page says so.
- Actual conditions are free reports. Nothing is compared to a limit. Sea temperature may be
  negative; other values may not.

## Review changes (security-reviewer, 2026-10-11)

Fixed: a revoked key clears the session via `/logout`; unknown status is treated as pending; a
recorded human decision, override flag and rationale are shown; failed writes with an unknown
outcome no longer say "nothing was recorded"; control and bidi characters are stripped from
third-party text; the id pattern matches the API's; extra headers, `object-src 'none'` and
`images.unoptimized`.

## Retry keys and confirmation

- The forms send an `Idempotency-Key` (a random 32-hex value made in the browser after first
  render, new whenever any field changes or the API answers 409). Without `crypto` no key is sent.
  A decision that differs from the one on record is not treated as recorded: the person is told
  theirs was not recorded. See ADR 0011 for the API behaviour.
- The decision form shows the outcome, the system's recommendation and the reason, and records
  only on a second, explicit "Confirm and record" press. Editing anything returns to the review.

## Known gaps

- **Login throttle is per instance.** The login action allows 5 rejected keys a minute per source
  (in memory, one instance; replicas multiply it, restarts reset it), below the API's 10, and the
  API counts failures too (ADR 0013). Each attempt is reserved before the network call. Behind this
  server the API sees one address unless the web server is listed in `DIVESAFE_TRUSTED_PROXIES`
  and `DIVESAFE_WEB_TRUST_PROXY=1` is set; without both, all sign-ins share one bucket and one
  person can block everyone for a minute (nothing is recorded; assessments stay pending). With
  `TRUST_PROXY=1` the web app must be reachable only through a proxy that overwrites or appends
  `X-Forwarded-For`. Only sign-in forwards the address, so a 401 elsewhere (a stale session) counts
  against the web server's own bucket when it is not trusted. Reservations of in-flight successful
  sign-ins count briefly, so five simultaneous sign-ins from one bucket can see a 429 (nothing is
  recorded). A late release can remove another attempt's hit when the API timeout (120 s) exceeds
  the 60 s window. `login` is a `"use server"` action with no direct test; the library is tested.
  API keys must be generated (at least 128 bits), as the hashes are unsalted.
- A retry key covers an unknown-outcome retry only while the form is unchanged and the page stays open; reloading starts a new attempt, so a timed-out create can still be duplicated after a reload. Check the record list before retrying.
- No "who am I" endpoint, so the UI cannot hide actions a role cannot perform. Sign-in calls `GET /v1/sites`
  and accepts a 403 only when the API's own refusal text is present.
- The cookie is the equivalent of the key for 8 hours. It cannot be revoked server-side; revoke the
  key at the API. One leaked session secret decrypts every stolen cookie; there is no rotation or
  version byte, and only length is checked.
- The CSP still allows inline scripts. A per-request nonce with `strict-dynamic` via `proxy.ts` is
  feasible (every page is dynamic) and not done. No HSTS, no `__Host-` cookie prefix, and `Secure`
  follows `NODE_ENV`. Behind a proxy, `serverActions.allowedOrigins` must be set; Next lets a
  request without an `Origin` header through.
- API responses are cast, not validated, and not size-capped.
- The boundary test is a tripwire: it misses extension-less imports, transitive imports and many
  sinks. No tests for headers, cookie attributes or the action origin check.
- CI (`web` job: `npm ci`, `npm audit --omit=dev`, typecheck, tests, build on Node 24) has not run on GitHub yet. A new advisory can fail an unrelated change. Not yet run in a container or behind a proxy. Tested against a
  fixture-backed API only, not a deployed one.

## Review changes (risk and security review of the decision pages, 2026-10-11)

Fixed: notices were labelled "measured"; the less-severe override is now flagged on the form and the
decision card; the form shows context before the choice; the conditions form waits for the planned
start; echoed 409 and 422 text is stripped of control and bidi characters; sign-in no longer trusts a
bare 403 (e.g. from a proxy); timeout wording no longer tells a decider to "check for a new
assessment"; the repository scan exclusion is anchored to `web/node_modules` and `web/.next`.

Gaps added:
- A form loses its typed text after an error. The decision form now has a review-and-confirm step
  (display only; the API still derives the override and demands the reason). Safety rests on the
  API's write-once compare-and-swap, not on the confirm step.
- Responses are cast, not validated, and not size-capped; evidence values are truncated to 20,000
  characters on screen only. One malformed item could still crash a page.
- The backend `observations` object is open-keyed and unbounded in value; bidi and C1 characters are
  rejected by the UI but stored raw if sent to the API directly. `notes` is untrusted human text and
  must be delimited if it is ever used in a prompt, the corpus or history.
- The evidence page does not show the planned dive window against each item's validity.
- The web actions themselves have no tests (ID tampering, a 401 mid-write). Only the library is tested.
