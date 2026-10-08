# AGENTS.md: DiveSafe AI engineering rules

DiveSafe AI is decision support for recreational diving. Mistakes here can contribute to harm,
so the rules below are strict. Read `docs/architecture.md` before changing design.

## Non-negotiable safety rules

1. **Hierarchy:** deterministic rules > validated data > risk model > agent reasoning > LLM
   explanation. A lower layer may only keep or tighten a higher layer's result.
2. **Authority:** `divesafe.risk.reconcile` is the only place a final recommendation is
   produced. Agent and LLM output is a *proposal*; it can never relax a deterministic result.
3. **Uncertainty:** missing, stale or contradictory required evidence yields
   `INSUFFICIENT EVIDENCE`. Never fill gaps with assumptions or invented values.
4. **Human decides:** the system recommends; a human accepts or overrides. Every override
   needs a rationale and is recorded.
5. **No invented thresholds:** do not hard-code numeric safety limits (wave height, current
   speed, visibility, etc.) from memory. Thresholds live in versioned, reviewed rule
   definitions with a cited source (`docs/risk-model.md`).
6. **No invented data sources:** do not add an API connector, endpoint or dataset unless it is
   documented in `docs/data-sources.md` and verified. No fabricated sample data in production
   paths; test fixtures must be clearly labelled as such.
7. **Auditability:** reasoning output is a concise summary with evidence IDs, never a raw
   chain-of-thought. Every assessment is stored as an `AssessmentRecord`.
8. **Tree-of-Thought is bounded** (fixed scenarios, fixed depth) and can never determine
   safety on its own (`docs/tot-design.md`).
9. **Live conditions come from structured sources**, not RAG. RAG is for contextual knowledge
   (`docs/rag-design.md`).

Any change touching `src/divesafe/risk/`, `src/divesafe/domain/` or `tests/safety/` needs the
`risk-reviewer` agent's review. Never weaken or delete a safety test to make a change pass.

## Architecture at a glance

```
Observe -> Retrieve -> Diagnose/Reason -> Assess Risk -> Propose -> Human Gate -> Validate -> Learn
```

| Package | Responsibility |
| --- | --- |
| `domain` | Pydantic types: evidence, plan, recommendation, scenario, audit record. Imports nothing else from the package. |
| `risk` | Deterministic rules engine and `reconcile`. Pure, no I/O, no LLM. |
| `data` | Environmental connectors returning validated `EvidenceItem`s. |
| `rag` | Contextual knowledge retrieval (pgvector). |
| `agents` | Specialist agents producing evidence-referenced findings. |
| `orchestration` | Runs the lifecycle and enforces the human gate. |
| `models` | `LLMProvider` abstraction; provider adapters register here. |
| `services` | Provenance, history, feedback/learning, persistence. |
| `api` | FastAPI app and human decision interface. |
| `config` | `Settings` from `DIVESAFE_*` env vars; JSON logging. |

Dependency direction: `api -> orchestration -> agents/rag/data/risk/services -> domain`.
`risk` and `domain` must not import `agents`, `rag`, `models` or `api`.

## Conventions

- Python >= 3.11, fully typed (`mypy --strict` on `src`), Pydantic v2 models for all data
  crossing a boundary, `frozen=True` for domain values.
- Config only via `Settings` / environment. Never read `os.environ` ad hoc, never commit
  secrets, never log secrets or API keys. Use `SecretStr`.
- Code against `LLMProvider`, never a vendor SDK, outside `models/` adapters.
- Logging: `logging.getLogger(__name__)` with `extra={...}` fields; no `print`.
- Keep it simple. No new dependency or framework without an ADR in `docs/adr/`.
- Comments only where behavior is non-obvious. Document decisions in ADRs, not code.

## Commands

```bash
pip install -e ".[dev]"
pytest                    # everything
pytest -m safety          # must always pass; never skip
ruff check . && ruff format --check .
mypy
```

Tests live in `tests/{unit,integration,agent,safety,evaluation}`. LLM calls in tests use
`FakeProvider`; no test may call a live LLM or external API unless marked `integration`.

## Definition of done

- Tests added or updated; `pytest`, `ruff`, `mypy` pass.
- Safety tests unchanged or strengthened.
- Docs/ADR updated when behavior or architecture changes.
- No secrets, no invented data sources or thresholds.

## Specialist agents (`.opencode/agents/`)

`architect`, `ai-engineer`, `data-engineer`, `backend-engineer`, `test-engineer`,
`risk-reviewer`, `security-reviewer`, `code-reviewer`. Reviewers are read-only.
