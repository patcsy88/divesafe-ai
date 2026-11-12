# ADR 0027: Observability (request ids, metrics, alerting, later tracing)

- Status: ACCEPTED for items 1 to 3 (owner decisions 2026-11-12: `/metrics` behind a `metrics`
  role, `prometheus_client`, Uptime Kuma); item 4 (tracing) stays PROPOSED, later. **Nothing is
  built yet.** The remaining owner decisions at the end are open with a recommended default.
  This ADR is the record for the `prometheus_client` dependency.
- Builds on ADR 0024 (production stack), ADR 0025 (the per-stage trace on the record), ADR 0023
  and ADR 0022 (what may leave the machine), ADR 0013 (roles).

## Context

What exists: JSON logs from the standard library with key-based secret redaction
(`config/logging.py`; no prompts, headers, keys or decision text are logged); the audit record
with its per-stage trace (`stages`, fixed issue codes such as `observe.connector_failed:<name>`,
ADR 0025); `/health`, the one unauthenticated endpoint; the backup scripts' exit status. What is
missing: a request id, metrics, tracing, and any alerting. Nothing watches the logs.

**Motivating incident (2026-11-12).** During the first production knowledge load, the model-host
tunnel returned 404 and every one of the 8 agent calls of assessment `da2d728f...` failed. The
pipeline did what it should (the deterministic result stood, the record listed the failed calls as
agent issues, nothing went to a model), and nobody noticed until the record was read by hand. The
failure was on the host side and needed no DiveSafe change, but a run of such records would mean
every assessment silently lacking the agent stage. The backup script likewise has an exit status
that nothing reads (ADR 0024 "Not solved": no failure alert).

Constraints: AGENTS.md section 3 (never log prompts, headers, keys or free-text decision
content), section 12 (every endpoint but `/health` is authenticated; only public evidence and
plan fields leave the machine), and "no new dependency without an ADR".

## Decision (proposed, in this order)

1. **A request id per HTTP request.** The API accepts an inbound `X-Request-ID` matching
   `[A-Za-z0-9-]{1,64}` only when the request comes from a trusted proxy, judged by the existing
   `DIVESAFE_TRUSTED_PROXIES` setting (`settings.py`, used by `auth.client_source`; no new
   setting); otherwise it generates a UUID4. An invalid or untrusted inbound id is replaced and
   never echoed or logged. The id goes on every log line of that request and is passed
   explicitly into the background run it starts (ADR 0019; the run does not inherit the request
   context), and is returned in the response header. It never goes into a prompt or a metrics
   label. It is **not stored in the record** unless the owner decides so (it would be a new
   record field, ADR 0012 old-row rule applies). Caveat: with the port published on `127.0.0.1`
   every host client arrives from the Docker gateway address, so the proxy cannot be told apart
   from another local process (the same weakness rate limiting has); the impact is low, since
   the id is for log correlation only.
