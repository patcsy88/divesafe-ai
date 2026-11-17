# ADR 0027: Observability (request ids, metrics, alerting, tracing)

- Status: ACCEPTED for items 1 to 3 (owner decisions 2026-11-12: `/metrics` behind a `metrics`
  role, `prometheus_client`, Uptime Kuma). Item 4 is ACCEPTED as amended on 2026-11-15:
  self-hosted Langfuse for tracing, evaluation, a read-only prompt mirror and cost reporting
  (see "Amendment 2026-11-15"). **Nothing is built yet.** The remaining owner decisions were
  settled on 2026-11-15, each as its recommended default (see "Owner decisions"); the
  metric-alert mechanism is deferred, not chosen. This ADR is the record for the
  `prometheus_client` and OpenTelemetry runtime dependencies and the `langfuse` development
  dependency (`[eval]` extra only).
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
   `DIVESAFE_TRUSTED_PROXIES` setting (`settings.py`, used by `auth.client_source`; no new setting);
   otherwise it generates a UUID4. An invalid or untrusted inbound id is replaced and never echoed
   or logged. The id goes on every log line of that request and is passed explicitly into the
   background run it starts (ADR 0019; the run does not inherit the request context), and is
   returned in the response header. It never goes into a prompt or a metrics label. It goes on
   the run span only (Amendment 2026-11-15), never on a stage or model-call span. It is **not
   stored in the record** (owner decision 2026-11-15: logs only; a record field would be new, and
   the ADR 0012 old-row rule would apply). Caveat: with the port published on `127.0.0.1` every host
   client arrives from the Docker gateway address, so the proxy cannot be told apart from another
   local process (the same weakness rate limiting has); the impact is low, since the id is for log
   correlation only.

   Built (2026-11-17, branch `request-id`): `api/request_id.py` (outermost middleware),
   `config/logging.py` (the bound id and the formatter field), the run manager (the id passed in
   and bound first in the run's task, guarded so it can never change the run), the run span
   (`request_id`, only when valid). The 500 handler binds the id again and returns the header.
   Connector worker threads get a fresh context holding only the id, never the run's trace
   collector. Pinned by `tests/safety/test_request_id.py` and the whole-export pin in
   `tests/safety/test_tracing_export.py`.

   Deployed (2026-11-17): main `7856847`, image `1beef6c3bb80`, `DIVESAFE_TRUSTED_PROXIES` empty.
   Checked on one production assessment: every log line of the request and its run carried the
   generated id, which was also returned in `X-Request-ID`. Only the run span had `request_id`;
   no stage or model-call span had it, and the stored record did not contain it.
2. **A `/metrics` endpoint in the Prometheus text format** with a small fixed set: assessments
   created and finished, by final recommendation; runs in flight; connector failures by connector
   name; model call outcomes by the nine `CALL_OUTCOMES` labels (`agents/runner.py`), the same set
   as the "model call finished" line and the spans; a per-stage duration histogram with fixed
   buckets; knowledge retrieval outcome by fixed code; trace export failures and dropped
   collector entries by their fixed reasons (`timeout`, `connection`, `http_status`,
   `queue_full`, `collector_full`; Amendment 2026-11-15); authentication failures as one
   unlabelled counter. **No label from user input,
   no prompt or model text, no site free text, and never an actor, `key_id`, client address,
   request id or assessment id**: site labels, if any, are the fixed site ids only. Counters are
   per process (as rate limiting is today). A `/metrics` request counts toward rate limiting and
   the auth-failure counter (owner decision 2026-11-15, decision 7 below). Two options were
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
   reviewed separately. The authenticated route returns `generate_latest(registry)` with
   `CONTENT_TYPE_LATEST` and `Cache-Control: no-store`; no OpenMetrics content negotiation and no
   exemplars unless reviewed. Never `start_http_server`, `make_asgi_app` or `make_wsgi_app`,
   which would be an unauthenticated listener, and never the global `REGISTRY`; a safety test
   (AST or grep pin) refuses those four names in `src`. `PROMETHEUS_MULTIPROC_DIR` stays unset
   (no multiprocess mode). Label names are fixed at metric definition; label values come only
   from code enums (`StageName`, the connector names, the outcome codes), never from
   runtime-built strings or exception text. Dependency terms: a bounded range `>=X,<Y` in
   `pyproject.toml` like the other runtime dependencies (the range chosen at build; a lock file
   with hashes is still open, ADR 0024 supply chain) and the same `pip-audit` in CI. Licence:
   Apache-2.0 AND BSD-2-Clause (0.26.0 package metadata, checked 2026-11-17).
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
   grants nothing else. **The key file enforces "only":** roles do not imply each other, so a
   `metrics`-only key already gets 403 elsewhere, but nothing would stop `metrics` being added
   to a decider or admin key that then sits in the scraper, and `groups_of` unions groups across
   keys that share an actor name (used for the read scope), so a metrics key reusing a person's
   actor would widen that scope. Therefore key-hash loading **refuses** (a) `metrics` together
   with any other role, (b) `metrics` with `groups`, and (c) a `metrics` key whose actor name is
   also used by a non-metrics key (precedent: the viewer-without-groups start-up check in
   `api/auth.py`). The `DevAuthenticator` grants every role including `metrics`; that is fine,
   since it cannot run in production. **MUST, in `tests/safety/`:** a key without `metrics` gets
   403 on `/metrics`; a `metrics`-only key gets 403 on every other route; a mixed key file is
   refused at start-up.
   **Network path and the limiter.** Uptime Kuma runs in the Mac host's Docker and reaches the
   API at `http://host.docker.internal:8000` (the port published on the host's `127.0.0.1:8000`;
   to verify on the owner's Docker Desktop at build time). It does not join the compose network
   (a bearer token over the bridge, and the limiter would see its container address) and is not
   exposed beyond loopback. The auth-failure limit is counted per source **before** the key is
   checked; behind Docker publishing every local client shares the gateway address, so a client
   with a bad key can block the scraper, and code cannot exempt a key it has not checked.
   Uptime Kuma must therefore treat a 429 as its own alert ("auth blocked"), not as "down". The
   rate-limit key: `LIMIT_KEYS` is a fixed set; **owner decision 2026-11-15 (decision 7):** add a
   `metrics` entry with its own setting, rather than reusing `read`.

   **Built and deployed (2026-11-17):** `api/metrics.py`, with
   `prometheus_client>=0.26,<0.27`.
   - Owner decisions (2026-11-17): counts are taken in the API layer from the stored record and
     the run's trace collector, after the result or failure code is stored; nothing is hooked
     into orchestration. A run that fails before its record is stored is counted only by its
     failure code and model calls. The rate limit is `DIVESAFE_RATE_LIMIT_METRICS_PER_MINUTE`,
     default 30. The histogram buckets are 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120 and 300 s.
   - The metrics: `divesafe_assessments_created_total`,
     `divesafe_assessments_finished_total{outcome}`,
     `divesafe_stage_duration_seconds{stage,outcome}`,
     `divesafe_connector_failures_total{connector,kind}`,
     `divesafe_knowledge_retrieval_total{outcome}`, `divesafe_model_calls_total{outcome}`,
     `divesafe_auth_failures_total`, `divesafe_runs_in_flight` and
     `divesafe_trace_dropped_total{reason}`.
   - Labels: a connector that is not configured is labelled `other`; model-call outcomes outside
     `CALL_OUTCOMES` are dropped.
   - Start-up refuses any `PROMETHEUS_*` environment variable, in any letter case, and a
     `metrics` key that also has another role or any groups. Actor names were already unique,
     which covers rule (c).
   - The run's collector now runs whenever the app runs, with tracing off too. It is held in
     memory only; nothing is exported unless tracing is on.
   - `divesafe_assessments_finished_total` counts the system's final recommendation at storage,
     before any human decision. A count of final recommendations is operational only: never a
     target, never an alert on a GO or NO-GO rate, and never a reason to change a rule,
     threshold, prompt or data check (as AGENTS.md section 4 says for scores). A rise in
     INSUFFICIENT EVIDENCE is worth alerting on as a data-source signal, not as a quality score.
   - Pinned by `tests/safety/test_metrics.py`.

   Deployed (2026-11-17): main `cc32583`, image `e974d495a4f7`, with one scraper key whose only
   role is `metrics`. That image's compose file did not yet pass
   `DIVESAFE_RATE_LIMIT_METRICS_PER_MINUTE`, so the Settings default of 30 applied; the
   follow-up adds it to the compose file with the same default. Checked in production: a request
   without a key gets 401 on `/metrics`, the scraper key gets 200 with all nine families and 403
   on `/v1/sites`, and no `PROMETHEUS_*` variable is set.
3. **Alerting stays outside the app.** The app exposes signals. **Owner decision 2026-11-12:
   Uptime Kuma, self-hosted.** Minimal set: agent-stage failure rate above zero over the last N
   runs (the incident above); any connector failure; `docker/backup.sh` exit status not 0;
   `/health` not answering. Thresholds and N are operator choices and are not safety thresholds.
   How Uptime Kuma fits:
   - `/health`: an HTTP status (or keyword) monitor.
   - Backups: a push monitor. The cron wrapper around `docker/backup.sh` calls the push URL only
     on exit 0; a missing push within the expected interval is the alert (the script already
     exits non-zero on every refusal). The push URL is a bearer secret (whoever holds it can
     fake a successful backup): a mode-600 file outside the repository, read by `curl -K <file>`
     (or stdin), never on the command line (`ps`, a committed cron wrapper), never logged; no
     backup output in the `msg=` parameter.
   - Metrics: Uptime Kuma cannot evaluate PromQL rates, so the agent-failure and connector-failure
     alerts need either a keyword or JSON check on a small authenticated endpoint, or a Prometheus
     later. **Deferred (owner decision 2026-11-15):** the `/health` and backup monitors come first;
     the mechanism and N are chosen once `/metrics` is built. Nothing is invented for it. Uptime
     Kuma holds its own API key with only the `metrics` role (above), sent in an `Authorization`
     header (monitor headers), never in a URL. A keyword or JSON monitor's failure message can
     include part of the response body, so the checked endpoint must return only names and values.
   - Alert messages carry metric names and values only, never record or decision text.
     Notifications (email, Telegram or similar) are a new egress of that alert text.
   - Uptime Kuma itself stores its secrets in plain text in its database: its data volume and
     its backups get the same care as `docker/secrets/` (outside synced folders) and stay out of
     DiveSafe's backups. Its web UI is published on loopback only, with its own admin password
     and 2FA if available. It is a new external dependency: image pinned by digest, updated by
     the owner.
4. **Later and optional: tracing.** OpenTelemetry spans per stage and per model call with
   timings and the fixed codes only, **never prompt or reply text**, sent to a self-hosted
   Langfuse on the same machine (Amendment 2026-11-15, which tightens these rules and states
   their exceptions: plain http on an internal Docker network, and plain http on loopback for
   development evaluation). Rules for any build of it:
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

## Amendment 2026-11-15: Langfuse for tracing, evaluation, prompts and cost

Owner decision: use self-hosted Langfuse (MIT core; no `ee/` feature, no licence key) for
tracing, evaluation, a read-only prompt mirror and cost reporting. It runs on the production Mac
in its own Docker Compose project, never in Langfuse's cloud. LangSmith was considered and not
chosen: closed source, self-hosting only on an Enterprise contract with Kubernetes, and its
value lies in LangChain integration, which DiveSafe does not use (ADR 0025).

### Rules for every use

- **One destination, a fixed address, enforced at connect time.** The API sends spans to one
  endpoint, `DIVESAFE_TRACE_ENDPOINT`, only when `DIVESAFE_TRACING_ENABLED=true`; either without
  the other refuses start-up.
  - **The network.** The endpoint is Langfuse's web service at a fixed IP address on a dedicated
    Docker network, `divesafe-trace`: `internal: true`, a fixed `/24` `ipam` subnet, a fixed
    `ipv4_address` for the web container, joined only by the API container and Langfuse's web
    container. DiveSafe's compose project creates the network (`name: divesafe-trace`); Langfuse's
    compose refers to it as `external: true`. After every compose change the operator checks that
    `docker network inspect divesafe-trace` shows `"Internal": true` and that `Containers` lists
    exactly the API container and Langfuse's web container (the latter at the fixed address). The
    subnet is chosen so it does not overlap the LAN, a VPN or Docker Desktop's own ranges, and the
    `ipam` config sets an `ip_range` that excludes the web container's fixed address, so no other
    container is ever given it.
  - **The value.** `DIVESAFE_TRACE_ENDPOINT` must be exactly
    `http://<that address>:<web port>/api/public/otel/v1/traces`, where the address is an IPv4
    literal in a private range. Start-up refuses any other value, including a host name (so no
    DNS lookup is ever made), credentials, a query or a fragment, and never echoes the value.
  - **The subnet check.** At start-up, and only when `DIVESAFE_TRACING_ENABLED=true` and the
    endpoint has passed the value check, the API checks that it has its own interface in the `/24`
    that holds that address: it connects a UDP socket to the address (no packet is sent) and
    compares `getsockname()`. Otherwise start-up refuses, so a container that is not on
    `divesafe-trace` never sends spans through its default route. A failed `connect` (an error
    number or an `OSError`) is a refusal with the same fixed message.
  - **Tracing off.** With tracing off, no socket is created, no tracer provider or exporter is
    built, and the only tracing-related refusals are an endpoint set without the flag and an
    unusable `divesafe_trace_*` secret file (the general ADR 0024 secret-file rule); the keys'
    presence and shape are not checked. `OTEL_*`
    variables are refused only when tracing is enabled: with tracing off no OpenTelemetry SDK
    object exists, so they have no effect, and an unused feature never stops the service.
  - **Plain http** is allowed to that address only, because it is on an `internal: true` Docker
    network, which has no route out of Docker (its bridge address, `.1`, is refused as an
    endpoint), so the traffic cannot leave Docker. This is the first of two exceptions to item 4's
    https rule and to AGENTS.md section 12's "https for outbound calls" (the second is
    development evaluation on loopback, section 2 below).
  - **The HTTP session.** The exporter gets a `requests.Session` with `trust_env=False` and
    `max_redirects=0`, passed as its `session=` (verified at build, step 3), like the model adapter
    (`ProxyHandler({})`, `_NoRedirect`). Only `http://` is mounted, with
    `HTTPAdapter(max_retries=0)`; the session refuses anything but a `POST` to the exact endpoint
    and forces `allow_redirects=False`. Every 3xx reply counts as `http_status` (with
    `max_redirects=0` requests raises `TooManyRedirects`; no second request is sent); a failure the
    session never saw (encoding, the size cap) counts as `connection`. Headers are exactly
    `Authorization` and `x-langfuse-ingestion-version: 4` besides the library's own. Endpoint,
    headers, timeout and processor limits are passed explicitly; when tracing is enabled,
    start-up refuses while any `OTEL_*` variable is set, so the environment cannot change where
    spans go, what they hold or the queue limits.
  - **Fixed bounds** (step 3): export timeout 3 s, queue 512 spans, batch 128, schedule delay
    2 s, request size 4 MiB, shutdown wait 4 s, attribute length 128 characters. A reservation
    (`_Pending`) capped at the queue size is taken per run before any span is made, so a run
    whose spans do not fit is dropped whole and counted `queue_full`, never partly exported, and
    the SDK's own queue (which drops the oldest silently) never overflows.
  - **FastAPI's own telemetry is off** (step 3). FastAPI 0.142+ has native OpenTelemetry: it
    requires and imports `opentelemetry-api`, records request spans, validation input and
    exception text once a global provider is set, and from 0.143 adds exporters from `OTEL_*`
    variables only on opt-in (`FASTAPI_OTEL_AUTO_CONFIGURE=true`). `create_app` passes
    `telemetry=FASTAPI_TELEMETRY_OFF` (tracing, metrics, logs, operation spans and
    auto-configuration all off, whatever the environment), and the dependency floor is
    `fastapi>=0.143,<1`.
  - **A private provider.** The API builds its own `TracerProvider` instance and takes tracers
    only from it. It never calls `trace.set_tracer_provider` or `trace.get_tracer_provider`, so
    no other library's spans (an instrumented library, or the `langfuse` SDK, which puts prompts
    in attributes) can reach the exporter.
  - For the API, `host.docker.internal`, a host port or a remote Langfuse needs a new ADR
    (evaluation from the owner's shell uses `127.0.0.1`, section 2).
  - **New destination.** Besides the model host, the connectors and DiveSafe's own PostgreSQL,
    Langfuse is the API's only destination of run data. This amends ADR 0022 decision 15 and
    ADR 0023 (which allowed only the model host) for span metadata only, never a prompt.
  - **Keys.** The production project's public and secret key are secret files under
    `DIVESAFE_SECRETS_DIR` (ADR 0024), read into `SecretStr` settings, sent only in the
    exporter's `Authorization` header, never logged. With tracing enabled, start-up refuses
    unless both are set and each matches `[A-Za-z0-9._-]{1,256}` (a fixed message), which keeps
    CR, LF and `:` out of the header.
- **Two Langfuse projects.** `divesafe-prod` holds production spans only; its keys exist only in
  the API container. `divesafe-eval` holds evaluation runs and the prompt mirror; its keys exist
  only as files in the directory named by `DIVESAFE_EVAL_SECRETS_DIR` on the owner's
  development machine. Langfuse keys are project-wide, so a production key never reaches
  evaluation content.
- **Not part of the result.** Tracing never changes an outcome, a refusal, a stage deadline or
  what is stored:
  - **Collected during the run.** One collector per run holds plain values only: the "model
    call finished" fields (the same dict the log line uses), a wall-clock start per model call
    (`time.time_ns()`, for the span only; the record's times still come from the run's clock),
    and, from each `StageTrace` the pipeline already builds, only its name, outcome, start,
    finish and duration (never its evidence ids or issue codes), so nothing a stage span may not
    carry ever reaches a sink. It lives in `divesafe.observability.collector`.
    - **Where it lives:** a `ContextVar` set inside `RunManager._wrapped`, in the run's own
      task, never in the request handler and never module-level state. Its default is `None`,
      so `assess_dive` called directly (tests, the evaluation harness) or with tracing off
      collects nothing.
    - **Child tasks:** `gather` tasks and the Retrieve task copy the context and share the one
      collector. Appends happen only on the event-loop thread.
    - **Bound:** at most `MAX_TRACE_ENTRIES` entries, a fixed constant at least as large as one
      run's model calls plus its stages. Extra entries are dropped and counted in the same
      in-process counter as export failures.
    - **Suppression:** each append runs under `contextlib.suppress(Exception)`: in
      `call_structured` inside `_log_call`'s existing suppression; in the pipeline a new
      suppression beside `_record_trace`'s append to `run.traces`.
    - Nothing in `domain` or `risk` touches the collector. No span is open during a stage, and
      no span context manager wraps an `await`.
  - **Emitted in one place, last.** Spans are built, with explicit start and end times, and
    handed off in `RunManager._wrapped`'s `finally`, after `complete` has returned or
    `_mark_failed` has run.
    - The emission is synchronous (no `await`) and runs under `contextlib.suppress(Exception)`.
      `CancelledError` and other `BaseException`s pass through unchanged, and the stored result
      or failure code is already final.
    - The run span carries `run_outcome`: `stored`, or the fixed `FailureCode` value. A trace of
      a run that stored nothing is then never read as a finished assessment.
  - **Off the event loop.** Only `BatchSpanProcessor` is used (never `SimpleSpanProcessor`), so
    export runs on its own thread. An export failure, timeout or full queue is dropped, never
    raised into the run. Fixed at build and tested: exporter timeout, retry cap, queue size and
    shutdown wait.
  - **Shutdown order.** The tracer provider is built with `shutdown_on_exit=False` (no `atexit`
    hook). It is shut down only after `RunManager.shutdown` has returned (every run marked), in
    a daemon thread joined with a fixed timeout, so a hung export delays process exit by at most
    that bound. It never delays
    storing a record.
  - **Counted, not logged as text.** A wrapper counts export failures by a fixed reason
    (`timeout`, `connection`, `http_status`, `queue_full`), dropped collector entries count as
    `collector_full`, and each of the five reasons is logged once as a code. Tracing is built
    before item 2 (owner, 2026-11-15). Until item 2 exists, the count is a plain in-process
    counter in `divesafe.observability.collector`: a fixed mapping from the five reasons to an
    `int`, incremented under a `threading.Lock` because the export thread writes it; the lock guards
    only the increment, never I/O or logging, so the event loop never waits on an export. It has
    no endpoint, and tracing imports no `prometheus_client`. A unit test (not a safety test: the
    counter cannot change an outcome) checks that a full collector increments `collector_full`
    and each stub-session failure increments its own reason. Item 2 moves the count into its
    dedicated `CollectorRegistry` in the same change that adds its AST pin on
    `start_http_server`, `make_asgi_app`, `make_wsgi_app` and the global `REGISTRY`. The
    `opentelemetry`, `urllib3` and `requests` loggers each get `propagate=False` and a
    `logging.NullHandler()` (without a handler Python's last-resort handler would print to
    stderr), because they can print the endpoint, the server's reply text and connection errors
    with host and port.
  - **Never read back.** Nothing read from Langfuse reaches a prompt, a rule, a record or the
    corpus. Langfuse is not the audit record: where a trace and a record disagree, the record
    wins.
  - **Not a decision or outcome store.** No scores, annotations, comments or annotation queues
    are used on `divesafe-prod` traces. Human decisions, override rationales and later actual
    conditions are recorded only through the API (AGENTS.md section 6); a Langfuse note is never
    one of them. Spans carry no recommendation (deterministic, proposed or final), so a trace
    cannot be read as the result of an assessment that is still `PENDING_HUMAN`.
- **Langfuse service hardening** (operator work, its own compose files outside the repository
  and outside synced folders, like `docker/secrets/`):
  - `TELEMETRY_ENABLED=false` and `NEXT_TELEMETRY_DISABLED=1` (the second to verify against
    the pinned version); `AUTH_DISABLE_SIGNUP=true`; `LANGFUSE_DISABLE_SIGNUP_ONBOARDING=true`.
  - Every default password and secret in the reference compose is replaced (`NEXTAUTH_SECRET`,
    `SALT`, `ENCRYPTION_KEY`, the PostgreSQL, ClickHouse, Redis and object-storage passwords).
    Langfuse reads these as env values in its own compose files; that is outside DiveSafe's
    secret-file rule and gets the same care.
  - No LLM connections, playground, LLM-as-a-judge evaluators, Halo, SMTP, SSO, Slack, webhooks,
    or blob-storage exports: each is new egress and needs a new ADR.
  - Only the web service publishes a port, on `127.0.0.1` only, with its own admin password. Its
    worker, PostgreSQL, ClickHouse, Redis and object storage are on an `internal: true` network
    with no published port.
  - Langfuse's address on `divesafe-trace` is never in `DIVESAFE_TRUSTED_PROXIES`.
  - Images pulled from Docker Hub (`langfuse/...`, not `docker.langfuse.com`) and pinned by
    digest; updated by the owner.
  - Its data stays outside DiveSafe's backups. It is a secret store and a store of run metadata.
  - `docker inspect` and `docker compose config` show every Langfuse secret: their output is never
    pasted anywhere. After each Langfuse compose change the operator also checks that
    `langfuse-web` still has `CapDrop: [ALL]` (it is what keeps the web container from routing
    between its networks). Headless initialisation creates the admin login only if it does not
    exist, so after the owner changes the password in the UI the `inspect` value is stale; the
    `LANGFUSE_INIT_USER_*` lines are then removed from the compose file, `ADMIN_PASSWORD` from
    `langfuse.env`, and the stack recreated.
  - Built (step 5, 2026-11-16) in `~/DiveSafe/langfuse/`: Langfuse 4.80.0, no ai-gateway, Redis
    as its own user with the password in a 0600 file, secrets from a 0600 `langfuse.env` made by
    `make-secrets.sh`; the network is `10.231.77.0/24`, pool `.128/25`, web at `10.231.77.10`.
    `langfuse-web` sets `HOSTNAME=0.0.0.0`: Next.js binds to `$HOSTNAME`, and Docker's default
    (the container id) resolves to the first network's address only, so `10.231.77.10` had no
    listener and the API's connection was refused. It also has a healthcheck on
    `/api/public/health`, so `up --wait` returns only once the UI answers (the worker has none).
    The owner created the `divesafe-prod` project and its keys in the UI and changed the admin
    password; the `LANGFUSE_INIT_*` lines and `ADMIN_*` values are removed. `make-secrets.sh`
    is for first set-up only: re-running it replaces every secret.
  - Compose wiring (2026-11-16): the API service passes the two settings, off by default, and
    mounts `divesafe_trace_public_key` and `divesafe_trace_secret_key`. Compose needs both files
    even with tracing off. `make-secrets.sh` creates them empty if they're missing, and an empty
    file means "not set". The owner writes the keys with the `read -rs` recipe in
    `first-run-on-the-real-host.md` (each file made writable, then back to 0444); on an existing
    deployment `make-secrets.sh` is re-run first to add the two empty files. Tracing is
    switched on only by setting both values in `production.env`. Switching it off means setting
    them back to `false` and empty and recreating the API.
  - Switched on (2026-11-16, owner): main `cb999fd` deployed. `production.env` sets
    `DIVESAFE_TRACING_ENABLED=true` and
    `DIVESAFE_TRACE_ENDPOINT=http://10.231.77.10:3000/api/public/otel/v1/traces`, and start-up
    passed every tracing check. First traced assessment, `9d60ab2e...`:
    - one trace with 18 spans (the run, 8 stages, 9 model calls) in `divesafe-prod`;
    - input, output, status message, user, session and tags are empty on every span;
    - the metadata holds only the allow-listed names, codes, counts and durations, and the
      longest value is the assessment id.

    Langfuse 4 stores OTLP spans in its `events_*` ClickHouse tables, not `traces` or
    `observations`, so a check must read those. To switch it off, set the flag to `false` and
    the endpoint to empty, then recreate the API.

### 1. Tracing (production)

The OpenTelemetry SDK with the OTLP/HTTP exporter to Langfuse's OpenTelemetry endpoint
(vendor-neutral; the `langfuse` SDK is not in the runtime image). Manual spans only: one per run,
per stage and per model call. These are timings and fixed codes, not a reasoning trace (AGENTS.md
section 9).

- **The allow-list covers the whole export.**
  - Span names are fixed: `run`, the `StageName` values and the task names.
  - Run span attributes: `run_outcome` (`stored` or a `FailureCode` value), `prompt_version`
    (`PROMPT_VERSION`), the request id, only when it matches `[A-Za-z0-9-]{1,64}` (item 1,
    built 2026-11-17; the whole-export pin was updated in the same change) and the assessment
    id, only when it is 32 lowercase hex characters (the
    two forms the API creates, `uuid4().hex` and `_idempotent_id`); otherwise it is left out.
  - Stage span attributes: the stage name, outcome and duration from its `StageTrace`, never its
    evidence ids or issue codes' free parts. A stage span ends at `StageEntry.ended_ns`, the wall
    clock when the collector appended the entry, and starts its duration earlier. An unknown
    stage name is dropped.
  - Model-call span attributes, exactly these and pinned by a test:
    - `task`, `outcome` (one of the nine `CALL_OUTCOMES` labels), `output_limit`,
      `output_chars` and `duration_ms`, taken from the same dict the "model call finished" line
      uses; an unknown task is named `unlisted_task`;
    - span-only, under the Langfuse / OpenTelemetry GenAI names (owner, 2026-11-16):
      `gen_ai.usage.output_tokens` (the log line's `output_used`), `gen_ai.usage.input_tokens`
      (`input_used`, the server's `usage.prompt_tokens` through `plain_count`, filled since
      step 4, also on a token-limit stop when the server sent usage before stopping) and
      `gen_ai.request.model` (`provider.model`, needed for Langfuse prices, only when it
      matches `[A-Za-z0-9._:/@-]{1,128}`, holds no `//`, a model host is recorded and the
      name does not contain it (case-insensitive); otherwise left out). The log line,
      `CALL_LOG_FIELDS` and its log-leak test are unchanged. The prompt version is a run-span
      attribute only.
  - The resource is built explicitly with `service.name` and the SDK's `telemetry.sdk.*` only:
    never `Resource.create()`, no resource detectors.
  - No events, links or baggage. No trace context is read from a request or sent to the model
    host or a connector (no `traceparent` in or out).
- **Never exported:** prompt or reply text, evidence values or ids, plan or site free text,
  actor, `key_id` or client address, decision or rationale text, a recommendation
  (deterministic, proposed or final), URLs, hosts, headers or exception text
  (`record_exception=False`, no status descriptions). No auto-instrumentation.

### 2. Evaluation (development only, the allow-listed harness sets only)

A script in `tests/evaluation/` (the `[eval]` extra) may send one harness run to the
`divesafe-eval` project as a Langfuse dataset run with the harness's scores.

- **What is sent:**
  - **Prompts:** the full prompts, captured by the harness's sink from `LLMRequest.messages`.
  - **Outputs:** only what the stored record holds (findings, scenarios, the proposal and the
    explanation), read from the record after the run, never from `LLMResponse`.
  - **Never kept, so never sent:** the sink never keeps `LLMResponse.text`. That excludes a
    refused reply, a reply parsed and then refused (such as a second-round reply carrying risk
    factors), partial text before a token-limit stop, raw stream events and any reasoning field
    (AGENTS.md section 9; evaluation-strategy.md "no raw text is kept for a refused reply").
  - **Self-estimate:** the model's `confidence` self-estimate is removed before sending and is
    never a Langfuse score (section 11).
  - **Route:** the harness sends through `LLMProvider` and its own sink, never through a
    Langfuse model-SDK wrapper.
- **The cases are synthetic** (synthetic engines and plans over recorded public fixtures), not
  production data.
- **Before any network call it refuses unless all of these hold:**
  - the cases are exactly the sets named in an allow-list in the harness (today `SCENARIOS` and
    the adversarial set), checked by identity, never by a `synthetic` flag, and the fixture
    payloads they use match pinned SHA-256 values;
  - the tide check is not included while PHN's terms are not on file (ADR 0021);
  - the sending script never imports `api.state` or `capture_item6` (an AST test), so it
    cannot use the item-6 capture, its replays or `build_default_state`;
  - no knowledge connector is built, and `DIVESAFE_EVAL_KNOWLEDGE_HOST`, `DIVESAFE_DATABASE_URL`
    and `DIVESAFE_ADMIN_DATABASE_URL` are unset;
  - the Langfuse URL is passed as `base_url=` and is exactly `http://127.0.0.1:<port>`, the fixed
    port Langfuse's web service publishes (never `localhost`, `::1` or another port). This is
    plain http on loopback: the second, development-only exception to section 12's https rule;
  - no `LANGFUSE_*`, `OTEL_*` or proxy variable (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
    `NO_PROXY`, in any case) is set, `DIVESAFE_TRACING_ENABLED` is unset, and
    `urllib.request.getproxies()` is empty (which includes the macOS system proxy). The SDK gets
    an `httpx_client` with `trust_env=False` and no redirects; how its own OTLP session is built
    is verified at build;
  - the keys are read from files in `DIVESAFE_EVAL_SECRETS_DIR` (outside the repository and
    synced folders) and passed as `public_key=` and `secret_key=`;
  - an authenticated read with those keys shows the project `divesafe-eval` and only it. Another
    listener on that port then sees the development keys, never a prompt.
- The prompts still go to the evaluation's model host under `egress_gate`; Langfuse only adds a
  local copy. Production assessments, records, knowledge extracts and human decisions are never
  sent.
- **Testing.** The live run is marked `integration`. The refusal checks live in a module that
  does not import `langfuse`; the SDK is imported only after every check has passed, so their
  offline tests run under `.[dev]` in CI. Each refusal test has a mutation check, uses an
  in-memory sink, makes no network call, is marked `safety` and never skips. An AST test pins
  that the checks' module has no top-level `langfuse` import. An offline test with a scripted
  refused reply carrying a marker shows the marker never reaches what the sink would send.
- **Scores are advisory.**
  - The offline safety gates are not scores. `false_go = 0` and `ran + refused = cases`
    (`tests/safety/test_adversarial_false_go.py`) stay hard CI gates, computed in the test and
    never read from Langfuse. A Langfuse run never replaces or waives them.
  - Scores never change a rule, a prompt, a threshold, a ruleset, `confidence` or the corpus
    automatically. A change they suggest is a reviewed Git change (for a prompt, a
    `PROMPT_VERSION` bump and the pinned digest), as for any other finding (architecture.md,
    Learn; evaluation-strategy.md, "Human-in-the-loop and learning").
  - A score on synthetic, unreviewed cases is never the source of a safety threshold (AGENTS.md
    section 7) and is never shown as confidence (section 11).
  - Dataset expected outputs are written by a person, never copied from a model reply.
  - Nothing from Langfuse is ingested into the corpus or history (section 8).

### 3. Prompt mirror (read-only)

Prompts stay in code, reviewed in Git: `agents/prompts.py` (system prompt and trusted notes) and
each agent's task instructions (`specialists.py`, `tot.py`, `risk_agent.py`,
`cross_examination.py`). `PROMPT_VERSION` and the pinned digest in `test_prompt_limits.py` cover
the system prompt and trusted notes; per-task instruction text is not pinned (ADR 0007 known gap).

- A development script pushes the system prompt, the trusted notes and each fixed task's
  instruction text rendered with code constants only (task names, `SPECIALISTS` fields), never
  with a plan, evidence or knowledge, to `divesafe-eval`. Each is labelled with
  `PROMPT_VERSION`, the commit and its SHA-256. It refuses to push a text under an existing
  version label with a different digest.
- The script has the same guards as section 2 (exact loopback `base_url`, no proxy, `LANGFUSE_*`
  or `OTEL_*` variable, keys from `DIVESAFE_EVAL_SECRETS_DIR`, the project check).
- It never uses Langfuse's `production` or `latest` labels.
- The mirror is not authoritative: an edit there changes nothing. `PROMPT_VERSION` in code, the
  record's `model_version` and the Supervise `prompt_version_pinned` check stay the only
  authority.

### 4. Cost reporting

Model-call spans carry the model name and the server's input and output token counts when the
server sends them. The adapter also reads `usage.prompt_tokens` (step 4; a change to ADR 0018's
amendment), from the same `usage` object as `completion_tokens` (the last event whose `usage` is
not null), through `plain_count`. A missing, malformed, negative or over-cap `prompt_tokens` gives
None and never changes what is accepted or refused: the bad-value, ceiling and "usage never
changes a refusal" cases cover it (`tests/unit/test_openai_compatible_input_count.py`). The new
`LLMResponse.input_tokens` field maps any value `plain_count` rejects (wrong type, bool, negative,
over the cap) to None, so building an `LLMResponse` never fails on it; `TokenLimitError` carries
`input_tokens` too, kept through pickle and copy. The runner passes it to the trace collector as
`input_used` only: the "model call finished" line keeps exactly `CALL_LOG_FIELDS`
(`tests/agent/test_model_call_input_count.py`, with mutants for logging it and for dropping it).
The request body stays byte-for-byte the golden body (no `stream_options`). Production's server
sends `completion_tokens` unasked (measured 2026-11-15, 27 of 27 calls); whether it sends
`prompt_tokens` is not yet measured. A server that sends no usage leaves the counts empty.

- Prices are Langfuse model-price entries kept by the owner. Costs are estimates, not invoices.
- Cost is reporting only: no money value goes into a prompt, a rule or a record.
- No budget stops a run. A budget would need its own decision, and any budget stop could only
  drop the agent stage to "no proposal", never block assessment creation, the deterministic
  result or the human gate.

### Safety tests (MUST, `tests/safety/`, never skipped)

- **Export scan:** the whole-export allow-list pin, and a scan of the serialised OTLP request
  body for the markers of `test_model_call_log_leaks.py`, with mutation checks, one mutant per
  field (reply text, prompt, exception text, evidence id, recommendation) plus `run_outcome`,
  assessment id, task name, model name and events, each asserted to be caught by its own
  marker. The model-name marker may appear only as the value of the `gen_ai.request.model`
  attribute of model-call spans (asserted separately: it appears
  nowhere else in the body). The body scan runs the real session, OTLP/HTTP exporter, batch
  processor and tracer provider over a stub HTTP adapter that keeps each request (no socket;
  `tests/safety/trace_wire.py`), on a real 14-call cross-examination run and a planted trace.
- **Silent export logs:** after start-up, a WARNING record carrying a marker on
  `opentelemetry.sdk.trace.export`, `opentelemetry.exporter.otlp.proto.http.trace_exporter` and
  `urllib3.connectionpool` reaches neither the JSON handler nor stderr (`capsys`); removing the
  `NullHandler` fails it, and removing `propagate=False` fails it (the record reaches the JSON
  handler).
- **Record unchanged.** Through `RunManager`, with an in-memory run store and a fixed clock,
  each traced setup is compared against tracing off:
  - **Setups:** a tracer whose `start_span` raises (the only tracer method the exporter calls);
    a collector whose append, start or finish raises; a full collector; a sink whose `emit`
    raises; the real export over the stub wire; an export that blocks until the test releases
    it (so no test can hang the suite); a session that raises a connection error (no socket).
    Each setup also checks that it took effect.
  - **Must be equal:** the stored record, field by field except `stages[*].duration_ms`; the
    run's status and failure code; and how the `_wrapped` task ends (it returns `None`, or
    raises the same exception type as with tracing off), with no extra error log record.
  - **Paths:** ok; call timeout; stage deadline (`STAGE_TIMED_OUT_ISSUE`); run deadline;
    cancellation (`CancelledError` still leaves `_wrapped`, code `shutdown`); Supervise
    invariant breach; a crash in Reason (nothing stored, code `internal`).
  - The export setups use the stub wire (the real exporter), not the SDK's in-memory exporter.
