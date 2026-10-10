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

## Deterministic, probabilistic and human responsibilities

| | DETERMINISTIC | PROBABILISTIC / AI | HUMAN |
| --- | --- | --- | --- |
| **What** | Validation, rules, hard constraints, calculations | Interpretation, prediction, scenario reasoning, explanation | The final decision |
| **Code** | `domain` (validators), `data` (connectors), `risk` (engine, `reconcile`), `services` | `agents` (specialists, bounded ToT, risk proposal), `rag` (not built) | `orchestration.human_gate`, `api` decision endpoints |
| **Authority** | Authoritative; the only source of `final_recommendation` | Advisory; a proposal that can only tighten, never relax | Accepts or overrides with rationale (ADR 0004) |
| **Reproducible** | Yes, same input gives same output | No; stored with model and prompt versions | n/a; recorded with identity and time |
| **Failure** | Fails closed to `INSUFFICIENT EVIDENCE` | Dropped and reported; deterministic result stands | n/a |
| **Examples** | Unit and range checks, freshness, `WarningNeedsHumanReadingRule`, placeholder rules | Specialist findings, three scenarios, explanation | Diver, dive master or dive leader |

Nothing in the AI column can change a result in the deterministic column; nothing is final until
the human column acts.

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

## Domain model

`divesafe.domain` holds frozen Pydantic types and imports nothing else from the package. The
project's concept names map onto it like this:

| Concept | Type |
| --- | --- |
| DivePlan | `DivePlan` |
| DiveSite | `DiveSite` (+ `SiteConstraint`, all `TBD` until validated) |
| WeatherConditions, WaveConditions, OceanConditions, TidalConditions | typed conditions in `domain.conditions` (physical plausibility only, `None` = not available) |
| EnvironmentalObservation | conditions plus the evidence id they came from; `observation_from_evidence` |
| MarineWarning | the warning as published; applicability never decided |
| Evidence | `EvidenceItem` (alias `Evidence`) with the provenance fields below |
| RiskFactor | `RuleResult` (alias `RiskFactor`), tagged with a `RiskFactorKind` |
| RiskAssessment | the engine's output; recommendation must equal the most severe result |
| ConfidenceAssessment | all `None` by default; a number needs a documented method (shape only; the record enforces `confidence_method` itself) |
| Recommendation | `GO`, `CAUTION`, `NO-GO`, `INSUFFICIENT EVIDENCE` |
| HumanDecision | decider, decision, time, derived `is_override`, rationale |
| PostDiveObservation | `ActualConditions` (alias `PostDiveObservation`) |

## Data provenance

Every `EvidenceItem` carries: `source` and `source_version`; `location` (`GeoPoint`, the data's
location such as the model grid cell); `data_kind` (observation, forecast, model, prediction,
notice, knowledge) with `is_forecast` forced to agree; `retrieved_at`; the validity window
(`valid_at` to `valid_until`); `quality` (default `unassessed`, `rejected` data is refused) with
`quality_notes`; and `transformations`, the steps from payload to stored value. The marine
connector records unit and range checks and the grid snap; the warnings connector records the
timezone inference. Provenance is stored in the audit record with the evidence.

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

## Rule definitions

Signed-off limits are data (`domain.RuleDefinition`, ADR 0009), applied by `risk.DefinitionRule`,
expired by the engine and loaded from a reviewed file by `orchestration.load_ruleset`
(`DIVESAFE_RULESET_PATH`; any refusal stops startup). The repository ships none.

## Known gaps

Tracked items not yet built (each needs an ADR or design before implementation):

- **Durable storage:** PostgreSQL adapter built and tested ([ADR 0012](adr/0012-postgresql-assessment-repository.md)).
  Still open: a migration tool, record hashing for tamper evidence, connection pooling.
- **Roles and authorization:** API keys identify an actor but grant no roles (a production gate;
  see ADR 0006). Who may override,
  and whether a less severe override needs stronger authority, is not enforced. Key rotation and
  revocation are manual (edit the configured hashes).
- **TLS and deployment hardening:** the app speaks plain HTTP; run it behind a TLS terminator.
  No request-size or per-client rate limits beyond the provider-limit guard.
- **Real LLM adapters and their evaluation:** the prompt layer, strict output validation and the
  egress flag exist (ADR 0007), but no adapter does, and real-model behaviour is unmeasured.
  Each adapter needs an ADR for its dependency and a documented note on what data the vendor sees.
- **Contradiction detection and validity-window coverage** (see [risk-model.md](risk-model.md)).
- **Supply chain:** pinned image digests and a lock file; dependency audit in CI.
- **Per-category evidence quality policy and engine-level validity-window coverage**
  (see risk-model.md).
- **Rate limiting is per process**, not shared across workers.
- **OpenCode permissions and the `rtk` plugin:** a global `rtk` plugin rewrites `git ...` to
  `rtk git ...` before permissions are checked, so `opencode.jsonc` and the code-reviewer carry
  `rtk git ...` variants, including a deny for `rtk git push`. Verified by running the reviewer:
  `git diff`, `git log` and `mypy` run; `git push`, `rm` and edits are denied; bare
  `git status` is still denied for unknown reasons (use `git diff --staged`). Without the plugin
  the extra rules are harmless. Re-verify after changing the plugin or OpenCode.
