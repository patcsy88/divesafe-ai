# ADR 0018: OpenAI-compatible adapter

Status: accepted

## Context

ADR 0016 built the Ollama adapter. A slow single-GPU Ollama server made a seven-case run take
52 minutes. vLLM, the llama.cpp server and LM Studio expose the same `/chat/completions` protocol,
and many hosted services do too. One adapter for that protocol covers all of them, so no vendor SDK
and no new dependency is needed (standard library only, as in ADR 0016).

## Decision

- **Provider `openai_compatible`** (`divesafe.models.openai_compatible`), over
  `<DIVESAFE_LLM_BASE_URL>/chat/completions`. The base URL is required and must include any path
  prefix such as `/v1`. The model name is required (`DIVESAFE_LLM_MODEL`).
- **Egress rules.**
  - A loopback host (`localhost`, `127.0.0.0/8`, `::1`) may use http or https and is not external.
    Amended by ADR 0023: it is still refused at start-up unless `DIVESAFE_LLM_SERVER_IS_LOCAL=true`
    or `DIVESAFE_ALLOW_EXTERNAL_LLM=true`, and the peer-address check now covers https too.
  - Any other host must use **https** and be listed by exact name in `DIVESAFE_LLM_ALLOWED_HOSTS`
    (no wildcards, IP addresses, all-digit names, schemes or paths). There is no plain-http
    exception for this adapter. A listed host is external and needs
    `DIVESAFE_ALLOW_EXTERNAL_LLM=true`.
  - Base URLs with credentials, a query, a fragment or an invalid port are refused. A look-alike
    such as `localhost.evil.example` or a subdomain of a listed host is not accepted.
  - TLS verification is never disabled; the adapter passes its own default SSL context (certificate
    and host name required) instead of relying on the process default.
  - `localhost` is resolved by the system, so over plain http the connection itself is checked:
    after connecting, the peer address must be loopback, or the call stops before anything (prompt,
    key) is sent.
- **The API key** (`DIVESAFE_LLM_API_KEY`, a `SecretStr`) is optional (local servers usually need
  none). It must be printable ASCII with no whitespace (no header injection) and is sent only as
  `Authorization: Bearer`, never in the body, a URL, an error or the provider's `repr`. Redirects
  are refused, so the key cannot be forwarded to another host. Environment proxies are ignored.
- **Safe call.** The same limits as the Ollama adapter: 1 MiB response cap, `read1` with a total
  deadline, the provider's own thread pool, per-provider concurrency, and fixed-string errors with
  no prompt, response, URL or key. The time budgets and their checks are shared (ADR 0016); the
  runner reads `call_timeout_seconds` and `stage_timeout_seconds` from the provider.
- **Streaming and strict acceptance.** The reply is streamed (server-sent events). It is accepted
  only if all hold: every event is a `data:` line (blank lines and `:` comments are allowed); no
  event carries an `error`; no delta carries `tool_calls`, `function_call` or a `refusal`; there is one choice, index 0; content is a string; the choice ends with
  `finish_reason` "stop" (so "length", "content_filter" and "tool_calls" are refused); nothing
  follows that choice; the stream ends with `[DONE]` and nothing follows it; at most 20,000 lines.
  Lines split only on `\\r\\n`, `\\r` and `\\n`, as the SSE format says, so a Unicode line separator
  inside content is not a break.
  A usage-only event (empty `choices`) is allowed. Anything else is refused, never repaired.
  Reasoning fields are ignored and never used.
- **Structured output** is requested with `response_format` of type `json_schema`. Servers differ
  in how they honour it, so the output still passes `call_structured` (strict schema, cited ids
  must exist); an unsupported server gives a refused reply, which is an issue, not a result.
- **Authority is unchanged.** The adapter adds no trust. A failure leaves the deterministic result
  and `reconcile` can only tighten.

## What a hosted service sees

Everything in the prompt: the dive plan fields and the evidence items (source, values, warning
text) of the assessment. Not identities or decision text (the prompt layer never includes them).
Retention and training terms differ by vendor and must be checked before listing a host. Listing a
host and setting the opt-in is the operator's explicit acceptance.

## Consequences

- Hosts are chosen by configuration, not by code: no new adapter per vendor for the protocol, but a
  service that is "almost compatible" (different stream fields, tool-call quirks) is refused
  rather than adapted.
- The `openai` and `anthropic` provider names remain unbuilt; Anthropic's native API is not
  OpenAI-compatible and needs its own ADR.

## Not solved

- Not run against any real server yet. The stream format was written from the public protocol and
  tested against a local fake server, so real servers may differ in ways only a live run shows.
