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
  does not apply to the Ollama adapter, which needs only the external opt-in over https.
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