2. **A `/metrics` endpoint in the Prometheus text format** with a small fixed set: assessments
   created and finished, by final recommendation; runs in flight; connector failures by connector
   name; model call outcomes by a fixed code (`ok`, `provider_error`, `timeout`,
   `invalid_output`); a per-stage duration histogram with fixed buckets; knowledge retrieval
   outcome by fixed code; authentication failures as one unlabelled counter. **No label from user
   input, no prompt or model text, no site free text, and never an actor, `key_id`, client address,
   request id or assessment id**: site labels, if any, are the fixed site ids only. Counters are
   per process (as rate limiting is today). Whether a `/metrics` request counts toward rate
   limiting and the auth-failure counter is an owner decision (below). Two options were
   considered:
   - **Option A: write the text format by hand.** A few counters and one histogram are a page
     of code with no dependency; the exposition format is stable and simple. Recommended at
     proposal time; not chosen.
   - **Option B: `prometheus_client`.** Less code for histograms and multiprocess use, but a new
     dependency, which is what this ADR would be justifying.
   **Owner decision 2026-11-12: option B, `prometheus_client`, a new runtime dependency recorded
   here.** What it brings and how it is used: pure Python, no network of its own. Use a
   dedicated `CollectorRegistry`, not the global default, so only the metrics named above are
   exposed; do **not** register the default process, platform and GC collectors unless that is
   reviewed separately. Generate the text with `generate_latest(registry)` inside the
   authenticated route; never `start_http_server` or the library's ASGI/WSGI app, which would be
   an unauthenticated listener. Pinned like every other runtime dependency and subject to the
   same `pip-audit` in CI (ADR 0024).
   **Authentication, owner decision 2026-11-12: (a), a `metrics` role; AGENTS.md section 12
   stays as written, no loopback-port exception.** The options were: (a) a
   `metrics` role on an API key (ADR 0013; the scraper holds a key with that role only), or (b)
   bind `/metrics` to a second, loopback-only port that the scraper reaches on the host, with the
   API's main port unchanged. (b) adds no key handling to the scraper; (a) needs no second
   listener. **But (b) does not satisfy section 12** ("authenticate every endpoint but
   `/health`"): it restricts reach, not identity, so it needs an owner amendment to section 12
   and a change to the safety test that checks every route is authenticated
   (`test_api_roles_and_limits.py`, open paths). And behind Docker publishing, loopback-only
   means the host's `127.0.0.1`, which any local user or process on the host can read. The
   metrics hold nothing sensitive, but they show activity and failure rates, so neither option
   exposes them to the network; only (a) keeps the rule as written, which is why it was chosen.
   The scraper holds its own API key carrying only the `metrics` role (ADR 0013); that role
   grants nothing else.
3. **Alerting stays outside the app.** The app exposes signals. **Owner decision 2026-11-12:
   Uptime Kuma, self-hosted.** Minimal set: agent-stage failure rate above zero over the last N
   runs (the incident above); any connector failure; `docker/backup.sh` exit status not 0;
   `/health` not answering. Thresholds and N are operator choices and are not safety thresholds.
   How Uptime Kuma fits:
   - `/health`: an HTTP status (or keyword) monitor.
   - Backups: a push monitor. The cron wrapper around `docker/backup.sh` calls the push URL only
     on exit 0; a missing push within the expected interval is the alert (the script already
     exits non-zero on every refusal).
   - Metrics: Uptime Kuma cannot evaluate PromQL rates, so the agent-failure and
     connector-failure alerts need either a keyword or JSON check on a small authenticated
     endpoint, or a Prometheus later. **Open point, not decided here**; nothing is invented for
     it. Uptime Kuma holds its own API key with only the `metrics` role (above).
   - Alert messages carry metric names and values only, never record or decision text.
4. **Later and optional: tracing.** OpenTelemetry spans per stage and per model call with
   timings and the fixed codes only, **never prompt or reply text**, sent to a local collector or
   a self-hosted Langfuse on the same machine. Rules for any build of it:
   - A trace exporter is a **new egress destination**. ADR 0022 decision 15 and ADR 0023 allow
     only the declared model host to receive anything, so they do not cover it: it needs its own
     opt-in setting and its own https host allow-list, and a `security-reviewer` review. Span
     metadata alone (model name, timings, site ids, evidence ids) leaving the machine is new
     egress.
   - **No auto-instrumentation** (the FastAPI, urllib and httpx instrumentors record URLs, query
     strings and possibly headers): manual spans with fixed attribute keys only.
   - **No exception text on spans**: `record_exception=False` and no status descriptions, since
     ADR 0025 keeps provider error text out of the stage trace.
   LangSmith only through its OpenTelemetry endpoint, under the same rules. **Why LangSmith's
   default mode is not an option:** its SDK instrumentation records prompts and completions by
   default and sends them to a hosted service, which conflicts with AGENTS.md section 3 (never
   log prompts or decision content) and section 12 (only public evidence and plan fields leave
   the machine), and with ADR 0022 decision 15 and ADR 0023 (only the declared model host may
   receive anything). Tracing is not needed for the incident above; item 2 covers it.

## Consequences

- Item 1 reuses `DIVESAFE_TRUSTED_PROXIES`; item 2 adds the `metrics` role to ADR 0013's role
  set (a key with only that role can read `/metrics` and nothing else) and no new port. Item 4
  would add an opt-in setting and a host allow-list for the exporter.
- Item 2 touches `api` and `orchestration` (counters are incremented where the stage trace is
  written); it does not touch `domain`, `risk` or the record.
- New dependency: `prometheus_client` (decided 2026-11-12; pinned, `pip-audit`). The
  OpenTelemetry SDK and exporter, and Langfuse if chosen, would come with item 4 and are not
  added before the owner decides. The all-routes-authenticated safety test keeps `/metrics` on
  the authenticated side; a new safety test should prove a key without `metrics` is refused
  there and that a `metrics`-only key is refused everywhere else.
- Option (b) is rejected: it would have needed an owner amendment to AGENTS.md section 12 and a
  change to the all-routes-authenticated safety test.
- Uptime Kuma is a separate self-hosted service with its own update and backup duties (operator
  work, outside this repository); it stores an API key, so it must be treated as a secret store.
- Item 1 is a contract with the operator's proxy: the proxy must strip or replace an inbound
  `X-Request-ID` it did not set.
- Alerting is operator work and can fail silently itself; the first alert to set up is the
  backup one, since it already has a hook.

## Owner decisions

Settled 2026-11-12:

1. ~~Build items 1 and 2 at all for the MVP, and in that order?~~ Yes, in that order (items 1
   to 3 accepted).
2. ~~Option A or B?~~ B, `prometheus_client`.
3. ~~`metrics` role or loopback-only port?~~ The `metrics` role; section 12 unchanged.
5. ~~Which alerting tool?~~ Uptime Kuma. The N for the agent-failure rule is still open (see 5
   below).
8. ~~Amend section 12 for (b)?~~ Not needed; (b) rejected.

Still open, with the recommended default:

4. Store the request id on the record (new field) or keep it in logs only? **Default: logs only**
   (no record field, no migration; a log search by id finds the run).
5. The N for the agent-failure rule, and how the metric-based alerts reach Uptime Kuma (keyword
   or JSON check on a small authenticated endpoint, or a later Prometheus). **Default: start with
   the `/health` and backup monitors, which need no metrics; add the metric alerts once the
   check mechanism is chosen.**
6. Whether tracing (item 4) is wanted at all for the MVP. **Default: no**; item 4 stays
   proposed.
7. Whether `/metrics` requests count toward rate limiting and the auth-failure counter.
   **Default: yes to both** (one scrape a minute is far below the limit, and a failed scrape
   auth is a real auth failure); the scraper's key must then never be rate-limited out by
   another client sharing its address.

## Known gaps

- Until item 2 is built, the only way to see a failed agent stage is the record (as on
  2026-11-12) or the logs.
- Per-process counters reset on restart and do not sum across workers (same as rate limiting).
- Metrics show failure counts, not causes; the cause stays in the logs and the record.
- Item 1 trusts the proxy addresses in `DIVESAFE_TRUSTED_PROXIES`; a misconfigured proxy that
  forwards a client's `X-Request-ID` lets a client choose ids, and behind a `127.0.0.1`-published
  port every local process looks like the proxy (log correlation only; no authority attaches to
  the id).
- No alert can be checked by a test in this repository; alerting is operator discipline.
- `prometheus_client`'s multiprocess mode is not used; with more than one worker each process
  exposes its own counters and a scrape sees whichever answers (same as the per-process rate
  limit). One worker is the production setting today (ADR 0024).
- Uptime Kuma cannot evaluate metric rates, so until the open point in item 3 is settled the
  two failure alerts that motivated this ADR are not wired; only `/health` and backup are.
- A `metrics`-only key that leaks lets the holder read activity and failure rates; rotation is
  manual, as for every key (ADR 0013).