- Settings errors no longer carry the submitted values (`hide_input_in_errors`), but
  `ValidationError.errors()` still holds the input unless the caller drops it. Nothing in `src`
  logs a Settings error's details.
- The deadline is checked between body reads. Connecting, the TLS handshake and the header lines each
  get a fresh first-byte timeout, so a server that drips headers can hold a worker for longer than
  the call budget (a watchdog that closes the connection would fix it). The agent stage still ends
  on time and the result stays deterministic.
- The allow-list ignores the port: any port on a listed host is allowed. `DIVESAFE_LLM_ALLOWED_HOSTS`
  applies to this adapter only: a non-loopback host must be listed, on top of https and the
  external opt-in. (It briefly covered the Ollama adapter too, until that adapter was removed.)
- The response `Content-Type` is not checked.
- The allow-list is checked at construction. DNS can still point a listed name anywhere; this is
  the same trust as for any hostname allow-list.
- A key is not rotated or scoped by the app. No per-host key: one key is sent to the one base URL.
- The injection screen, the web timeout coupling and the missing server-wide cap on assessments are
  unchanged (ADR 0016).
- Hosted-service data terms are not reviewed here.

## Amendment (2026-10-16): token-limit stops

A `finish_reason` of "length" now raises `OpenAICompatibleTokenLimitError` (also a `TokenLimitError`,
fixed message). The reply is still refused and the agent issue reads
`<task>: output stopped at the token limit`. The classification is what the server reported, and it
is raised as soon as "length" is seen, so a malformed event after it is also labelled this way.
Other non-"stop" reasons still raise the generic error. See ADR 0016.

## Amendment (2026-11-15): one log line per model call

Owner decision (2026-11-15), so that `DIVESAFE_LLM_MAX_OUTPUT_TOKENS` is chosen from measured reply
sizes and not guessed. In production, 2 of about 72 calls stopped at the limit of 3000, both in
one run (ADR 0024 A8), and nothing recorded how long the other replies were.

- **What is logged:** `call_structured` (`agents/runner.py`) logs exactly one INFO line,
  "model call finished", per model call, with only these `extra` fields:
  - `task`, the fixed task name, such as `specialist:weather`;
  - `outcome`, one of `ok`, `token_limit`, `timed_out`, `provider_error`, `too_long`, `schema`,
    `unknown_evidence_id`, `cancelled` (the call was cancelled, for example by the stage deadline
    or a shutdown) or `error` (anything else unexpected, which still propagates unchanged);
  - `output_limit`, the output-token limit sent with the request;
  - `output_chars`, the reply's length in characters (on a token-limit stop, the characters
    received before the stop);
  - `output_used`, the server's `usage.completion_tokens`, in tokens;
  - `duration_ms`, from a monotonic clock.

  Counts are plain non-negative ints up to `MAX_LOGGED_COUNT` (10^9), or `null`. A larger
  server count is `null`, and the reply is still judged as before. A prompt refused as too
  large before any call logs nothing.
- **Reading the counts:**
  - On a `token_limit` stop, `output_used` is usually `null`. Servers send usage after the
    finish event, and the reply is refused as soon as "length" is seen; `output_chars` is then
    the measure.
  - `completion_tokens` can include a model's hidden reasoning tokens, so `output_used` can be
    far larger than `output_chars` suggests.
  - Calls cut off by the stage deadline log `cancelled` with no counts, and they are probably
    among the longest replies. Count them alongside `token_limit`.
- **Field names:** the two token counts are named without "token" because the JSON formatter
  masks any field whose name matches `config.logging._SENSITIVE`. That rule is unchanged, and a
  test fails if a field is renamed back into it.
- **Never logged:** prompts, reply or partial text, headers, keys, URLs, hosts, evidence ids, plan
  fields or provider error text. A safety test (`tests/safety/test_model_call_log_leaks.py`)
  scans the record and has an in-test mutation check.
- **Usage is read, never trusted:** `join_sse` reads `usage.completion_tokens` from the last event
  whose `usage` is not null, after the event-level checks (JSON, duplicate keys, `error`,
  `choices` shape). Reading it cannot raise, and the count is discarded on any refusal. A
  missing, malformed or oversized count is `null`. A count never changes whether a reply is
  accepted or refused, and every refusal is
  unchanged.