- **Ordering:** no span reaches the exporter before `complete` has returned or `_mark_failed`
  has run; the test exporter records the store's state when it receives each span.
- **Shutdown bound:** with the blocking exporter (released at teardown) and the provider built
  with `shutdown_on_exit=False`, `RunManager.shutdown` and the tracer shutdown finish within
  the fixed bound.
- **Mutations:**
  - Removing the suppression around the append fails the record test.
  - Removing it around the emission fails the "task ends the same way" check on every path.
  - Widening the emission's suppression to `suppress(BaseException)` fails a dedicated case. The
    tracer raises a test-private `BaseException` subclass (never `KeyboardInterrupt` or
    `SystemExit`, which asyncio re-raises out of the event loop and would stop the pytest session)
    after the record is stored. Run through `RunManager.start`, the test takes the run's task by its
    name `run-<id>` from `asyncio.all_tasks()` before it finishes, and asserts that the record is
    stored and that the task ends with that exception (`task.exception()`). (The cancellation case
    cannot catch this mutation: a `suppress` inside a `finally` never sees an in-flight
    `CancelledError`.)
  - Moving emission before `complete` fails the ordering test.
- **AST pins:** no `opentelemetry` import in `domain` or `risk`; no `langfuse` import anywhere
  in `src`, and `langfuse` is not a runtime dependency in `pyproject.toml` (the image build
  checks it is not installed); no `trace.set_tracer_provider` or `trace.get_tracer_provider` in
  `src` (nor any other global getter or setter, and `get_tracer` only on the private provider);
  the process-wide provider is still the default after start-up; the OpenTelemetry SDK, the
  exporter and `requests` are imported only inside the tracing set-up, which runs only when
  tracing is enabled (no module-level `opentelemetry`, `requests` or `urllib3` import in `src`;
  a subprocess test shows that with tracing off none of the SDK, exporter, `requests`,
  `urllib3` or `google.protobuf` is loaded). The image build checks that the SDK and exporter
  import and that `langfuse`, `opentelemetry-distro`, `opentelemetry-instrumentation` and the
  `opentelemetry-instrument` and `opentelemetry-bootstrap` launchers are absent.
