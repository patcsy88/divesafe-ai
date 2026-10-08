# DiveSafe AI

Agentic environmental risk assessment and decision support for recreational diving.

DiveSafe AI combines weather, wind, waves/swell, currents, tides, sea temperature, marine
warnings, historical observations, dive-site knowledge and a user's dive plan into an
**explainable recommendation**: `GO`, `CAUTION`, `NO-GO` or `INSUFFICIENT EVIDENCE`.

> **Decision support only.** The final decision always belongs to the human diver, dive master
> or dive leader. DiveSafe AI is not a safety authority.

## Status

Foundation phase. The repository contains the architecture, engineering rules, and the
safety-critical core (domain types, deterministic rules engine with reconciliation, LLM provider
abstraction, config, API skeleton). Data connectors, agents, RAG and the human interface are
not implemented yet.

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
pytest                      # all tests
pytest -m safety            # safety-hierarchy tests only
ruff check . && mypy        # lint and types
uvicorn divesafe.api.app:create_app --factory --reload
```

With Docker (API + PostgreSQL/pgvector):

```bash
docker compose -f docker/docker-compose.yml --env-file .env up --build
```

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
