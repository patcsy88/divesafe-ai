# ADR 0002: LLM provider abstraction

- Status: Accepted
- Date: 2026-10-09

## Context

The project must switch between OpenAI, Anthropic, Ollama/local and other compatible
providers, and results must not depend on one vendor.

## Decision

Agents depend on the `LLMProvider` protocol (`divesafe.models.llm`): an async `complete`
taking a provider-neutral `LLMRequest` and returning an `LLMResponse` carrying provider and model.
Adapters register by name via `register_provider`; `create_provider(settings)` selects one from
`DIVESAFE_LLM_PROVIDER`. A `FakeProvider` supports tests and offline work. No vendor SDK is a
dependency yet; each adapter adds its own optional dependency when implemented.

## Consequences

- Swapping providers is a configuration change.
- Safety behavior must be identical across providers because it does not depend on the LLM.
- Provider and model are recorded in each `AssessmentRecord`.
- Provider-specific features (tool calling, structured output) must be mapped to neutral
  request fields or not used.