- **FastAPI telemetry:** with a global SDK provider set, the app records no span; with
  `FASTAPI_OTEL_AUTO_CONFIGURE=true` and an `OTEL_*` endpoint set, starting the app sets no
  global provider. Mutations: dropping `telemetry=FASTAPI_TELEMETRY_OFF`, or turning tracing and
  auto-configuration on, fails each.
- **Session and refusal:** the session ignores proxy and CA variables, mounts only `http://`
  with no retries, refuses any other method or URL, never follows a redirect (mutations:
  following redirects, trusting the environment); nothing is built when the start-up check
  refuses or tracing is off (mutation: skipping the check).
- **Start-up refusals:** tracing enabled without an endpoint or the reverse; tracing enabled
  without both project keys, or with a key of the wrong shape; an endpoint that is not exactly the
  fixed address form (a host name, another scheme or path, a missing, zero-padded or out-of-range
  port, credentials, a query or a fragment, an address outside the private ranges, or the network,
  gateway (.1) or broadcast address of its /24); a failed subnet check or a failed `connect` (an
  error number or an `OSError`); any `OTEL_*` variable while tracing is enabled; never echoing the
  value. The address is not pinned to the web container's fixed `ipv4_address`: any acceptable
  private address passes, and the subnet check and the operator's `docker network inspect` stand
  in for it. With tracing off and no endpoint, start-up succeeds even when the subnet check would
  fail and an `OTEL_*` variable is set, and no socket is created (mutations: running the subnet
  check, or the `OTEL_*` refusal, unconditionally fails it; the test patches `socket.socket` to
  prove no socket is created).

