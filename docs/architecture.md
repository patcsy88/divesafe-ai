# Architecture

## Safety hierarchy

| Rank | Layer | May do |
| --- | --- | --- |
| 1 | Deterministic safety rules | Set hard constraints. Authoritative. |
| 2 | Validated environmental data | Supply evidence; invalid or stale data is rejected. |
| 3 | Risk model | Estimate risk and confidence within rule limits. |
| 4 | Agent reasoning | Interpret evidence, compare scenarios, propose. |
| 5 | LLM explanation | Explain in prose. Never changes the outcome. |

Severity order: `GO < CAUTION < INSUFFICIENT EVIDENCE < NO-GO`.
`reconcile(deterministic, proposed)` returns the more severe of the two, so no proposal can
relax a deterministic result. If a proposal is less severe than the deterministic result it is
discarded and flagged (`llm_attempted_downgrade`) in the audit record. The engine fails
closed (no applicable rule is `INSUFFICIENT EVIDENCE`, never `GO`). Consistency is enforced
in `divesafe.risk.engine` and by `AssessmentRecord` validation, and tested in `tests/safety`.
Evidence value validation, contradiction detection and ToT guardrails are planned
(see Known gaps).

## Components

```
DATA SOURCES            weather | ocean | waves/swell | tides/currents |
                        marine warnings | historical observations | bathymetry/site info
        |
ENVIRONMENTAL DATA CONNECTORS  -> validated EvidenceItem (with provenance)
        |
PLATFORM SERVICES       Evidence/Provenance | Historical data | RAG + vector store (pgvector)
                        Risk Rules Engine | Risk Model | Feedback/Learning
        |
AGENTS                  Site Intelligence | Weather | Ocean Conditions | Tide/Current |
                        Risk Assessment | Prediction
        |
AGENT ORCHESTRATOR      runs the lifecycle, bounded ToT, enforces the human gate
        |
APPLICATION             Dive Assessment API (FastAPI) | Human decision interface
```

## Agentic lifecycle

| Stage | What happens | Primary components |
| --- | --- | --- |
| Observe | Receive the dive plan; fetch live conditions as structured evidence. | Connectors, Provenance |
| Retrieve | Fetch site knowledge, guidance, history. | RAG, Historical data |
| Diagnose / Reason | Specialist agents interpret evidence; bounded ToT compares scenarios. | Agents, Orchestrator |
| Assess Risk | Rules engine (authoritative) and risk model score the plan. | Risk Rules Engine, Risk Model |
| Propose | Agents propose a recommendation with evidence IDs, confidence, uncertainty. `reconcile` fixes the final recommendation. | Risk Assessment Agent, `reconcile` |
| Human Gate | Human reviews evidence, risk factors and uncertainty; accepts or overrides with rationale. Nothing is final until this step. | API, Human interface |
| Validate | Check the decision record is complete and consistent; later compare against actual conditions. | Provenance, Feedback |
| Learn | Store actual conditions and outcomes; feed evaluation and calibration. Learning never edits rules automatically. | Feedback/Learning |

## Data flow rules

- Live conditions: structured connectors only. Contextual knowledge: RAG only
  ([rag-design.md](rag-design.md)).
- Everything an agent says about conditions must cite `EvidenceItem` IDs.
- Evidence carries source, version, retrieval time, valid time, and forecast/observation flag.
- Freshness and required categories are enforced by the rules engine's `EvidencePolicy`.

## Audit record

Each assessment persists an `AssessmentRecord` (`divesafe.domain.models`): plan, evidence,
rule results, deterministic / proposed / final recommendation, downgrade flag, confidence,
scenarios, explanation, model / ruleset / data versions, human decision, override rationale,
and later actual conditions.

## Technology

Python 3.11+, FastAPI, Pydantic v2, PostgreSQL (+ pgvector for RAG), pytest, Docker Compose,
stdlib JSON logging, `pydantic-settings` for configuration. LLMs sit behind
`divesafe.models.LLMProvider` ([ADR 0002](adr/0002-llm-provider-abstraction.md)).

## Package dependency direction

`api -> orchestration -> agents / rag / data / risk / services -> domain`

`domain` and `risk` are pure (no I/O, no LLM, no imports from agents, rag, models, api).

## Decisions

See [docs/adr/](adr/).

## Known gaps

Tracked items not yet built (each needs an ADR or design before implementation):

- **Authentication and authorization** for decision/override endpoints. This is a precondition
  for exposing them outside development, because any caller could otherwise override a `NO-GO`;
  `decided_by` must come from the auth context, never the request body; the actor recorded on a
  `HumanDecision` must come from an authenticated identity.
- **Append-only audit storage:** INSERT/SELECT-only DB role, record hashing, tests that updates
  fail.
- **Prompt construction helper** that delimits untrusted retrieved/connector text and validates
  structured output; a data-egress flag on LLM providers and personal-data minimization.
- **Contradiction detection and validity-window coverage**
  (see [risk-model.md](risk-model.md)).
- **Supply chain:** pinned image digests and a lock file; dependency audit in CI.
- **Connector rate limiting, caching and attribution display** (see data-sources.md).
- **Per-category evidence quality policy and engine-level validity-window coverage**
  (see risk-model.md).
