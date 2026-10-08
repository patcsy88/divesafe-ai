# ADR 0007: Agent stage, bounded Tree-of-Thought and the prompt layer

- Status: Accepted
- Date: 2026-10-09

## Context

Agents interpret evidence and compare scenarios (docs/agent-design.md, docs/tot-design.md), but
LLM output must never relax a deterministic result (ADR 0001), and much of the text an LLM reads
(MET Malaysia warning text, later RAG documents) is untrusted. Only a fake provider exists, so
nothing here has been run against a real model.

## Decision

**Calls and bounds.** `run_agent_stage` makes at most 9 provider calls per assessment: five
specialists (weather, ocean conditions, tide/current, site intelligence, prediction), three
scenarios (favourable, marginal, deteriorating; one level, kind assigned by code) and one risk
proposal. Each call has a 60 s timeout, a 1,500-token output cap, temperature 0, at most 120
evidence items and a 200,000-character prompt budget. The whole stage has a 150 s deadline (the
sequence specialists, scenarios, risk call could otherwise take about 180 s); on expiry there is
no proposal. `now` and freshness are captured before the stage runs. A specialist with no evidence in its categories makes no call and returns an
explicit `no_evidence` finding.

**Validation, never repair.** Outputs must be one JSON object matching a strict Pydantic schema
(unknown fields rejected, so a reasoning trace cannot be stored; length caps on all text). Every
cited evidence id must be in that call's allowed set and at least one must be cited. Any
failure raises `AgentError`, is recorded in `agent_issues`, and that output is discarded. Provider
error text is never propagated or stored.

**Authority.** The risk agent returns a proposal. `reconcile` keeps the more severe of it and the
deterministic result, flags a less severe proposal (`llm_attempted_downgrade`), and is the only
source of `final_recommendation`. When the stage fails or is skipped, there is no proposal and the
deterministic result stands alone. Explanation, findings and scenarios are stored for review. They influence a
decision only through the risk agent's proposal, which `reconcile` can only tighten (a second-order
injection path: hostile text, then a finding, then a proposal; it can add caution, never relax).
A proposal less severe than the rules is discarded together with its rationale, which is replaced
by a fixed notice; the cited evidence ids are stored (`proposal_evidence_ids`). The API reports
`llm_tightened` and says "Rules alone: X; an unverified LLM proposal raised this to Y".

**Prompt layer.** `divesafe.agents.prompts` is the only place untrusted text enters a prompt.
Evidence, specialist findings (LLM-derived) and rule rationales go inside one
`<untrusted_data>` block as ASCII-only JSON with `<`, `>` and `&` escaped, long strings
truncated at 4,000 characters. The plan and the allowed evidence ids sit outside it. The system
prompt states the data-not-instructions rule, the citation rule, and that rules are authoritative.
The prompt version is stored in `model_version`.

**Plain text only.** `explanation`, findings, scenarios and rationales are untrusted LLM prose.
Clients must render them as plain text (no HTML or markdown), and they must never be ingested into
the RAG corpus, `HISTORICAL_OBSERVATIONS` or any Learn-stage label. Learn uses the deterministic
result and the human decision.

**Data egress.** `LLMProvider.external` marks hosted providers, and `build_provider` refuses one unless
`DIVESAFE_ALLOW_EXTERNAL_LLM=true`. Adapters must never log an `LLMRequest` (it holds the whole
prompt) or any API key. The agent layer sends only
public evidence and plan fields (site id, time, duration, depth): no actor identity, assessment
id, rationale or actual conditions. A configured provider with no adapter fails at startup; the
`fake` setting disables the stage rather than producing empty agent output.

## Consequences

- A hostile or broken model can only add caution or add noise; it cannot lower severity or
  corrupt the record.
- Prompt-injection defence is structural (delimiting, escaping, schema, ids, authority), not
  proof against a model that follows injected text; the authority design is what bounds the
  damage. Real-model behaviour is unknown until an adapter exists and is evaluated.
- The explanation is LLM prose and can be wrong or persuasive; the API labels it as such.
- Fixed bounds mean long dive windows (roughly over 35 hours: three hourly categories per hour
  against a 120-item cap) skip scenarios and the risk proposal until evidence is summarised.
- Findings omit uncalibrated self-confidence when fed back to later agents. Validation checks that
  each output cites at least one valid evidence id; it does not bind ids to individual claims or
  detect invented numbers in prose.