Also: in `tests/unit` (not safety), with two concurrent runs, each trace holds only its own model
calls. The evaluation refusal tests (section 2) live in `tests/evaluation`, are marked `safety`,
are offline and need no `[eval]` extra.

They never import `langfuse` and never `importorskip`; `opentelemetry-sdk` is a runtime
dependency, so `.[dev]` in CI has it. Section 4's two skip exceptions stay the only ones.

Build note: `mutated_module` in `test_model_call_log_leaks.py` requires each edit target (such as
the `"duration_ms"` line) to occur exactly once in `runner.py`. If the collector copies that
line, update the target; never loosen the check.

### Dependencies

`opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` (runtime, bounded ranges,
`pip-audit`); `requests` declared directly as `>=2.32.4,<3` (step 3: the trace session imports
it and relies on its redirect, proxy and environment behaviour; 2.32.4 fixes the `.netrc`
credential leak, CVE-2024-47081); `langfuse` in a new `[eval]` extra only, never in the
runtime image. Licences to verify from package metadata at build.

### Owner decisions (2026-11-15, each as the recommended default)

- **Assessment id on spans: yes** (links a trace to its record), while Langfuse stays local.
- **Production prompt and reply text in traces: no.** Allowing it would amend AGENTS.md
  sections 3 and 9, put knowledge extracts and plan fields in a second store, and turn the
  remaining "local rests on compose files" gap into a blocker.
