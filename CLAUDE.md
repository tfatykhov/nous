# CLAUDE.md - Nous Development Guide

## What is Nous?

Nous (Greek: mind/intellect) is a cognitive agent framework built on Minsky's Society of Mind principles. It gives AI agents persistent memory, decision intelligence, and the ability to learn from experience.

**Status: v1.0.0 released 2026-09-05** ([release notes](CHANGELOG.md)). All core architecture is live and deployed.

## Architecture

```
Cognitive Layer (hooks into LLM calls)
    ├── Brain (decisions, deliberation, calibration, guardrails)
    ├── Heart (episodes, facts, procedures, censors, working memory)
    ├── Context Engine (token budgets, relevance scoring, intent-driven retrieval)
    └── Event Bus (async handlers for automation)

Runtime: Direct Anthropic API + tool dispatch loop
Storage: PostgreSQL + pgvector (one DB, three schemas: brain/heart/system)
API: REST (42 endpoints) + MCP server + Telegram bot (streaming)
```

## Project Structure

```
nous/
├── docker-compose.yml      # Nous agent + Postgres + pgvector
├── Dockerfile              # Python container with OAT support
├── sql/                    # init.sql (base schema), migrations/, seed.sql
├── nous/                   # Python package
│   ├── config.py           # Settings via pydantic-settings
│   ├── main.py             # Entry point, component wiring, lifecycle
│   ├── storage/            # Database layer (async SQLAlchemy): pool, ORM models, migrator
│   ├── brain/              # Decision intelligence organ
│   ├── heart/              # Memory system organ
│   ├── cognitive/          # Cognitive layer (Nous Loop): pre_turn / post_turn / end_session
│   ├── handlers/           # Event bus handlers
│   ├── skills/             # Skill discovery (F011)
│   ├── heartbeat/          # Proactive monitoring (F034)
│   ├── identity/           # Agent identity (F018)
│   ├── observability/      # Telemetry writers — dashboards read these tables, the agent never does
│   ├── dag/                # F038/F087 DAG orchestration
│   ├── a2ui/               # F092 companion surfaces
│   └── api/                # External interfaces: REST, MCP, agent runner, tool dispatch
├── nous_eval/              # Retrieval eval harness (F051) — dev-only, NOT shipped in the prod Dockerfile
├── dashboard-app/          # Svelte dashboard + companion (see Dashboard below)
├── tests/                  # pytest suite (real Postgres)
└── docs/                   # research/, features/, implementation/, plans/, reviews/, reference/
```

Per-file map with annotations: [docs/reference/project-structure.md](docs/reference/project-structure.md).

## How to Work

### Read Before Building

1. Check `docs/implementation/` for build specs
2. Reference `docs/research/` for design rationale
3. Reference `docs/features/` for high-level feature context
4. Check `docs/features/INDEX.md` for current status of everything

### Tech Stack

- **Python 3.12+** (3.14 in container)
- **PostgreSQL 17** with pgvector extension
- **SQLAlchemy 2.0+** (async, declarative ORM)
- **asyncpg** (async Postgres driver)
- **pydantic v2** + pydantic-settings for config
- **Starlette** for REST API
- **httpx** for HTTP clients (Anthropic API, Telegram, etc.)
- **pytest** + pytest-asyncio for tests
- **uv** for dependency management

### Key Principles

- **Brain and Heart are in-process Python modules** — no MCP, no HTTP between them. Direct function calls, shared connection pool.
- **MCP is only the external interface** — for other agents/tools to talk to Nous.
- **Same ideas as Cognition Engines, not same code** — CE proved the concepts, Nous reimplements natively.
- **Direct Anthropic API** — no SDK wrapper. httpx calls with internal tool dispatch loop.
- **Async everywhere** — all database operations use async/await.
- **pgvector for all embeddings** — unified semantic search, no separate vector DB.
- **HNSW indexes over ivfflat** — works on empty tables, better recall.
- **OAT token support** — Max subscription tokens use Bearer auth + beta headers.

### Database

