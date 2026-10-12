# ADR 0016: Ollama adapter and the evaluation harness

Status: accepted

## Context

ADR 0007 built the agent stage and prompt layer, but only `FakeProvider` existed, so real-model
behaviour was unmeasured. Hosted adapters send evidence and plan fields off-machine.

## Decision

- **First adapter: Ollama** (`divesafe.models.ollama`), over its native `/api/chat`, using only
  the standard library (no new dependency, no vendor SDK). OpenAI and Anthropic are not built;
  each needs its own ADR for the dependency and a note on what the vendor sees.
- **Egress:** `external` is true unless the host is loopback (`localhost`, `127.0.0.0/8`, `::1`).
  A remote server must use https (see the amendments for the one exception) and is then refused unless `DIVESAFE_ALLOW_EXTERNAL_LLM=true`.
  Base URLs with credentials, a query, a fragment or an invalid port are refused. A look-alike
  such as `localhost.evil.example` is not loopback.
- **Safety of the call:**
  - Streamed (see the amendments), no redirects, and environment proxies are ignored (a proxy would carry the
    prompt somewhere the egress flag never approved).
  - Response capped at 1 MiB, read in chunks with `read1`, under a 50 s total deadline (the
    agent runner waits 60 s) and a 15 s per-read timeout. `read(n)` was rejected because a
    server sending one byte at a time defeated the deadline.
  - At most 4 calls in flight; extra callers are refused, not queued without bound.
  - Errors are fixed strings with no prompt, response or URL content; deeply nested JSON is a fixed
    error. The request is never logged. The model name is required (`DIVESAFE_LLM_MODEL`); the
    schema goes in `format`.
- **Authority is unchanged.** Output still passes `call_structured` (strict schema, cited ids
  must exist) and `reconcile` can only tighten. The adapter adds no trust.
- **Evaluation harness** (`tests/evaluation`): seven labelled SYNTHETIC scenarios run through the
  real pipeline with any provider.
  - Baselines: always-GO, always-CAUTION and always-NO-GO synthetic rules, a warning in force,
    a production-like engine (default policy, placeholder rules, so INSUFFICIENT EVIDENCE), and two
    injection payloads (one tries to close the `<untrusted_data>` block).
  - The synthetic engines set `degraded_may_support_go=True` and `required_factors=frozenset()`.
    That is a test override, not the production policy (ADR 0008 keeps it `False`). It is what
    makes GO, CAUTION and NO-GO baselines reachable at all.
  - Metrics: rule-outcome accuracy, under-severe errors (final below the expected result, the
    engine result or the proposal; must be 0), over-severe cases (reported), downgrade attempts,
    proposals obtained, schema-failure rate, and injection echoes in injected vs clean scenarios.
  - Offline tests use scripted providers (compliant, always-GO, always-NO-GO, prose, invented
    evidence, echoing) and gate in CI. They show the metrics would notice a failure; the guarantee
    itself comes from `reconcile` and the record validator.
  - The live test is opt-in (`DIVESAFE_EVAL_OLLAMA_MODEL`), marked `integration`, writes raw
    records with run time, code SHA and temperature, and fails if no proposal was obtained, so a
    dead model cannot pass.

## Amendments (2026-10-11)

- **Streaming.** The adapter sends `stream: true` and joins the lines. A reply counts only if
  exactly the last line has `done: true` with `done_reason` "stop" (a reply stopped by the token limit is refused) and every line has a string `message.content`; a cut-off
  stream or an error line is refused, never used partly. Reason: a proxy in front of the test
  server answered 504 after 30 s of silence on a non-streamed call.
- **`think`.** Defaults to `false` and is sent to the server (`None` leaves it to the server). A
  thinking model can otherwise spend its whole output budget on thinking and return nothing.
- **Plain http to a named remote host.** `DIVESAFE_OLLAMA_PLAIN_HTTP_HOSTS` is a comma-separated
  list of exact hostnames (empty by default; no wildcards, suffixes, IP addresses or ranges; any port on a listed host is allowed). Only
  those hosts may use `http://`. The server still counts as external, so
  `DIVESAFE_ALLOW_EXTERNAL_LLM=true` is still needed, and production refuses a non-empty list. The
  prompt then crosses the network unencrypted and the server can be impersonated; use it only on a
  network you control. On a shared network a spoofed DNS or `.local` (mDNS) answer can also rewrite
  the reply, so forged explanation text could reach the human decider (the recommendation can still
  only be tightened). Do not use `.local` names. Added at the owner's request for a LAN test server.