- **Langfuse's update check: accepted.** To publish its UI port, the web container sits on a
  network with internet access, so it can reach any host, not only `langfuse.com`; the other
  outbound features are off by configuration, not blocked by the network. Langfuse documents no
  switch for the update check, which sends the version number when a signed-in user loads the
  UI. Alternatives not chosen: keep the web container on internal networks only and publish
  `127.0.0.1:<port>` through a small reverse-proxy container (the update check then fails,
  which Langfuse documents as graceful), or a host firewall rule.
- **Order of build: Langfuse first** (owner, 2026-11-15, replacing "after items 1 to 3",
  given earlier the same day): tracing, cost, evaluation and the prompt mirror, then items 1
  to 3 in their own order.

### Known gaps (amendment)

- Export failures and dropped collector entries are counted in `/metrics` (item 2,
  `divesafe_trace_dropped_total`). No alert is wired on them yet (item 3). The collector now
  also runs with tracing off (for metrics), so the `collector_full` log line can appear then too.
  The run span carries the request id (item 1); stage
  and model-call spans never do.
- FastAPI itself requires and imports `opentelemetry-api`, so with tracing off the OpenTelemetry
  API is loaded (inert: no provider is set and FastAPI's telemetry is off). The SDK, the
  exporter, `requests`, `urllib3` and `google.protobuf` are not loaded (subprocess test, step 3).
- The library retries 429, 502, 503, 504 and connection errors up to its internal
  `_MAX_RETRIES=6`, with backoff, bounded only by the 3 s export timeout; the adapter's own
  `max_retries=0` does not change that. Each failed export is still counted once.
- On a token-limit stop, `input_used` is usually None: servers send usage after the finish
  event, and the reply is refused as soon as "length" is seen (as for `output_used`).
- The model-name filter refuses only the run's own model host (and the whole name when no
  model host is recorded; `host` has no default): any other host written into the model name
  by the operator still passes the pattern and reaches a span. The host is
  `urlsplit(...).hostname` (no port, lowercased); a base URL written with a trailing dot
  (`host.example.`) would miss a name holding `host.example`.