- **Input count (ADR 0027 amendment, step 4, 2026-11-16):** the adapter also reads
  `usage.prompt_tokens` from the same `usage` object, by the same rule, into
  `LLMResponse.input_tokens` (and `TokenLimitError.input_tokens` on a "length" stop). It goes to
  the run's trace only, as `input_used`; it is never on the log line, and `CALL_LOG_FIELDS` is
  unchanged. Unlike `output_tokens`, the field maps any value `plain_count` rejects to `null`, so
  building a response never fails on it.
- **Logging cannot change a result:** a failure to read a count gives `null`, and a logging error
  is dropped. The line holds only counts: no reply, partial reply or error object is kept for
  it. Agent issues, records, error strings, `kind` and `label` are unchanged. One small
  difference: too-long and unknown-evidence-id errors now have `__suppress_context__` set; their
  `__context__` is `None`, as before.
- **A safety test strengthened (2026-11-15):** the `token_limit` case of
  `tests/safety/test_duplicate_json_keys.py::test_a_provider_error_leaves_no_provider_text_in_the_exception_context`
  raised `TokenLimitError("…")`. The constructor takes no positional argument, so the case
  raised `TypeError` and actually tested the provider-error path. It now raises a real
  `TokenLimitError` and asserts the token-limit message, so it cannot fall through again.
- **`stream_options` is deliberately not sent:** the request body is byte-for-byte unchanged (a
  test pins it). Asking for usage with `stream_options.include_usage` could break calls on a
  server that refuses unknown fields. So `output_used` is `null` when the server does not send
  usage on its own, and `output_chars` is the fallback measure. Whether to ask for usage is a
  separate decision.
- **Production log level:** the line is INFO. Production compose sets `DIVESAFE_LOG_LEVEL: INFO`
  (the settings default too), so it is written. Raising the level to WARNING hides it.
- **Known gaps (review nits, 2026-11-15):**
  - `LLMResponse.output_tokens` is strict (`ge=0`, `le=MAX_LOGGED_COUNT`). An adapter must pass a
    server count through `plain_count` first; the OpenAI-compatible adapter does. A future
    adapter that passed a raw negative, bool or oversized count would raise, so a reply would be
    refused over a number that is only logged. A `BeforeValidator` would close this.
  - `tests/safety/test_model_call_log_leaks.py` sets the mutant's `logger` again although the
    copy already names its own logger. This is harmless.
  - `output_used` is `null` unless the server sends usage without being asked (`stream_options`
    is not sent). Check after the first assessment on the deployed image.

## Amendment (2026-11-16): the schema sent to the host

On 2026-11-16 every model call of two production assessments failed with an HTTP 400 from the
model host: it now refuses `maxItems`, `minimum`/`maximum` and a `minItems` other than 0 or 1 in
`response_format` (synthetic probes; `maxLength`, `minLength`,
`enum`, `anyOf`, `default`, `title` and `description` are accepted). The previous image sent the
same schema, so the deploy of that day was not the cause; the host changed.

- **Decision:** the adapter sends `host_schema(request.json_schema)`: a copy without `maxItems`,
  `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf` and any `minItems`
  other than 0 or 1, walked by JSON Schema structure (`properties`, `$defs`, `definitions`,
  `items`, `prefixItems`, `additionalProperties`, `anyOf`, `allOf`, `oneOf`, `not`), so a property
  that happens to be named `maximum` is kept. It only removes keywords; it never adds one.
- **Acceptance is unchanged.** The request's own schema is not modified, the prompt still states
  the full schema, and `call_structured` still validates the reply against the full Pydantic
  model. A reply over a removed limit (confidence outside 0–1, more than 10 risk factors, more
  than 120 evidence ids) is refused as before; `tests/safety/test_host_schema_acceptance.py`
  pins this and that the host copy is exactly the original minus those keywords.
- **Consequence:** the host's constrained decoding no longer enforces those limits, so a reply
  that breaks one becomes a refused task (an agent issue), not a result. No authority changes.
- **Known gaps:** a host that refuses another keyword fails the same way, visibly, as every call
  a `provider_error`. The walk covers only `properties`, `$defs`, `definitions`, `items` (an
  object), `prefixItems`, `additionalProperties`, `anyOf`, `allOf`, `oneOf` and `not`. A dropped
  keyword under any other location (`patternProperties`, `contains`, `if`/`then`/`else`,
  `dependentSchemas`, a list `items`) is sent as is. Today's outputs use none of them, and such a
  keyword fails as a visible `provider_error`, never as an acceptance.
  The run logs and the record do not show the adapter's `http_error` label or the host's
  message, which made this slow to diagnose (ADR 0027's observability work). A fix surfaces the
  fixed label only, never the host's error body, which can echo the request and so the prompt.
