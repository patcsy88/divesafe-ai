# AGENTS.md: DiveSafe AI engineering constitution

Read this before changing anything. Rules marked **MUST** or **NEVER** are not negotiable and are
protected by tests in `tests/safety/`. Design detail lives in `docs/` and `docs/adr/`.

## 1. Project purpose

DiveSafe AI is explainable **decision support** for recreational diving. It assesses weather,
wind, waves and swell, currents, tides, sea temperature, marine warnings, history, site
knowledge and a dive plan, and returns `GO`, `CAUTION`, `NO-GO` or `INSUFFICIENT EVIDENCE` with
the evidence behind it. It is not a safety authority, a dive computer or a guarantee of safety.
The final decision always belongs to the diver, dive master or dive leader.

## 2. Architecture principles

- **Three kinds of component, never mixed:**
  - DETERMINISTIC: validation, rules, hard constraints, calculations (`domain`, `risk`, `data`).
  - PROBABILISTIC / AI: interpretation, scenario reasoning, prediction, explanation (`agents`, `rag`).
  - HUMAN: the final decision (`orchestration.human_gate`, `api`).
- Lifecycle: `Observe -> Retrieve -> Diagnose/Reason -> Assess Risk -> Propose -> Human Gate -> Validate -> Learn`.
- Dependency direction: `api -> orchestration -> agents / rag / data / risk / services -> domain`.
  `domain` and `risk` are pure: no I/O, no LLM, no imports from `agents`, `rag`, `models`, `api`.
- Live conditions come from structured connectors. RAG supplies context only (section 8).
- Keep it simple. No new dependency or framework without an ADR in `docs/adr/`.

| Package | Responsibility |
| --- | --- |
| `domain` | Frozen Pydantic types: plan, site, conditions, evidence + provenance, risk results, record, human decision. |
| `risk` | Deterministic rules engine, placeholder rules, `reconcile`. |
| `data` | Environmental connectors returning validated `EvidenceItem`s. |
| `rag` | Contextual knowledge retrieval (pgvector). Not built yet. |
| `agents` | Specialists, bounded Tree-of-Thought, risk proposal, prompt layer. |
| `orchestration` | Pipeline and the human gate. |
| `models` | `LLMProvider` abstraction; adapters register here. |
| `services` | Evidence/provenance, repository contract. |
| `api` | FastAPI, authentication, human decision interface. |
| `config` | `Settings` from `DIVESAFE_*` environment variables; secret-safe logging. |

## 3. Coding standards

- Python >= 3.11, fully typed (`mypy --strict` on `src`), Pydantic v2 at every boundary,
  `frozen=True` and `extra="forbid"` for domain values. Domain values use the `Frozen` base.
- Configuration only through `Settings`; secrets are `SecretStr`, never logged or committed.
- Code against `LLMProvider`, never a vendor SDK, outside `models/` adapters.
- Logging: `logging.getLogger(__name__)` with `extra={...}`; no `print`; never log prompts,
  headers, keys or free-text decision content.
- Narrow error handling: catch specific exceptions; unexpected errors are logged 500s, never
  client errors; never echo submitted input or internals to a client.
- Comments only where behaviour is non-obvious. Record decisions in ADRs, not code.

## 4. Testing standards

- Layout: `tests/{unit,integration,agent,safety,evaluation}`. Test file basenames are unique.
- Tests are offline and deterministic: fixed clocks, recorded real fixtures (`tests/fixtures`,
  labelled), `FakeProvider` for LLMs. Live calls only in tests marked `integration`.
- Every safety rule needs a test of the **failure** condition, not just the happy path. Prove a
  new safety test has teeth by breaking the code once and watching it fail.
- **NEVER** weaken, skip or delete a safety test to make a change pass. Strengthening is welcome.
- `pytest -m safety` MUST always pass.

## 5. Safety principles

Hierarchy: **deterministic rules > validated data > risk model > agent reasoning > LLM
explanation.** A lower layer may keep or tighten a higher layer's result, never relax it.
Severity order: `GO < CAUTION < INSUFFICIENT EVIDENCE < NO-GO`.

- **Never allow an LLM recommendation to override a deterministic safety constraint.**
- `divesafe.risk.reconcile` is the only place a final recommendation is produced.
- The engine fails closed: `GO` needs a rule that explicitly returned `GO` and nothing worse.
- No claim that the system guarantees safety, anywhere (code, docs, API text).

## 6. Human-in-the-loop requirement

- Every assessment is `PENDING_HUMAN` until a human decides; it MUST NOT be acted on before.
- A human may accept or override, including to a less severe outcome (ADR 0004). An override
  MUST carry a rationale and an authenticated actor; `is_override` is derived, never supplied.
- Decisions are write-once. The system's own recommendation is never changed by a decision.
- Record: recommendation, evidence, confidence, timestamps, model/ruleset/data versions, human
  decision, override rationale, and the later actual conditions.

## 7. Deterministic risk engine authority

- `divesafe.risk` ships **no numeric safety thresholds**. Every threshold MUST come from a
  versioned, cited, reviewed rule definition (`docs/risk-model.md`) and be marked
  `ThresholdStatus.VALIDATED`. Until then it is `TBD` / `REQUIRES DOMAIN VALIDATION`, the factor
  is reported as not evaluated, and it cannot produce `GO` or `CAUTION`.