- `OTLPSpanExporter` falls back to `OTEL_EXPORTER_OTLP_*` variables for every argument passed
  as `None` (certificate and client-key files among them). That is safe only because start-up
  refuses any `OTEL_*` variable while tracing is enabled; the two must change together.
- The `_global_tracer` AST pin allows `get_tracer` on a name `provider` only; an alias of the
  global provider under that name would pass. The exact ban on `set_tracer_provider` and
  `get_tracer_provider` by attribute name is the real pin.
- Tests rely on private internals: `opentelemetry.trace._TRACER_PROVIDER`, FastAPI's
  `telemetry._runtime._shutdown` and `_OTLPHTTPClient._compute_backoff`. Re-check them on any
  OpenTelemetry or FastAPI version bump.
- `check_tracing_startup` runs twice (in `build_default_state` and in `build_trace_export`);
  harmless, and it makes the exporter fail closed on its own.
- `trace_export.shutdown()` blocks the event loop in the lifespan for up to its 4 s bound, at
  exit only.
- A failure while building spans (after the reservation) loses that run's trace without a
  drop count: the reservation is released and the runner swallows the error, but no reason is
  counted.
- The fixed bounds are pinned by the code's constants; the tests read those constants rather
  than literals, so changing a bound changes the test with it.
- `test_tracing_record_unchanged.py`'s `EXPORTS` holds module-level mutable `_Export`
  instances, reset per test.