- Three schemas: `brain`, `heart`, `nous_system` (47 tables total: brain 9, heart 15, nous_system 23 — ground truth is the expected set in `tests/test_database.py::test_all_tables_exist`)
- `nous_system.execution_ledger` (migration 074, harness Phase 1b) is the durable record of side-effecting tool calls; it assumes one Nous process per (database, agent_id)
- All tables are agent-scoped (`agent_id` column) for multi-agent readiness
- Use `vector(1536)` for embeddings (text-embedding-3-small)
- Full-text search via `tsvector` + GIN indexes
- JSONB for flexible fields (config, conditions, items)
- Soft deletes (`active` boolean), never hard delete memory

### Code Style

- Type hints on everything
- Docstrings on public functions
- Use `mapped_column()` for SQLAlchemy models
- Use `pydantic.BaseModel` for API schemas
- Async context managers for database sessions
- Tests use real Postgres (via docker-compose), not mocks

### Running

```bash
# Full stack (Nous + Postgres)
docker compose up -d

# Just Postgres (for local dev)
docker compose up -d postgres

# Install dependencies
uv sync

# Run tests
uv run pytest tests/ -v

# Start Nous locally
uv run python -m nous.main
```

### Environment Variables

DB connection vars are **unprefixed** (shared with docker-compose). All others use `NOUS_` prefix. Settings are defined in `nous/config.py`, which is the source of truth; [docs/reference/environment-variables.md](docs/reference/environment-variables.md) documents defaults and rationale for the settings that have a row there.

### Dashboard (Svelte v2)

The dashboard is a Svelte SPA under `dashboard-app/`. Build with `cd dashboard-app && npm run build` (or via the Docker `dashboard` build stage); output lands in `static/dashboard-v2/dist/` and is served at `/dashboard/v2/`. Visiting `/dashboard` or `/dashboard/` redirects there. The legacy vanilla-JS dashboard (`static/dashboard/js/`, `css/`, `index.html`) was retired 2026-06-19.

## Detailed Reference (read on demand)

Detailed reference lives in `docs/reference/` so it is read only when a task needs it. Read the matching doc before working in that area.

| Doc | Contents | Read when |
|-----|----------|-----------|
| [Environment variables](docs/reference/environment-variables.md) | Settings with a documented default, rationale, measured evidence or rollback notes (`nous/config.py` is the full list) | Adding or changing a setting or feature flag, or debugging config. It is large: search it for the variable name. |
| [Shipped features](docs/reference/shipped-features.md) | Per-feature ship log with design rationale and invariants | Touching a shipped feature (an `F0xx` number, a harness phase) |
| [Project structure](docs/reference/project-structure.md) | Per-file map of `nous/` and `nous_eval/` | Locating a module |
| [REST API](docs/reference/rest-api.md) | Documented endpoints (`nous/api/rest.py` is the full list) | Adding or changing an endpoint or dashboard route |
| [Agent tools](docs/reference/agent-tools.md) | Documented agent tools and their frame access (the `dispatcher.register()` calls are the full list) | Adding or changing a tool |

**Keep them in sync:** a new setting, shipped feature, module, endpoint or tool gets its row in the matching reference doc in the same PR, not in this file. Feature status also goes in `docs/features/INDEX.md`. Older specs that say "update the CLAUDE.md env table" mean `docs/reference/environment-variables.md`.

## Git Workflow

- Work on feature branches, not main
- Commit messages: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- Keep commits focused — one logical change per commit
- All PRs need code review before merge

## References

- [Feature Index](docs/features/INDEX.md) — Current status of all features
- [Society of Mind](docs/research/002-minsky-mapping.md) — How Minsky maps to Nous
- [Database Design](docs/research/008-database-design.md) — Complete SQL for all tables
- [Storage Architecture](docs/research/004-storage-architecture.md) — Why Postgres + pgvector
- [Cognitive Layer](docs/research/005-cognitive-layer.md) — The seven systems
- [Automation Pipeline](docs/research/012-automation-pipeline.md) — Event bus design