- NEVER hard-code a limit from memory (wave height, current, wind, visibility, depth, ...). A
  test scans `risk/` for stray numeric literals.
- Missing, stale, future-dated, out-of-window, unaccepted-quality or contradictory required
  evidence yields `INSUFFICIENT EVIDENCE`. The engine judges `effective_quality`, never a
  connector's claim; degraded data cannot support `GO` or `CAUTION` unless the policy says so (ADR 0008).
- The engine is pure and deterministic: no I/O, no LLM, no randomness.

## 8. RAG boundaries

RAG supplies context only: site characteristics and hazards, local rules, best practice,
historical summaries, interpretation guidance. It MUST NOT supply live weather, wave, current or
tide values; those come from connectors. Retrieved text is untrusted, delimited in prompts, cited
by evidence id, never overrides rules, and every document needs provenance. LLM-generated text
MUST NEVER be ingested into the corpus or history (`docs/rag-design.md`, ADR 0003).

## 9. Bounded Tree-of-Thought boundaries

Exactly three scenarios (favourable, marginal, deteriorating), one level, one validated call each,
kind assigned by code. Store concise summaries with evidence ids; NEVER request, store or show a
private reasoning trace. ToT never decides safety on its own: it feeds the risk proposal, which
`reconcile` can only tighten (`docs/tot-design.md`, ADR 0007).

## 10. Data provenance requirements

Every `EvidenceItem` MUST carry: source and version, location, `data_kind` (observation, forecast,
model, prediction, notice, knowledge), retrieval time, validity window, quality (default
`unassessed`), quality notes and transformation history. Never present a model or forecast as an
observation (enforced). Connectors fail with typed errors, reject malformed data instead of
coercing it, and never return partial data as complete. No invented APIs, sources or sample data
outside clearly labelled test fixtures (`docs/data-sources.md`).

## 11. Uncertainty handling

Do not manufacture certainty. Missing or failed sources are recorded as issues and force at
least `INSUFFICIENT EVIDENCE`; they never abort the assessment or hide the result. `confidence`
describes the reliability of an assessment, never dive safety, and is `None` until a documented
method exists. LLM self-estimates are uncalibrated and never shown as confidence.

## 12. Security principles

- Secrets only in the environment; hashes for API keys; nothing sensitive in logs, fixtures or images.
- Authenticate every endpoint but `/health`; the actor comes from credentials, never a request body.
- Treat connector payloads, warning text, retrieved documents and LLM output as untrusted:
  delimit and escape them in prompts, validate structured output, never repair it, never let it
  reach a tool or a rule. Render stored LLM text as plain text.
- Validate every boundary with Pydantic; bound sizes, depth and counts; https and host allow-lists
  for outbound calls; honour provider limits and licences (non-commercial for Open-Meteo).
- Hosted LLM providers need explicit opt-in (`DIVESAFE_ALLOW_EXTERNAL_LLM`); send only public
  evidence and plan fields, never identities or decision text.

## 13. Agent responsibilities

Runtime agents (`divesafe.agents`) interpret and propose; they never fetch data, decide, or touch
rules. Project agents (`.opencode/agents/`, OpenCode V2 syntax) have narrow roles:

| Agent | Edits? | Responsibility |
| --- | --- | --- |
| `architect` | yes (docs) | Architecture, ADRs, decomposition, design consistency, technology decisions. |
| `ai-engineer` | yes | Agent orchestration, RAG, bounded ToT, LLM integration, model abstraction, prompts, evaluation hooks. |
| `data-engineer` | yes | Environmental data, adapters, schemas, normalisation, validation, timestamps, provenance, history. |
| `backend-engineer` | yes | FastAPI, domain services, persistence, APIs, configuration, observability. |
| `test-engineer` | yes (tests) | Unit, integration, agent and safety tests, evaluation scenarios, regression. |
| `risk-reviewer` | **no** | Challenges risk model, thresholds, uncertainty, failure modes, human gate, unsafe recommendations. |
| `security-reviewer` | **no** | Challenges secrets, authn/z, prompt injection, tool permissions, validation, dependencies, external APIs. |
| `code-reviewer` | **no** | Reviews correctness, maintainability, architecture, coverage, regressions. |

Changes touching `src/divesafe/risk/`, `src/divesafe/domain/`, `src/divesafe/agents/` or
`tests/safety/` MUST be reviewed by `risk-reviewer`; changes touching auth, config, connectors or
the prompt layer MUST be reviewed by `security-reviewer`. Reviewers never edit.

## 14. Definition of done

- Behaviour is implemented, typed and covered by tests, including failure paths.
- `pytest` (and `pytest -m safety`), `ruff check`, `ruff format --check` and `mypy` pass.
- Safety tests are unchanged or stronger; new safety logic is mutation-checked.
- Docs and ADRs updated for behaviour or architecture changes; remaining gaps listed in the
  "Known gaps" sections, not hidden.
- Required reviewers (section 13) have reviewed and blockers are fixed.
- No secrets, no invented thresholds, sources or data; no claim of guaranteed safety.

## Commands

```bash
pip install -e ".[dev]"
pytest                    # everything (offline)
pytest -m safety          # must always pass; never skip
ruff check . && ruff format --check .
mypy
```