- Verified at build (step 3, SDK 1.45.2, FastAPI 0.162.7): `OTLPSpanExporter` takes
  `max_request_size`; FastAPI's only telemetry variable is `FASTAPI_OTEL_AUTO_CONFIGURE`
  (`FASTAPI_ENV` controls a front-end path check only). Re-check on a version bump.
- The no-socket test sees only `socket.socket`: a later `socket.create_connection` or
  `from socket import socket` would get past it. An AST pin would close that.
- Langfuse is a second store of run metadata with its own retention; nothing deletes it in step
  with DiveSafe.
- A run that stores nothing (a Supervise invariant breach or a crash in Reason) still leaves a
  trace, marked by `run_outcome`, with no matching record.
- Trace export is best effort; dropped spans are counted, not recovered.
- Token counts are the server's own claims, unchecked (up to 10^9 per call), and exist only
  when the server sends usage unasked; a misbehaving server can only inflate Langfuse's cost
  estimate. Prices apply to the requested model name, not necessarily the model that answered
  (routing, evaluation-strategy.md).
- Langfuse's web container has internet access, and it holds every evaluation prompt and output.
  Its documented outbound call is the update check to `langfuse.com`. The UI loads assets from
  `img.shields.io` and `static.langfuse.com` in the owner's browser. Because of that route, an
  LLM connection or the playground, if added in the UI, would send data off the Mac; the rules
  forbid them, but only the operator stops it.