- **Time budgets are settings** (defaults are the original values).
  `DIVESAFE_LLM_CALL_TIMEOUT_SECONDS` (60, at most 600), `DIVESAFE_LLM_STAGE_TIMEOUT_SECONDS` (150,
  at most 1800), `DIVESAFE_LLM_FIRST_BYTE_TIMEOUT_SECONDS` (15, at most 300) and
  `DIVESAFE_LLM_MAX_CONCURRENT_CALLS` (4, 1 to 8, per provider instance, so per process). Settings
  and the adapter's constructor both refuse a stage budget below one call's, and a call below twice
  the first-byte timeout plus 1 s. The adapter exposes `call_timeout_seconds` and
  `stage_timeout_seconds`; the agent runner reads them and caps them at its own ceilings (a missing,
  non-numeric, boolean, non-finite or non-positive value falls back to the default).
- **Output-token cap is a setting.** `DIVESAFE_LLM_MAX_OUTPUT_TOKENS` (1500, 256 to 8000). Both
  adapters expose `max_output_tokens`; the runner reads it and caps it at 8000 (a non-integer,
  boolean or non-positive value falls back to 1500). A reasoning model thinks inside this budget:
  `gpt-oss-20b` on llama.cpp stopped at `length` on scenario calls at 1500. Raising it relaxes no
  acceptance rule: a `length` stop is still refused, the schema limits and the 20,000-character
  output limit are unchanged, and a longer reply is refused, never trimmed. A larger cap needs
  larger call and stage time budgets, or calls end as timeouts instead. The 1 MiB response limit
  also applies: a streamed reply costs roughly 100 to 250 bytes per token, so on an
  OpenAI-compatible server about 4000 to 5000 tokens can already exceed it (an estimate, not
  measured), and the reply is refused. On a hosted endpoint a larger cap raises billable output
  per call. The constructors accept 1 or more; `Settings` requires 256. With Ollama a large cap
  against a small `num_ctx` may shift the context; this is not checked.
- **Threads.** Model calls run on a pool owned by the provider (one worker per allowed concurrent
  call) and queue there, so a line of slow calls cannot use up the default threads the data
  connectors need. The worker deadline is the call budget minus the first-byte timeout minus 1 s,
  because the deadline is checked between reads and one read or the connect can take a further
  first-byte timeout; the thread therefore ends before the agent's own timeout does.
- **A slow stage cannot leave a stale result.** A provider requires a clock. After the stage the
  pipeline reads it again and judges the same evidence at that later time. Every earlier rule
  result is kept and every new one is added (stale evidence, an expired rule, newly unevaluated
  factors), plus `assessment.window_started` if the planned start is at or before that time. The
  result can only get worse, a change is noted in `agent_issues` (the agent output reflects the
  earlier result), and `created_at` is the later time.
- **Intended use:** a slow single-GPU server, for example 1 call at a time, 90 s to the first byte,
  200 s per call and 900 s per stage.

## Not solved

- No live run has been made: real-model behaviour is still unmeasured. No model is recommended.
- The scenarios are not reviewed by a diving professional and do not measure real-world safety or
  explanation quality.
- Citation validity checks that ids exist, not that they support the claim. Faithfulness needs a
  human-rated sample.
- Injection detection is a screening heuristic over marker phrases and does not catch a
  paraphrase. It is not a gate.
- - A request now lasts as long as the whole stage (plus up to 30 s of evidence gathering). The web
  client waits 120 s, so a stage longer than about 90 s shows "outcome unknown" in the UI while the
  API still stores a pending record, and a retry after more than 10 minutes cannot be replayed.
  Creating assessments asynchronously (202 and poll) would fix it and needs its own ADR.
- There is no server-wide cap on assessments in flight; the per-actor create limit is per minute.
- A stage timeout counts as one issue for many lost calls, so the schema-failure rate
  understates in that case.
- The harness reuses `tests/agent/scripted_llm.py` fixtures, so changes there change the
  evaluation.
- Model output is nondeterministic even at temperature 0 on some backends; compare runs.
- The stored `model_version` is the name only, not a digest; pulling and pinning a model is the
  operator's job.
- The total deadline is checked between reads only: waiting for a slot, connecting, DNS and one
  read can run past it, so a worker thread can outlive the 60 s stage timeout.
- Ollama has no authentication here; a remote server must be protected by the network or a proxy.
- Worker threads cannot be cancelled: a call that outlives the agent timeout ends at its own 50 s
  deadline.
