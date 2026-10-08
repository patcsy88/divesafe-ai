# DiveSafe AI

Agentic environmental risk assessment and decision support for recreational diving.

DiveSafe AI combines weather, wind, waves/swell, currents, tides, sea temperature, marine
warnings, historical observations, dive-site knowledge and a user's dive plan into an
**explainable recommendation**: `GO`, `CAUTION`, `NO-GO` or `INSUFFICIENT EVIDENCE`.

> **Decision support only.** The final decision always belongs to the human diver, dive master
> or dive leader. DiveSafe AI is not a safety authority.

## Status

Foundation, data connectors, assessment pipeline, human gate and API. The repository contains the architecture, engineering rules,
the safety-critical core (domain types, fail-closed rules engine with reconciliation, LLM provider
abstraction, config, API skeleton), and two connectors for Pulau Redang, Malaysia (Open-Meteo
marine model data and MET Malaysia warnings via data.gov.my) with an evidence/provenance
service, an assessment pipeline, the human decision gate, a FastAPI interface, and an agent
stage (five specialists, three bounded Tree-of-Thought scenarios and a risk proposal) that has
only been exercised with a scripted fake LLM. Wind, tides,
reviewed rules, real LLM adapters, RAG, durable storage (PostgreSQL) and a web UI are not
implemented yet. With `DIVESAFE_LLM_PROVIDER=fake` (the default) the agent stage is off.
Until wind and tide sources exist, every assessment is `INSUFFICIENT EVIDENCE` by design.

Marine data is Open-Meteo.com (CC BY 4.0, non-commercial use only), with wave models from DWD
and others.

## Safety hierarchy

1. Deterministic safety rules
2. Validated environmental data
3. Risk model
4. Agent reasoning
5. LLM-generated explanation

A lower layer can never relax a higher one. See [docs/architecture.md](docs/architecture.md).

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest                      # all tests (offline; uses recorded fixtures)
pytest -m safety            # safety-hierarchy tests only
ruff check . && mypy        # lint and types
uvicorn divesafe.api.app:create_app --factory --reload
```

With Docker (API + PostgreSQL/pgvector):

```bash
docker compose -f docker/docker-compose.yml --env-file .env up --build
```

> **macOS note:** if `import divesafe` stops working in the venv, macOS may have flagged the
> editable-install `.pth` file as hidden. Run `chflags -R nohidden .venv`. `pytest` is
> configured with `pythonpath = ["src"]` and is unaffected.

## API (development)

```bash
export DIVESAFE_AUTH_MODE=dev DIVESAFE_EVIDENCE_MAX_AGE_MINUTES=60 DIVESAFE_DECISION_MAX_AGE_MINUTES=120
uvicorn divesafe.api.app:create_app --factory --port 8000
curl -X POST localhost:8000/v1/assessments -H 'X-Dev-Actor: me' -H 'content-type: application/json' \
  -d '{"site_id":"my-terengganu-pulau-redang","planned_start":"<future UTC time>","planned_duration_minutes":90,"max_depth_m":18}'
```

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness (no auth) |
| `POST /v1/assessments` | Gather evidence and create a `PENDING_HUMAN` assessment |
| `GET /v1/assessments/{id}` | Read an assessment |
| `POST /v1/assessments/{id}/decision` | Human accepts or overrides (rationale required for overrides) |
| `POST /v1/assessments/{id}/actual-conditions` | Post-dive conditions (after a decision, once) |

Without `DIVESAFE_EVIDENCE_MAX_AGE_MINUTES` the create endpoint returns 503: there is no default
evidence age. Outside development use `DIVESAFE_AUTH_MODE=api_key` with
`DIVESAFE_API_KEY_HASHES` (see `.env.example`). Records are in memory only and are lost on
restart ([ADR 0006](docs/adr/0006-api-auth-and-repository.md)).

## Documentation

| Doc | Contents |
| --- | --- |
| [docs/project-brief.md](docs/project-brief.md) | Purpose, scope, non-goals |
| [docs/architecture.md](docs/architecture.md) | Components, lifecycle, safety hierarchy |
| [docs/agent-design.md](docs/agent-design.md) | Agents and the orchestrator |
| [docs/rag-design.md](docs/rag-design.md) | RAG scope and the live-data separation |
| [docs/tot-design.md](docs/tot-design.md) | Bounded Tree-of-Thought scenario evaluation |
| [docs/risk-model.md](docs/risk-model.md) | Rules engine, risk model, reconciliation |
| [docs/data-sources.md](docs/data-sources.md) | Source categories and connector contract |
| [docs/evaluation-strategy.md](docs/evaluation-strategy.md) | How the system is tested and evaluated |
| [docs/adr/](docs/adr/) | Architecture decision records |

Contributor and AI-agent rules are in [AGENTS.md](AGENTS.md).
# divesafe-ai