- Like ADR 0023, "local" rests on the operator's compose files: the code checks that the
  endpoint is the fixed address, and the network provides the isolation. Compose does not check
  the `internal` flag of a network created elsewhere (hence the operator's inspect check).
- Langfuse's web container can reach the API's port on `divesafe-trace`. Every endpoint but
  `/health` still needs a key; this path bypasses the TLS terminator, and
  `DIVESAFE_TRUSTED_PROXIES` must never include an address on `divesafe-trace` (only the
  `production.env.example` comment says so; a start-up refusal would enforce it).
- A container joined to `divesafe-trace` with a network alias equal to a name the API resolves
  (`db`, the model host) would answer that lookup. SCRAM channel binding stops a fake database,
  the adapter's https certificate check a fake model host, and the operator's inspect check is
  what stops anything else joining.
- The `test_production_compose.py` tracing check is an exact pin (2026-11-16): the API's only
  tracing lines are `DIVESAFE_TRACING_ENABLED` (default `false`), `DIVESAFE_TRACE_ENDPOINT`
  (default empty) and the two key secrets. No other service has a line naming tracing, and no
  service has an `OTEL_` line. It reads the compose file as text, so a setting arriving another
  way (an `--env-file` value, `docker run -e`) is caught only by start-up's own checks.
- The API's trace keys can also read the `divesafe-prod` project through Langfuse's public API.
  The data is its own span metadata, and `src` never reads from Langfuse.
- The compose scans read raw text, not decoded YAML. A YAML escape (`\x`, `\u`, `\U`) is refused
  anywhere in the file, so a name can't be hidden from them.
- Loopback is a port, not a proof (ADR 0023). The evaluation project check stops a listener
  started by mistake, not a hostile local process.
- OpenTelemetry, `requests`, `httpx` and Langfuse SDK behaviour (proxies, redirects, logging,
  `OTEL_*`, the SDK's OTLP session) and Docker Desktop's DNS and network flags are to verify
  against the pinned versions at build.
- With tracing off, `OTEL_*` variables have no effect only while no auto-configuration is
  installed and the SDK is not imported (both checked above); verify at build that importing
  the pinned SDK reads no `OTEL_*` variable that matters.
- The evaluation script's AST import check sees direct imports only, not transitive or
  `importlib` ones; requiring `DIVESAFE_DATABASE_URL` to be unset is what stops
  `build_default_state` in practice.
- `urllib.request.getproxies()` does not report PAC or auto-discovery proxies. The
  `httpx_client` with `trust_env=False` does not use them; the SDK's own OTLP session is to
  verify at build.
- The subnet check runs at start-up only. A `docker network disconnect` while the API is
  running is not detected, and spans would then follow the default route until restart.
  Compose recreates the container on any network change, so this needs a manual command; the
  operator's inspect check is the only control after start-up.
- "No scores, annotations, comments or annotation queues on `divesafe-prod`" is operator
  discipline; the Langfuse UI does not prevent it unless project roles can (verify at build).
- If `_mark_failed` itself fails, `run_outcome` shows the intended code while the store keeps
  `RUNNING` until cleanup marks it (later `timeout`); if the run's task is cancelled again while
  `_mark_failed` is awaited, no trace is emitted. The record and the store win.
- The collector's module is imported by `agents`, `orchestration` and `api`, so it imports
  nothing from `api` or `orchestration` (AGENTS.md section 2).
- AGENTS.md section 12's "only destination of run data" covers destinations the app pushes to;
  logs on stdout and the `/metrics` scrape are outside that wording.
- AGENTS.md section 12's first bullet names `DIVESAFE_SECRETS_DIR` only; the development-only
  `DIVESAFE_EVAL_SECRETS_DIR` is described here, not in AGENTS.md.
- Outside this amendment: the Supervise pin is a substring check (`prompts=2026-11-10.1` also
  matches `prompts=2026-11-10.12`); worth an exact-token match when the prompt mirror is built.

## Consequences

- Item 1 reuses `DIVESAFE_TRUSTED_PROXIES`; item 2 adds the `metrics` role to ADR 0013's role
  set (a key with only that role can read `/metrics` and nothing else) and no new port. Item 4
  adds `DIVESAFE_TRACING_ENABLED`, `DIVESAFE_TRACE_ENDPOINT` (a fixed address, Amendment
  2026-11-15) and the `divesafe-trace` network.
- Item 2 touches `api` and `orchestration` (counters are incremented where the stage trace is
  written); it does not touch `domain`, `risk` or the record.
- New dependencies: `prometheus_client` (decided 2026-11-12; pinned, `pip-audit`);
  `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` (runtime, item 4); `langfuse` in
  an `[eval]` extra only, never in the runtime image (Amendment 2026-11-15). The
  all-routes-authenticated safety test keeps `/metrics` on the authenticated side; the MUST tests in
  item 2 (role isolation, mixed key refused at start-up) and the AST pin on the library's listeners
  and global registry are new safety tests.
- Option (b) is rejected: it would have needed an owner amendment to AGENTS.md section 12 and a
  change to the all-routes-authenticated safety test.
- Uptime Kuma is a separate self-hosted service with its own update and backup duties (operator
  work, outside this repository); it stores the API key and the push URL in plain text, so it
  is a secret store (item 3).
- Item 1 is a contract with the operator's proxy: the proxy must strip or replace an inbound
  `X-Request-ID` it did not set.
- Alerting is operator work and can fail silently itself; the first alert to set up is the
  backup one, since it already has a hook.
- Langfuse is a separate self-hosted stack (web, worker, PostgreSQL, ClickHouse, Redis, object
  storage) with its own update, backup and secret duties (operator work), and DiveSafe's compose
  file gains the `divesafe-trace` network.

## Owner decisions

Settled 2026-11-12 (named by topic):

- **Scope:** build items 1 to 3 for the MVP, in that order. (Amended 2026-11-15: the Langfuse
  amendment is built first, then items 1 to 3; see "Amendment 2026-11-15", Owner decisions.)
- **Metrics library:** B, `prometheus_client`.
- **Metrics authentication:** the `metrics` role; section 12 unchanged; the loopback-port
  exception is rejected.
- **Alerting tool:** Uptime Kuma.

Settled 2026-11-15, each as the recommended default:

- **Request id storage: logs only.** No record field and no migration; a log search by id finds
  the run.
- **Metric alerts and N: deferred.** Start with the `/health` and backup monitors, which need no
  metrics. The way metric alerts reach Uptime Kuma (a keyword or JSON check on a small
  authenticated endpoint, or a later Prometheus) and the N for the agent-failure rule are chosen
  once `/metrics` is built. This is still an open point, not a choice.
- **Tracing (item 4): yes, through self-hosted Langfuse** (owner, 2026-11-15; replaces the
  "no" given earlier the same day). See "Amendment 2026-11-15", which also records the four
  Langfuse decisions of that day.
- **`/metrics` and the limiter (decision 7): `/metrics` counts toward both** the rate limit and
  the auth-failure counter, with its own `metrics` entry in `LIMIT_KEYS` and its own setting,
  not reusing `read`. The auth-failure limit is counted per source before the key is checked,
  and all local clients share the Docker gateway address, so another client's bad keys **can**
  block the scraper and code cannot exempt it; Uptime Kuma treats a 429 as "auth blocked", not
  "down".

## Known gaps

- Until items 2 and 3 are built (now after the Langfuse amendment, owner 2026-11-15), the
  2026-11-12 failure mode raises no alert. A failed agent stage shows in the record, in the logs
  and, for exported traces, in Langfuse's model-call spans. Langfuse is best effort and not a
  monitor: nobody is notified.
- Per-process counters reset on restart and do not sum across workers (same as rate limiting).
- Metrics show failure counts, not causes; the cause stays in the logs and the record.
- Item 1 trusts the proxy addresses in `DIVESAFE_TRUSTED_PROXIES`; a misconfigured proxy that
  forwards a client's `X-Request-ID` lets a client choose ids, and behind a `127.0.0.1`-published
  port every local process looks like the proxy (log correlation only; no authority attaches to
  the id).
- No alert can be checked by a test in this repository; alerting is operator discipline.
- Connector worker threads carry only the request id (a fresh context per job). The run's trace
  collector is not thread-safe, so any future `record_*` call from `data` needs a lock or must
  stay out of the worker.
- An idempotent replay gets its own request id; the run keeps the id of the request that started
  it, so a replayed POST and its run show different ids in the logs.
- `prometheus_client`'s multiprocess mode is not used; with more than one worker each process
  exposes its own counters and a scrape sees whichever answers (same as the per-process rate
  limit). One worker is the production setting today (ADR 0024).
- Uptime Kuma cannot evaluate metric rates. The owner deferred the alert mechanism
  (2026-11-15), so the two failure alerts that motivated this ADR are not wired; only `/health`
  and backup are planned.
- A `metrics`-only key that leaks lets the holder read activity and failure rates; rotation is
  manual, as for every key (ADR 0013).
- A run that fails before its record is stored gives no stage, connector or knowledge counts,
  only its failure code and model calls. To be revisited when such a failure occurs (owner,
  2026-11-17).
- The in-flight gauge reads the run set without a lock, so it is exact only on a standard (GIL)
  Python build.
- `disable_created_metrics()` applies to the whole process: any later user of
  `prometheus_client` in the process gets no `_created` series either.
- `prometheus_client` reads `PROMETHEUS_*` variables as soon as it is imported, which is before
  the start-up check. Start-up still refuses before any metric or value file is created.
- The scraper shares the Docker gateway address with every other local client, so one client's
  bad keys can 429 the scraper (decision 7); the alert then says "auth blocked" and someone has
  to look.
- `host.docker.internal` from Uptime Kuma's container to the host-published port is unverified
  until build time on the owner's Docker Desktop.
- Built ahead of this ADR (2026-11-15, ADR 0018 amendment): one "model call finished" log line
  per model call, with task, outcome, output-token limit and use, characters and duration. It is
  a log line, not a metric; this ADR's metrics may count its outcomes later.
