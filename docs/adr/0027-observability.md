# ADR 0027: Observability (request ids, metrics, alerting, later tracing)

- Status: PROPOSED (2026-11-12). Not reviewed. Nothing here is built; the owner decisions at the
  end come first.
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

1. **A request id per HTTP request.** The API accepts a well-formed inbound `X-Request-ID`
   (fixed grammar and length) only when the request comes from the trusted reverse proxy
   (ADR 0024: the API binds to loopback behind the operator's TLS terminator; "trusted" is the
   proxy's address, a setting); otherwise it generates one. The id goes on every log line of that
   request and of the run it starts (the background assessment, ADR 0019), and is returned in the
   response header. It is **not stored in the record** unless the owner decides so (it would be a
   new record field, ADR 0012 old-row rule applies).
2. **A `/metrics` endpoint in the Prometheus text format** with a small fixed set: assessments
   created and finished, by final recommendation; runs in flight; connector failures by connector
   name; model call outcomes by a fixed code (`ok`, `provider_error`, `timeout`,
   `invalid_output`); a per-stage duration histogram with fixed buckets; knowledge retrieval
   outcome by fixed code; authentication failures. **No label from user input, no prompt or model
   text, no site free text**: site labels, if any, are the fixed site ids only. Counters are per
   process (as rate limiting is today). Two options for the implementation:
   - **Option A (recommended): write the text format by hand.** A few counters and one histogram
     are a page of code with no dependency; the exposition format is stable and simple.
   - **Option B: `prometheus_client`.** Less code for histograms and multiprocess use, but a new
     dependency, which is what this ADR would be justifying. Choose B only if the reviewers
     prefer it.
   **Authentication (owner decision):** section 12 forbids an open endpoint, so either (a) a
   `metrics` role on an API key (ADR 0013; the scraper holds a key with that role only), or (b)
   bind `/metrics` to a second, loopback-only port that the scraper reaches on the host, with the
   API's main port unchanged. (b) adds no key handling to the scraper; (a) needs no second
   listener. The metrics themselves hold nothing sensitive, but they show activity and failure
   rates, so neither option exposes them to the network.
3. **Alerting stays outside the app.** The app exposes signals; the owner picks the tool
   (Uptime Kuma, a cron job with a mail script, or similar; not chosen here). Minimal recommended
   set: agent-stage failure rate above zero over the last N runs (the incident above); any
   connector failure; `docker/backup.sh` exit status not 0 (it already exits non-zero on every
   refusal, so a cron wrapper can mail on failure); `/health` not answering. Thresholds and N are
   operator choices and are not safety thresholds.
4. **Later and optional: tracing.** OpenTelemetry spans per stage and per model call with
   timings and the fixed codes only, **never prompt or reply text**, sent to a local collector or
   a self-hosted Langfuse on the same machine. LangSmith only through its OpenTelemetry endpoint,
   under the same no-text rule, and only after a `security-reviewer` review of the egress
   (ADR 0022 decision 15 and ADR 0023 apply: a trace that leaves the machine is an egress).
   **Why LangSmith's default mode is not an option:** its SDK instrumentation records prompts and
   completions by default and sends them to a hosted service, which conflicts with AGENTS.md
   section 3 (never log prompts or decision content) and section 12 (only public evidence and
   plan fields leave the machine, and only to the declared model host). Tracing is not needed
   for the incident above; item 2 covers it.

## Consequences

- One new setting for the trusted proxy address (item 1) and one for the metrics port or role
  (item 2); both through `Settings`.
- Item 2 touches `api` and `orchestration` (counters are incremented where the stage trace is
  written); it does not touch `domain`, `risk` or the record.
- A new dependency only under option B, recorded here.
- Item 1 is a contract with the operator's proxy: the proxy must strip or replace an inbound
  `X-Request-ID` it did not set.
- Alerting is operator work and can fail silently itself; the first alert to set up is the
  backup one, since it already has a hook.

## Owner decisions needed

1. Build items 1 and 2 at all for the MVP, and in that order?
2. Item 2 option A (hand-written) or B (`prometheus_client`)?
3. Item 2 authentication: a `metrics` role on a key, or a loopback-only port?
4. Store the request id on the record (new field) or keep it in logs only?
5. Which alerting tool, and the N for the agent-failure rule.
6. Whether tracing (item 4) is wanted at all for the MVP.

## Known gaps

- Until item 2 is built, the only way to see a failed agent stage is the record (as on
  2026-11-12) or the logs.
- Per-process counters reset on restart and do not sum across workers (same as rate limiting).
- Metrics show failure counts, not causes; the cause stays in the logs and the record.
- Item 1 trusts one proxy address; a misconfigured proxy that forwards a client's
  `X-Request-ID` lets a client choose ids (log correlation only; no authority attaches to the id).
- No alert can be checked by a test in this repository; alerting is operator discipline.
- A hand-written exposition (option A) needs its own test against the format, and will lack the
  multiprocess mode of `prometheus_client`.
