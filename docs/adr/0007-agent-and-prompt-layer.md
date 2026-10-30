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
The prompt version is stored in `model_version`. Rule 8 states the output length limits (a live
run showed 44% of calls rejected for length, ADR 0016) and says brevity never outranks the caveat
rules. **The prompt limits are deliberately stricter than the schema limits (owner decision,
2026-10-25):** summary 800, rationale 1500, risk factor / conflicting signal / uncertainty 200, at
most 6 items each in the prompt, against 1000, 2000, 500 and 10 in the schema, so a reply that
follows the prompt always validates. The short-text schema limit was raised from 300 to 500 after
the ADR 0025 item 6 live runs rejected a 302-character item (the prompt still asks for 200); see
ADR 0025. An over-long reply is refused, never trimmed. Rule 2 (prompt `2026-10-13.1`) bars a hazard from general knowledge that no input mentions
(a live run added "heat stress" and "eddies"), allows interpreting cited values, and tells the
model to state the gap instead. Not solved: nothing checks that a reply keeps its caveats or
invents no hazard, and a lower rejection rate is not evidence that it does. Evidence text can
also claim to cover a hazard; the prompt says such claims are data, but nothing enforces it
(`reconcile` still only tightens). Prompt `2026-10-30.1` adds a warning-applicability ban (trusted
wording, present when a warning item is) to the scenario, risk and cross-examination prompts,
matching the weather specialist's focus text; see [tot-design.md](../tot-design.md). Nothing
checks a reply obeys it. A safety test pins the system prompt and the trusted notes (`TIDE_NOTE`,
`WARNING_NOTE`) to `PROMPT_VERSION`; the per-task instruction text is not pinned, and the output
cap is a setting (`DIVESAFE_LLM_MAX_OUTPUT_TOKENS`, ADR 0016), not fixed at 1,500.

**Self-estimates are not confidence (owner choice, 2026-10-26; Code Review finding 3).** A
scenario's or finding's `confidence` is the model's own uncalibrated number. The stored domain
fields (`Scenario.confidence`, `Finding.confidence`), the stored JSON, the prompts and what
PostgreSQL holds are unchanged; only the API views rename it to `uncalibrated_self_estimate` (same
value and 0..1 bounds). A future rename of the stored field would need an old-row plan: the
omit-defaults rule of ADR 0012 does not cover renames.

**Plain text only.** `explanation`, findings, scenarios and rationales are untrusted LLM prose.
Clients must render them as plain text (no HTML or markdown), and they must never be ingested into
the RAG corpus, `HISTORICAL_OBSERVATIONS` or any Learn-stage label. Learn uses the deterministic
result and the human decision.

**Data egress.** `LLMProvider.external` marks a server whose address is not on this machine, and `build_provider` refuses one unless
`DIVESAFE_ALLOW_EXTERNAL_LLM=true` (amended by ADR 0023: a loopback server also needs
`DIVESAFE_LLM_SERVER_IS_LOCAL=true`, an operator statement the software cannot check, or that opt-in). Adapters must never log an `LLMRequest` (it holds the whole
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
