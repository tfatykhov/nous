# CLAUDE.md - Nous Development Guide

## What is Nous?

Nous (Greek: mind/intellect) is a cognitive agent framework built on Minsky's Society of Mind principles. It gives AI agents persistent memory, decision intelligence, and the ability to learn from experience.

**Status: v0.1.0 shipped and deployed.** All core architecture is live.

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
├── docker-compose.yml          # Nous agent + Postgres + pgvector
├── Dockerfile                  # Python container with OAT support
├── sql/
│   ├── init.sql                # Base schema (21 tables, 3 schemas)
│   ├── migrations/             # Schema migrations (006-016)
│   └── seed.sql                # Default agent, frames, guardrails
├── nous/                       # Python package (~30,000 lines)
│   ├── config.py               # Settings via pydantic-settings
│   ├── main.py                 # Entry point, component wiring, lifecycle
│   ├── telegram_bot.py         # Telegram interface (streaming + usage)
│   ├── events.py               # Event bus (async pub/sub)
│   ├── utils.py                # Shared utilities
│   ├── storage/                # Database layer (async SQLAlchemy)
│   │   ├── database.py         # Connection pool, session management
│   │   ├── models.py           # ORM models for all 28 tables
│   │   └── migrator.py         # Schema migration runner
│   ├── brain/                  # Decision intelligence organ
│   │   ├── brain.py            # Core: record, query, review, calibrate
│   │   ├── bridge.py           # Structure + function descriptions
│   │   ├── calibration.py      # Brier scores, confidence tracking
│   │   ├── embeddings.py       # pgvector embedding provider
│   │   ├── graph_linker.py     # Cross-type auto-linking (common-template embedding)
│   │   ├── guardrails.py       # CEL expression guardrails
│   │   ├── quality.py          # Decision quality scoring
│   │   ├── schemas.py          # Pydantic models
│   │   └── spreading_activation.py  # Density-gated multi-hop graph traversal
│   ├── heart/                  # Memory system organ
│   │   ├── heart.py            # Core: learn, recall, episode lifecycle
│   │   ├── episodes.py         # Episodic memory
│   │   ├── facts.py            # Semantic memory
│   │   ├── procedures.py       # Procedural memory
│   │   ├── censors.py          # Guardrail censors
│   │   ├── censor_actions.py   # F031: Censor action executor (read-only tools)
│   │   ├── working_memory.py   # Short-term scratch space
│   │   ├── search.py           # Full-text + vector search
│   │   ├── subtasks.py         # Subtask CRUD operations
│   │   ├── schedules.py        # Schedule CRUD operations
│   │   └── schemas.py          # Pydantic models
│   ├── cognitive/              # Cognitive layer (Nous Loop)
│   │   ├── layer.py            # pre_turn / post_turn / end_session
│   │   ├── frames.py           # Frame selection (task, question, decision, etc.)
│   │   ├── context.py          # Token-budgeted context assembly
│   │   ├── deliberation.py     # Pre-action protocol
│   │   ├── intent.py           # Intent classification for retrieval
│   │   ├── dedup.py            # Conversation deduplication
│   │   ├── monitor.py          # Post-turn self-assessment
│   │   ├── usage_tracker.py    # Context usage feedback loop
│   │   └── schemas.py          # TurnContext, TurnResult, etc.
│   ├── handlers/               # Event bus handlers
│   │   ├── episode_summarizer.py  # Episode summary generation
│   │   ├── fact_extractor.py      # Fact extraction from conversations
│   │   ├── knowledge_extractor.py # Pre-prune fact extraction
│   │   ├── decision_reviewer.py   # Automated decision review
│   │   ├── session_monitor.py     # Session timeout monitoring
│   │   ├── sleep_handler.py       # Sleep/reflection handler
│   │   ├── subtask_worker.py      # Async subtask execution
│   │   ├── task_scheduler.py      # Cron/one-shot scheduling
│   │   └── time_parser.py         # Natural language time parsing
│   ├── skills/                 # Skill discovery system (F011)
│   │   ├── parser.py           # SkillParser + SkillManifest
│   │   └── bootstrap.py        # One-time local skill registration
│   ├── heartbeat/              # Proactive monitoring (F034)
│   │   ├── runner.py           # HeartbeatRunner tick loop + triage
│   │   ├── registry.py         # CheckRegistry + BaseCheck ABC
│   │   ├── checks.py           # HealthCheck, SelfInitiatedCheck, EmailCheck
│   │   ├── dynamic.py          # DynamicCheck + DynamicCheckLoader (F034.5)
│   │   └── schemas.py          # Finding, CheckResult, HeartbeatResult
│   ├── identity/               # Agent identity system (F018)
│   │   ├── manager.py          # Identity section CRUD
│   │   ├── protocol.py         # Initiation protocol
│   │   └── tools.py            # Identity-related tools
│   └── api/                    # External interfaces
│       ├── rest.py             # Starlette REST API (52 endpoints)
│       ├── mcp.py              # MCP server (nous_chat, nous_decide, etc.)
│       ├── runner.py           # Agent runner (tool loop, streaming)
│       ├── tools.py            # Tool dispatcher + registration
│       ├── retrieval_pipeline.py # F051: run_recall_pipeline (shared by recall_deep + eval)
│       ├── builtin_tools.py    # bash, read_file, write_file
│       ├── web_tools.py        # web_search, web_fetch (multi-tier routing)
│       ├── search_providers.py # SearchProvider protocol + Tavily, Exa, Brave
│       ├── search_router.py   # Query classification + cascading fallback
│       ├── compaction.py       # History compaction engine
│       ├── smart_compress.py   # Smart compression for tool results
│       ├── tool_cache.py       # Tool result caching
│       └── models.py           # API request/response models
├── nous_eval/                  # Retrieval evaluation harness (F051) — dev-only sibling package
│   │                           #   NOT shipped in prod Dockerfile; `COPY nous/ nous/` skips this.
│   ├── config.py               # EvalSettings (pydantic-settings, NOUS_EVAL_* prefix)
│   ├── source_registry.py      # sources.yaml loader + per-source toggles
│   ├── corpus_loader.py        # Bulk JSONL → Postgres (ingest + test-DB seed)
│   ├── qrels_loader.py         # Qrel pydantic model + JSONL loader + reviewed_by gate
│   ├── retrieval_runner.py     # run_matrix: RuntimeConfig.reset + per-config Heart/Brain
│   ├── metrics.py              # MRR/P@K/R@K/nDCG (pure Python, no numpy)
│   ├── report.py               # Markdown + JSON + decide_gate_f050
│   ├── retrieval.py            # `python -m nous_eval.retrieval` CLI
│   ├── rebuild.py              # `python -m nous_eval.rebuild` (volume purge)
│   ├── ingest_entry.py         # `python -m nous_eval.ingest_entry` dispatcher
│   ├── tasks.py                # Cross-platform task runner (build-image, push, etc.)
│   ├── ingest.py               # Quarterly prod-DB fixture refresh
│   ├── ingest_longmemeval.py   # 20-Q stratified LongMemEval_S subset ingestion
│   ├── probe_gen.py            # Auto-generate probes from INDEX.md + git log
│   ├── hand_labels_draft.py    # AI-drafted hand-label qrels
│   ├── multi_turn_eval.py      # F051.4: walks LongMemEval haystacks via dispatcher; per-config metrics
│   ├── run_history.py          # F051 Phase 1 finish (#365/#366/#367): persists eval_runs to EVAL DB
│   └── regression.py           # `python -m nous_eval.regression` — compares latest run vs N-day-old baseline, exits non-zero on regression
├── tests/                      # 1750+ tests across 91 files
└── docs/
    ├── research/               # Theory & design notes (001-016)
    ├── features/               # High-level feature specs (F001-F030)
    ├── implementation/         # Build specs (001-014.1, all shipped)
    ├── plans/                  # Implementation plans
    └── reviews/                # Code review documents
```

## What's Shipped (v0.1.0)

| Spec | Component | PR |
|------|-----------|----|
| 001 | Postgres scaffold (24 base tables + 19 migrations, 3 schemas) | #1 |
| 002 | Brain module (decisions, deliberation, calibration, guardrails) | #2 |
| 003 | Heart module (episodes, facts, procedures, censors, working memory) | #3 |
| 003.1 | Heart enhancements (contradiction detection, domain compaction) | #6 |
| 003.2 | Frame-tagged memory encoding | — |
| 004 | Cognitive Layer (frames, recall, deliberation, monitoring) | #10 |
| 004.1 | CEL expression guardrails | #10 |
| 005 | Runtime (REST API, MCP server, agent runner) | — |
| 005.1 | Smart context preparation (intent-driven retrieval) | — |
| 005.2 | Direct Anthropic API rewrite (replaced Claude Agent SDK) | #15 |
| 005.3 | Web tools (web_search, web_fetch via Brave) | #16 |
| 005.4 | Streaming responses (SSE + Telegram progressive editing) | #23 |
| 005.5 | Noise reduction (frame instructions, decision filtering) | #20 |
| 006 | Event Bus (async handlers, DB persistence) | — |
| F010 | Memory improvements (episode summaries, fact extraction, user tagging) | #21 |
| 011.1 | Subtasks & Scheduling (F009) | #85 |
| 011.2 | Subtask Result Delivery (F009) | — |
| 014.1 | Context Quality Engine (F016+F017) | #122 |
| F012 | K-Line Procedure Learning (auto-create procedures from decision clusters, monitor reinforcement) | #134 |
| F011 | Skill Discovery v2 (learn_skill tool, SkillParser, bootstrap, auto-activation via RECALL) | — |
| F022 | Graph-Augmented Recall (polymorphic edges, cross-type linking, contradiction bridge, spreading activation) | — |
| F023 | Memory Admission Control (5-dimension scoring, shadow mode) | — |
| F024 | Critic Agent Phase 0 (smart frame selector, LLM classification, diagnostic critics) | — |
| F024-3b | Self-Modifying Rubrics (outcome signals, dimension proposals, rubric evolution) | #196 |
| F026 | Execution Integrity (execution ledger, action gating, claim verification, ghost planning detection) | #183 |
| F030 | MMR Diversity Reranking (Maximal Marginal Relevance in recall_deep) | #205 |
| F031 | Censor Middleware with Action Payloads (censors execute read-only tools, conditional unblock, update API) | #208 |
| F032 | Execution Ledger Dashboard (per-action visibility, status filtering, side-effect classification) | — |
| F033 | Multi-Tier Search Routing (Tavily primary, Exa research, Brave fallback, query classification) | — |
| F034 | Heartbeat Proactive Monitoring (tick loop, health/email/self-initiated checks, triage, Telegram) | #236 |
| F034.1 | Finding Lifecycle (fingerprint dedup, state machine, escalation, daily digest, outcome signals) | #241 |
| F034.2 | Intelligent Checks (embedding search, LLM email classification, drive significance, tunable params) | #241 |
| F034.3 | Self-Tuning Heartbeat (outcome-driven adjustment, cross-cycle rollback, pinned params) | #241 |
| F034.5 | Dynamic Heartbeat Checks (prompt-driven checks, conversational creation/management, full lifecycle) | #252 |
| F034.6 | on_complete Callback for Dynamic Checks (callback prompt on self-disable, 3-layer failure handling, background execution) | #275 |
| F036 | Prompt Cache Optimization (3-tier system prompt split, cache break detection, single breakpoint strategy, tool schema caching) | #253 |
| 012.3 | Programmatic Tool Calling (run_python with memory functions in scope) | — |
| F025 | Amnesia Prevention Phase 2+3 (staleness exemptions, budget scaling, transcript 16K, dedup 0.92, source text passthrough, chunked summarization, transcript persistence) | — |
| F038 | Memory Quality & Context Loading Fixes (quality gate 0.55, fact 30-char min, procedure floor 0.40, episode recency, user_direct bonus, task synthesis, context dedup, bash hints) | #258 |
| F038 | Unified DAG Orchestration (DAGStore, DAGOrchestrator, DAG tools, dashboard tab) | #289 |
| F040 | Graph Densification (orphan backfill, reverse linking, per-relation thresholds, density dashboard, cluster discovery) | — |
| F042 | Cross-Encoder Reranking (sigmoid-normalized, async, head-truncation, feature-flagged, optional sentence-transformers dep) | #312 |
| F043 | Cross-Encoder Reranking in Sleep-Cycle Graph Backfill (precision pre-filter before cosine gate, reuses F042 reranker, feature-flagged, _ce_stats telemetry) | #314 |
| F045 | CE-Aware Cosine Thresholds + Content-Length Guard (relaxed per-relation thresholds when CE backfill is upstream, 80-char min to drop URL-only facts, empirically validated at 80% LLM-judged precision) | #315 |
| F046 | [DAG Node Timeout Configuration](docs/features/F046-dag-node-timeout-config.md) (env-var-driven DAG node timeouts — `NOUS_DAG_NODE_DEFAULT_TIMEOUT`=600s, `NOUS_DAG_NODE_MAX_TIMEOUT`=7200s; Settings DI on DAGStore+DAGOrchestrator; schema `timeout_seconds` → `int \| None`; defensive clamp at 3 read sites; unblocks long-running Claude Code / deep-research DAG nodes) | — |
| F047 | [Actionability Classification](docs/features/F047-actionability-classification.md) (learn-time classifier persists `actionable: bool` on `heart.facts`, replacing the `_OBSERVATION_PATTERNS` arms-race at heartbeat read time — 3 tiers: hard filter → positive-wins heuristic → Haiku LLM; backfill handler with PG advisory lock + supervision wrapper; heartbeat now consults persisted verdict with positive-wins fallback for NULL rows, fixing the PR #335 short-circuit bug; supersedes PR #335) | — |
| F048 | [Background Streaming + TCP Keep-Alive](docs/features/F048-background-streaming-keepalive.md) (subtask + heartbeat turns stream under the hood via `call_streaming_aggregated` on both Anthropic clients — keeps TCP socket warm with incremental SSE bytes so long background generations no longer hit idle-connection drops; `AgentRunner.run_turn(is_background=True)` threads through `_tool_loop` to every `_call_api` call; wired at 5 sites: subtask_worker, heartbeat cognitive_triage + on_complete callback, DynamicCheck._run_check, and inline `spawn_task(await_result)`; `httpx.AsyncHTTPTransport` gains `SO_KEEPALIVE` + Linux `TCP_KEEPIDLE` / macOS `TCP_KEEPALIVE` via `_build_socket_options` helper; truncated-stream detection raises rather than silently returning empty content; fixes pre-existing censor-block 2-tuple return bug at runner.py:249; gated by `NOUS_API_BACKGROUND_STREAMING_ENABLED=true` + `NOUS_API_SOCKET_KEEPALIVE_ENABLED=true`) | — |
| F049 | [Session & Memory Lifecycle Hygiene](docs/features/F049-session-lifecycle-hygiene.md) (closes #187 + scoped #166 — `_execute_subtask` wraps body in `try/finally` calling `end_conversation` under `asyncio.shield(asyncio.wait_for(..., 30))` with three distinct except branches (TimeoutError/CancelledError/Exception) at ERROR severity; `WorkingMemoryManager.cleanup_stale()` sweeps stale `heart.working_memory` rows via `ctid IN (SELECT … LIMIT N)` batched DELETE under `pg_try_advisory_xact_lock` keyed on a SHA-256 hash of `agent_id` for cross-process-stable replica serialization; session monitor grows `heart: Heart | None` kwarg and invokes the sweep at most once per `NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS`; 13 new tests, empirically targets 86/87 stale rows observed in 2026-04-20 audit) | — |
| F051 | [Retrieval Evaluation Harness](docs/features/F051-retrieval-eval-harness.md) (local-first retrieval eval + per-source qrels + paired A/B — new `nous/api/retrieval_pipeline.py::run_recall_pipeline` extracted from `tools.py::recall_deep` to expose structured results alongside unchanged LLM-facing text; new `nous_eval/` module tree (config, source_registry, corpus_loader, qrels_loader, retrieval_runner, metrics, report, CLI entries); persistent `nous-eval-db` Docker image under `docker compose --profile eval` bound to `127.0.0.1:5433`; new `sql/migrations/037_eval_runs.sql` for run history; `_verify_fixture_version` + `_verify_corpus_agent_id` preflight probes; `RuntimeConfig.reset()` between configs; 14-flag disable list for background handlers; `.gitattributes` enforces LF on `.sh`; `F050` gate logic in `decide_gate_f050` requires aggregate MRR +7%, no single-source regression >3%, and majority-positive sources; 69 new tests + byte-identical recall_deep snapshot; 4-agent implementation team with 2-cycle review) | — |

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

- Three schemas: `brain`, `heart`, `nous_system` (28 tables total)
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

DB connection vars are **unprefixed** (shared with docker-compose). All others use `NOUS_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `localhost` | Database host |
| `DB_PORT` | `5432` | Database port |
| `DB_USER` | `nous` | Database user |
| `DB_PASSWORD` | `nous_dev_password` | Database password |
| `DB_NAME` | `nous` | Database name |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `ANTHROPIC_AUTH_TOKEN` | — | OAT token (Max subscription, uses Bearer auth) |
| `NOUS_IDENTITY_PROMPT` | Built-in default | **Agent identity.** First section of every system prompt. How Nous knows who it is, what tools it has, and how to behave. Override to customize. |
| `NOUS_AGENT_ID` | `nous-default` | Agent identifier |
| `NOUS_AGENT_NAME` | `Nous` | Agent display name |
| `NOUS_MODEL` | `claude-sonnet-4-6` | LLM model for chat |
| `NOUS_MAX_TURNS` | `10` | Max tool loop iterations |
| `NOUS_MCP_ENABLED` | `true` | Enable MCP server |
| `NOUS_LOG_LEVEL` | `info` | Log level |
| `BRAVE_SEARCH_API_KEY` | — | Brave Search API key (tertiary fallback) |
| `TAVILY_API_KEY` | — | Tavily Search API key (primary search provider) |
| `EXA_API_KEY` | — | Exa Search API key (deep research queries) |
| `NOUS_SEARCH_PROVIDER` | `auto` | Search routing: auto, tavily, exa, brave |
| `OPENAI_API_KEY` | — | For embeddings (text-embedding-3-small) |
| `NOUS_EVENT_BUS_ENABLED` | `true` | Enable async event bus |
| `NOUS_EPISODE_SUMMARY_ENABLED` | `true` | Enable episode summarization handler |
| `NOUS_FACT_EXTRACTION_ENABLED` | `true` | Enable fact extraction handler |
| `NOUS_SLEEP_ENABLED` | `true` | Enable sleep/reflection handler |
| `NOUS_BACKGROUND_MODEL` | `claude-sonnet-4-6` | Model for background LLM tasks |
| `NOUS_SESSION_TIMEOUT` | `1800` | Session idle timeout in seconds |
| `NOUS_SLEEP_TIMEOUT` | `7200` | Sleep mode timeout in seconds |
| `NOUS_SLEEP_CHECK_INTERVAL` | `60` | Sleep check interval in seconds |
| `NOUS_SUBTASK_ENABLED` | `true` | Enable subtask worker pool |
| `NOUS_SUBTASK_WORKERS` | `2` | Number of async worker tasks |
| `NOUS_SUBTASK_POLL_INTERVAL` | `2.0` | Seconds between queue polls |
| `NOUS_SUBTASK_DEFAULT_TIMEOUT` | `600` | Default subtask timeout (seconds). Bumped from 120 in F048 so the outer `asyncio.wait_for` does not cancel before the new 600 s per-chunk streaming read completes. |
| `NOUS_SUBTASK_MAX_TIMEOUT` | `3600` | Maximum allowed subtask timeout (seconds). Bumped from 900 in F048 to pair with `NOUS_API_BACKGROUND_TIMEOUT_READ`. |
| `NOUS_API_BACKGROUND_STREAMING_ENABLED` | `true` | F048 master switch — route `is_background=True` turns (subtask + heartbeat) through `call_streaming_aggregated` instead of `call()`. |
| `NOUS_API_BACKGROUND_TIMEOUT_READ` | `600` | F048 per-chunk read timeout (seconds) applied only to background streamed requests. |
| `NOUS_API_SOCKET_KEEPALIVE_ENABLED` | `true` | F048 master switch for TCP keep-alive on the httpx transport (both Anthropic client backends). |
| `NOUS_API_SOCKET_KEEPALIVE_IDLE` | `30` | F048 seconds of idle before the first TCP keep-alive probe (Linux `TCP_KEEPIDLE` / macOS `TCP_KEEPALIVE`; ignored on Windows). |
| `NOUS_API_SOCKET_KEEPALIVE_INTERVAL` | `10` | F048 seconds between TCP keep-alive probes (Linux `TCP_KEEPINTVL`; ignored otherwise). |
| `NOUS_API_SOCKET_KEEPALIVE_COUNT` | `3` | F048 number of failed keep-alive probes before dropping the connection (Linux `TCP_KEEPCNT`; ignored otherwise). |
| `NOUS_DAG_NODE_DEFAULT_TIMEOUT` | `600` | Default timeout (s) for DAG nodes when node spec omits `timeout_seconds` (F046) |
| `NOUS_DAG_NODE_MAX_TIMEOUT` | `7200` | Hard ceiling (s) for DAG node `timeout_seconds` — clamped at insert and at read sites (F046) |
| `NOUS_ACTIONABILITY_ENABLED` | `true` | Enable F047 actionability classification at fact learn time |
| `NOUS_ACTIONABILITY_LLM_ENABLED` | `true` | Use Haiku LLM for ambiguous actionability cases (tier 2) |
| `NOUS_ACTIONABILITY_MODEL` | `claude-haiku-4-5-20251001` | LLM model for F047 actionability tier-2 classification |
| `NOUS_ACTIONABILITY_DEFAULT` | `false` | Fallback verdict when classifier can't decide (false = fail-closed, don't page user) |
| `NOUS_ACTIONABILITY_BACKFILL_ON_STARTUP` | `true` | Run F047 backfill automatically on startup for NULL rows |
| `NOUS_ACTIONABILITY_BACKFILL_TOKEN_BUDGET` | `10000` | Rough Haiku daily token cap for backfill |
| `NOUS_SUBTASK_CLEANUP_TIMEOUT_SECONDS` | `30` | F049 — max seconds to wait for `end_conversation` in the subtask `finally` before logging ERROR. Bounds a hung reflection `_call_api` so it cannot block a worker forever. |
| `NOUS_WORKING_MEMORY_TTL_HOURS` | `24` | F049 — delete `heart.working_memory` rows older than this (0 disables the sweep entirely). Safety net for session paths that bypass `end_conversation`. |
| `NOUS_WORKING_MEMORY_SWEEP_INTERVAL_SECONDS` | `3600` | F049 — minimum seconds between WM TTL safety-net sweeps. |
| `NOUS_WORKING_MEMORY_SWEEP_BATCH_SIZE` | `5000` | F049 — rows per DELETE batch during WM sweep; avoids long exclusive locks at scale. |
| `NOUS_EVAL_DB_HOST` | `localhost` | F051 — eval DB host (separate container on port 5433) |
| `NOUS_EVAL_DB_PORT` | `5433` | F051 — eval DB port (127.0.0.1 bind; separate from main :5432) |
| `NOUS_EVAL_DB_USER` | `nous` | F051 — eval DB user |
| `NOUS_EVAL_DB_PASSWORD` | `nous_eval` | F051 — eval DB password; warns UserWarning if left default |
| `NOUS_EVAL_DB_NAME` | `nous_eval` | F051 — eval DB name |
| `NOUS_EVAL_AGENT_ID` | `nous-eval-corpus` | F051 — agent_id used by ingested corpus; must match Heart.recall filter |
| `NOUS_EVAL_FIXTURES_DIR` | *(unset)* | F051 — path to clone of nous-eval-fixtures repo; unset = smoke mode |
| `NOUS_EVAL_FIXTURE_VERSION` | `v2026-Q2` | F051 — image tag pinned in docker-compose (not "latest" for reproducibility) |
| `NOUS_EVAL_TOP_K` | `10` | F051 — retrieval top-K for all metrics |
| `NOUS_EVAL_REPORT_DIR` | `reports` | F051 — where markdown + JSON reports land |
| `NOUS_EVAL_RUN_HISTORY_ENABLED` | `true` | F051 — persist run to nous_system.eval_runs on main DB |
| `NOUS_EVAL_RUN_HISTORY_INSERT_TIMEOUT_S` | `5.0` | F051 — asyncio.wait_for timeout on eval_runs INSERT (never blocks run) |
| `NOUS_EVAL_F050_GATE_THRESHOLD` | `0.07` | F051 — F050 paired-A/B MRR delta threshold (+7%) |
| `NOUS_EVAL_F050_GATE_MAX_SINGLE_REGRESSION` | `0.03` | F051 — max allowed per-source regression to still pass gate |
| `NOUS_EVAL_F050_GATE_REQUIRE_MAJORITY_POSITIVE` | `true` | F051 — gate additionally requires majority of gate-eligible sources positive |
| `NOUS_SUBTASK_MAX_CONCURRENT` | `3` | Max concurrent subtasks |
| `NOUS_SCHEDULE_ENABLED` | `true` | Enable task scheduler |
| `NOUS_SCHEDULE_CHECK_INTERVAL` | `60` | Seconds between schedule checks |
| `NOUS_TELEGRAM_BOT_TOKEN` | — | Telegram bot token for subtask notifications |
| `NOUS_TELEGRAM_CHAT_ID` | — | Telegram chat ID for subtask notifications |
| `NOUS_SUBTASK_TOOL_CALL_LIMIT` | `20` | Max tool calls per subtask execution |
| `NOUS_INLINE_SUBTASK_TIMEOUT` | `90` | Default timeout for inline (await_result) subtasks |
| `NOUS_FRAME_DEFAULT_MODELS` | `{}` | JSON map of frame type to default model (falls back to `NOUS_BACKGROUND_MODEL`) |
| `NOUS_PROGRAMMATIC_TOOLS_ENABLED` | `true` | Enable run_python tool for client-side code execution |
| `NOUS_PROGRAMMATIC_TOOLS_TIMEOUT` | `10` | Timeout in seconds for run_python code execution |
| `NOUS_CONTEXT_WINDOW` | auto | Override model context window size in tokens (0 = auto-detect from model name) |
| `NOUS_ANTI_HALLUCINATION_PROMPT` | `true` | Inject "don't guess, re-fetch" safety prompt into system context |
| `NOUS_TOOL_PRUNING_ENABLED` | `true` | Enable 4-tier tool result pruning pipeline |
| `NOUS_TOOL_SOFT_TRIM_CHARS` | `4000` | Threshold above which tool results get soft-trimmed |
| `NOUS_TOOL_SOFT_TRIM_HEAD` | `1500` | Chars to keep from start when soft-trimming |
| `NOUS_TOOL_SOFT_TRIM_TAIL` | `1500` | Chars to keep from end when soft-trimming |
| `NOUS_TOOL_METADATA_DEGRADE_AFTER` | `8` | Tool result age (in results) before metadata degradation |
| `NOUS_TOOL_HARD_CLEAR_AFTER` | `12` | Tool result age before hard-clear replacement |
| `NOUS_KEEP_LAST_TOOL_RESULTS` | `2` | Number of most recent tool results always protected |
| `NOUS_COMPACTION_ENABLED` | `true` | Enable LLM-powered history compaction |
| `NOUS_COMPACTION_THRESHOLD` | auto | Token count triggering compaction (auto-scales per model context window) |
| `NOUS_KEEP_RECENT_TOKENS` | auto | Tokens to preserve during compaction (auto-scales per model) |
| `NOUS_RELEVANCE_FLOOR_ENABLED` | `true` | Enable per-type minimum score filtering on memory retrieval |
| `NOUS_RELEVANCE_DROP_RATIO` | `0.6` | Diminishing returns cutoff — stop at >40% score drops |
| `NOUS_BUDGET_SCALE_ENABLED` | `true` | Scale context budgets based on model context window |
| `NOUS_CONTEXT_BUDGET_OVERRIDES` | `{}` | JSON dict overriding per-frame budget defaults (e.g. `{"total": 12000, "decisions": 3000}`) |
| `NOUS_STALENESS_PENALTY_ENABLED` | `true` | Apply time-decay penalty to memory scores |
| `NOUS_STALENESS_HALF_LIFE_DAYS` | `30` | Half-life in days for staleness decay |
| `NOUS_TRANSCRIPT_MAX_CHARS` | `16000` | Max chars for episode transcript truncation before summarization |
| `NOUS_FACT_DEDUP_THRESHOLD` | `0.92` | Hybrid search score threshold for fact extractor dedup (Leg 1, RRF pre-check at fact_extractor.py:243) |
| `NOUS_FACT_NATIVE_COSINE_THRESHOLD` | `0.95` | Native cosine threshold for Heart.learn dedup (Leg 2, facts.py:691). F056 #377 made this env-tunable; the F056 dedup eval found 0.80 lifts combined F1 from 0.40 → 0.76 on the smoke fixture. Default kept 0.95 for backwards-compat. |
| `NOUS_RRF_K` | `60` | RRF smoothing constant for hybrid search rank fusion |
| `NOUS_TOOL_TIMEOUT` | `120` | Max seconds for any single tool execution |
| `NOUS_KEEPALIVE_INTERVAL` | `10` | Seconds between keepalive events during tool execution |
| `NOUS_SSE_PING_INTERVAL` | `15` | Seconds between SSE comment-line pings on `/chat/stream`. Keeps the socket warm during stalls in pre_turn, compaction, or any non-streaming phase. Comment lines are ignored by SSE clients but reset their read timer. |
| `NOUS_GRAPH_RECALL_ENABLED` | `true` | Enable graph expansion in recall_deep |
| `NOUS_GRAPH_RECALL_MAX_EXPAND` | `5` | Max seed results to expand |
| `NOUS_GRAPH_RECALL_DECAY` | `0.7` | Score decay per graph hop |
| `NOUS_GRAPH_RECALL_MAX_NEIGHBORS` | `3` | Max neighbors per seed |
| `NOUS_CROSS_TYPE_LINKING_ENABLED` | `true` | Enable cross-type auto-linking |
| `NOUS_CROSS_TYPE_THRESHOLD` | `0.80` | Cross-type similarity threshold |
| `NOUS_CONTRADICTION_DETECTION` | `true` | Enable LLM contradiction detection |
| `NOUS_CONTRADICTION_MODEL` | `claude-haiku-4-5-20251001` | Model for contradiction classification |
| `NOUS_CONFIDENCE_CALIBRATION_FACTOR` | `0.7627` | F058: multiplicative scale applied to agent-recorded decision confidence at write time. Default derived empirically from `reports/calibration_eval.md` (401 reviewed prod decisions: mean conf 0.834 vs strict accuracy 0.636, Brier 0.252 at random baseline). Set to `1.0` to disable scaling. Pre-calibration value preserved in `brain.decisions.confidence_raw`. |
| `NOUS_CROSS_TYPE_LINK_MIN_CONTENT_CHARS` | `40` | F022 audit fix (2026-04-30): minimum content length (after strip) for the live event-bus linker to fire. Mirrors F054's `NOUS_CE_BACKFILL_MIN_DECISION_CHARS` for the backfill path. Empty/near-empty source or target content was the dominant cause of NO/WEAK edge verdicts on `informed_by` and `evidence_for` (precision 0.70). Set to `0` to disable. Filters both source side (in Python before embed) and target side (SQL `length` clause). |
| `NOUS_F026_PERSISTENCE_ENABLED` | `true` | F026 (2026-04-30): persist every action-gate verdict and claim-verification outcome to `nous_system.events` so a retrospective accuracy eval can run against real prod data. Fire-and-forget via `asyncio.create_task` so the gate hot path never blocks on DB I/O. Event types: `f026_action_gate`, `f026_claim_verification`. Set to `false` to disable. |
| `NOUS_SPREADING_ACTIVATION_ENABLED` | `auto` | Spreading activation (auto/true/false) |
| `NOUS_SPREADING_ACTIVATION_DENSITY_THRESHOLD` | `3.0` | Density threshold for auto-enable |
| `NOUS_EXECUTION_LEDGER_ENABLED` | `true` | Enable execution ledger (F026) |
| `NOUS_EXECUTION_LEDGER_MAX_TOKENS` | `500` | Token budget for ledger in system prompt |
| `NOUS_CLAIM_VERIFICATION_ENABLED` | `true` | Enable claim verification (F026) |
| `NOUS_CLAIM_VERIFICATION_MODE` | `enforce` | Claim verification mode (shadow/warn/enforce) |
| `NOUS_ACTION_GATING_ENABLED` | `true` | Enable action gating (F026) |
| `NOUS_ACTION_GATING_MODE` | `enforce` | Action gating mode (shadow/warn/enforce) |
| `NOUS_ACTION_GATING_MODEL` | `claude-haiku-4-5-20251001` | Model for Tier 3 LLM gate |
| `NOUS_ACTION_GATING_EXTERNAL_ONLY` | `false` | Skip Tier 2, only gate external/irreversible |
| `NOUS_ACTION_GATING_TURN_WINDOW` | `5` | Only block duplicates within this many turns |
| `NOUS_PROCEDURE_SCORE_FLOOR` | `0.40` | Minimum score for procedures when embeddings enabled (F038) |
| `NOUS_MMR_ENABLED` | `false` | Enable MMR diversity re-ranking in recall_deep |
| `NOUS_MMR_DIVERSITY_WEIGHT` | `0.7` | MMR relevance vs diversity weight (1.0=pure relevance, 0.0=pure diversity) |
| `NOUS_MMR_SKIP_AFTER_CE` | `true` | F030.1: skip MMR when CE rerank just reordered the head. F051 harness measured +30% MRR (0.372 -> 0.484, +190% on jargon-drift) when MMR is gated this way. Set `false` to restore pre-F030.1 chained CE-then-MMR behavior. |
| `NOUS_CROSS_ENCODER_ENABLED` | `false` | Enable F042 cross-encoder reranking in recall_deep (requires sentence-transformers) |
| `NOUS_QUERY_EXPANSION_ENABLED` | `false` | F050 master flag — enable Haiku-driven multi-query expansion at recall time (Phase 1 lands dark; flip after harness gate) |
| `NOUS_QUERY_EXPANSION_MODEL` | `claude-haiku-4-5-20251001` | F050 model used for the expansion call (forced tool use, ~256 tok output) |
| `NOUS_QUERY_EXPANSION_TIMEOUT_SECONDS` | `2.0` | F050 per-call timeout (seconds); on timeout, expand() fails open to [query] |
| `NOUS_QUERY_EXPANSION_MAX_VARIANTS` | `3` | F050 max variants returned including the original query |
| `NOUS_QUERY_EXPANSION_MIN_WORDS` | `3` | F050 gate threshold — queries shorter than this skip expansion |
| `NOUS_QUERY_EXPANSION_MAX_PER_HOUR` | `500` | F050 budget cap on Haiku calls per hour (asyncio.Lock-serialized in-process counter) |
| `NOUS_QUERY_EXPANSION_CACHE_TTL_DAYS` | `30` | F050 cache retention for `heart.query_expansions` rows |
| `NOUS_CROSS_ENCODER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model name for F042 reranking |
| `NOUS_CROSS_ENCODER_MAX_CANDIDATES` | `30` | Max candidates to rerank (head-truncation, tail untouched) |
| `NOUS_CROSS_ENCODER_TEXT_LIMIT` | `512` | Max chars per doc fed to cross-encoder |
| `NOUS_CE_BACKFILL_ENABLED` | `false` | Enable F043 cross-encoder reranking in F040 sleep-cycle graph backfill (requires sentence-transformers, reuses F042 model) |
| `NOUS_CE_BACKFILL_TOP_K` | `10` | Max candidates per orphan reranked by cross-encoder before cosine verification |
| `NOUS_CE_BACKFILL_MIN_SCORE` | `0.30` | Sigmoid-normalized CE score floor — candidates below this are dropped before the cosine gate |
| `NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT` | `0.55` | F054 (was F045 0.65): CE-mode fact↔fact cosine threshold. Relaxed 2026-04-26 — F053 density-eval measured +67% same-type edges at unchanged 0.83 LLM-judged precision. |
| `NOUS_CE_BACKFILL_THRESHOLD_FACT_DECISION` | `0.55` | F045: CE-mode fact→decision threshold (KEPT STRICT by F054 — loosening regressed cross-type precision in the 2026-04-26 audit; corpus-quality issue addressed via the new decision content guard below). |
| `NOUS_CE_BACKFILL_THRESHOLD_FACT_EPISODE` | `0.55` | F045: CE-mode fact→episode threshold (KEPT STRICT by F054 — same rationale as fact_decision). |
| `NOUS_CE_BACKFILL_THRESHOLD_DECISION_DECISION` | `0.50` | F054 (was F045 0.60): CE-mode decision↔decision threshold. Relaxed 2026-04-26. |
| `NOUS_CE_BACKFILL_THRESHOLD_EPISODE_EPISODE` | `0.50` | F054 (was F045 0.58): CE-mode episode↔episode threshold. Relaxed 2026-04-26 (extrapolated — eval corpus had 0 episode orphans). |
| `NOUS_CE_BACKFILL_THRESHOLD_PROCEDURE_ANY` | `0.45` | F054 (was F045 0.55): CE-mode procedure→* threshold. Relaxed 2026-04-26. |
| `NOUS_CE_BACKFILL_MIN_CONTENT_CHARS` | `80` | F045: drop candidates whose content (after strip) is shorter than this before CE inference. Filters URL-only / boilerplate facts. |
| `NOUS_CE_BACKFILL_MIN_DECISION_CHARS` | `40` | F054: type-aware content guard for decisions. Mirrors `ce_backfill_min_content_chars=80` for facts. Set to 0 to disable. Addresses empty `brain.decisions.context` polluting cross-type edges (root cause of ~5/9 evidence_for NO/WEAK verdicts in the 2026-04-26 F053 audit). |

**F045 migration note:** When `NOUS_CE_BACKFILL_ENABLED=true`, the `NOUS_GRAPH_THRESHOLD_*` env overrides below are **ignored** — `_get_threshold()` routes to the `NOUS_CE_BACKFILL_THRESHOLD_*` set instead. Operators upgrading from an F043-only deployment that had `NOUS_GRAPH_THRESHOLD_FACT_FACT` overridden must re-set the equivalent `NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT` to keep their override effective.

**F054 rollback note:** F054 relaxed four same-type CE-mode thresholds and added the decision content guard. To revert to pre-F054 (F045) behavior, set: `NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT=0.65`, `NOUS_CE_BACKFILL_THRESHOLD_DECISION_DECISION=0.60`, `NOUS_CE_BACKFILL_THRESHOLD_EPISODE_EPISODE=0.58`, `NOUS_CE_BACKFILL_THRESHOLD_PROCEDURE_ANY=0.55`, `NOUS_CE_BACKFILL_MIN_DECISION_CHARS=0`.
| `NOUS_CRITIC_SKILL_INJECTION` | `disabled` | Critic skill injection mode: enabled, disabled, log_only |
| `NOUS_CRITIC_SKILL_SLOTS` | `2` | Reserved procedure slots for Critic-recommended skills |
| `NOUS_EMBEDDING_SKILL_SLOTS` | `3` | Procedure slots for embedding similarity search |
| `NOUS_RUBRIC_ENABLED` | `true` | Enable self-modifying rubric system (F024-3b) |
| `NOUS_RUBRIC_OUTCOME_DETECTION_ENABLED` | `true` | Enable outcome signal detection on episodes |
| `NOUS_RUBRIC_EVOLUTION_ENABLED` | `false` | Enable weight/split/merge evolution (Phase 1+) |
| `NOUS_RUBRIC_MIN_EPISODES_FOR_CORRELATION` | `50` | Minimum episodes before correlation runs |
| `NOUS_RUBRIC_WEIGHT_CHANGE_CAP` | `0.05` | Max weight shift per adjustment cycle (±5%) |
| `NOUS_RUBRIC_MIN_DIMENSIONS` | `3` | Floor for dimension count |
| `NOUS_RUBRIC_MAX_DIMENSIONS` | `7` | Ceiling for dimension count |
| `NOUS_RUBRIC_MAX_VERSIONS_PER_WEEK` | `1` | Rate limit on rubric evolution |
| `NOUS_RUBRIC_OUTCOME_MODEL` | `claude-haiku-4-5-20251001` | Model for outcome classification |
| `NOUS_HEARTBEAT_ENABLED` | `true` | Enable heartbeat proactive monitoring |
| `NOUS_HEARTBEAT_TICK_INTERVAL` | `30` | Seconds between heartbeat tick loop iterations |
| `NOUS_HEARTBEAT_QUIET_START` | `23` | Quiet hours start (hour, user timezone) |
| `NOUS_HEARTBEAT_QUIET_END` | `8` | Quiet hours end (hour, user timezone) |
| `NOUS_HEARTBEAT_DAILY_TOKEN_BUDGET` | `50000` | Max tokens/day for heartbeat cognitive sessions |
| `NOUS_HEARTBEAT_EMAIL_ENABLED` | `false` | Enable email check (needs IMAP credentials) |
| `NOUS_HEARTBEAT_EMAIL_INTERVAL` | `180` | Seconds between email checks |
| `NOUS_HEARTBEAT_EMAIL_IMAP_HOST` | `imap.gmail.com` | IMAP server host |
| `NOUS_HEARTBEAT_HEALTH_INTERVAL` | `3600` | Seconds between health checks |
| `NOUS_HEARTBEAT_SELF_INITIATED_INTERVAL` | `1800` | Seconds between self-initiated checks |
| `NOUS_HEARTBEAT_ESCALATION_LOW_TO_NORMAL_HOURS` | `72` | Hours before low→normal finding escalation |
| `NOUS_HEARTBEAT_ESCALATION_NORMAL_TO_HIGH_HOURS` | `24` | Hours before normal→high finding escalation |
| `NOUS_HEARTBEAT_ESCALATION_HIGH_REALERT_HOURS` | `12` | Hours between high-urgency re-alerts |
| `NOUS_HEARTBEAT_ESCALATION_ACCUMULATION_THRESHOLD` | `5` | Acknowledged findings count to trigger collection escalation |
| `NOUS_HEARTBEAT_DIGEST_HOUR_UTC` | `9` | UTC hour for daily digest Telegram message |
| `NOUS_HEARTBEAT_SUPPRESSION_TTL_HOURS` | `24` | TTL for suppressed finding state |
| `NOUS_HEARTBEAT_TUNING_ENABLED` | `false` | Enable heartbeat self-tuning (F034.3) |
| `NOUS_HEARTBEAT_TUNING_INTERVAL_HOURS` | `168` | Hours between tuning passes (weekly) |
| `NOUS_HEARTBEAT_TUNING_MIN_SAMPLES` | `10` | Minimum outcome signals before adjusting params |
| `NOUS_HEARTBEAT_TUNING_LEARNING_RATE` | `0.1` | Max parameter change per cycle (fraction of range) |
| `NOUS_HEARTBEAT_TUNING_ROLLBACK_THRESHOLD` | `0.2` | Negative rate increase that triggers auto-rollback |
| `NOUS_HEARTBEAT_DEFAULT_CHECK_TIMEOUT` | `30` | Default max seconds per heartbeat check run |
| `NOUS_HEARTBEAT_MAX_DYNAMIC_CHECKS` | `10` | Maximum number of concurrent dynamic checks |
| `NOUS_HEARTBEAT_DYNAMIC_SYNC_TICKS` | `60` | Ticks between periodic dynamic check sync (re-loads from DB) |
| `NOUS_CACHE_BREAK_DETECTION_ENABLED` | `true` | Enable cache break detection logging (F036) |
| `NOUS_CACHE_SPLIT_SYSTEM_PROMPT` | `true` | Enable 3-tier system prompt splitting (F036) |
| `NOUS_CACHE_SINGLE_BREAKPOINT` | `true` | Use single cache breakpoint strategy (F036) |
| `NOUS_TOOL_SCHEMA_CACHE_ENABLED` | `true` | Cache tool schemas per frame (F036) |
| `NOUS_DAG_ENABLED` | `true` | Enable DAG orchestration (F038) |
| `NOUS_CORRECTION_EXTRACTION_ENABLED` | `true` | Enable correction learning pipeline (F039) |
| `NOUS_GRAPH_BACKFILL_ENABLED` | `true` | Enable graph densification backfill during sleep (F040) |
| `NOUS_GRAPH_BACKFILL_MAX_FACTS` | `50` | Max orphan facts to process per sleep cycle |
| `NOUS_GRAPH_BACKFILL_MAX_DECISIONS` | `30` | Max orphan decisions to process per sleep cycle |
| `NOUS_GRAPH_BACKFILL_MAX_EPISODES` | `30` | Max orphan episodes to process per sleep cycle |
| `NOUS_GRAPH_BACKFILL_MAX_PROCEDURES` | `20` | Max orphan procedures to process per sleep cycle |
| `NOUS_GRAPH_THRESHOLD_FACT_FACT` | `0.82` | Same-type threshold for fact↔fact linking |
| `NOUS_GRAPH_THRESHOLD_FACT_DECISION` | `0.72` | Cross-type threshold for fact→decision linking |
| `NOUS_GRAPH_THRESHOLD_FACT_EPISODE` | `0.70` | Cross-type threshold for fact→episode linking |
| `NOUS_GRAPH_THRESHOLD_DECISION_DECISION` | `0.78` | Same-type threshold for decision↔decision linking |
| `NOUS_GRAPH_THRESHOLD_EPISODE_EPISODE` | `0.75` | Same-type threshold for episode↔episode linking |
| `NOUS_GRAPH_THRESHOLD_PROCEDURE_ANY` | `0.70` | Threshold for procedure→fact/decision linking |
| `NOUS_GRAPH_HEALTH_ORPHAN_WARN_THRESHOLD` | `0.40` | Orphan rate threshold for health warnings |
| `NOUS_GRAPH_HEALTH_CHECK_ENABLED` | `true` | Enable graph health monitoring |

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Send message, get response |
| POST | `/chat/stream` | SSE streaming chat |
| DELETE | `/chat/{session_id}` | End conversation |
| GET | `/status` | Agent status + memory stats + calibration |
| GET | `/decisions` | List recent decisions |
| GET | `/decisions/unreviewed` | Unreviewed decisions |
| POST | `/decisions/{id}/review` | Review a decision |
| GET | `/decisions/{id}` | Decision detail |
| GET | `/episodes` | List recent episodes |
| GET | `/facts?q=query` | Search facts |
| GET | `/censors` | Active censors |
| PUT | `/censors/{id}` | Update censor fields (trigger_action, action_instruction, unblock_pattern) |
| GET | `/procedures` | List procedures |
| GET | `/frames` | Available cognitive frames |
| GET | `/calibration` | Calibration report |
| GET | `/identity` | Get agent identity |
| PUT | `/identity/{section}` | Update identity section |
| POST | `/reinitiate` | Re-run initiation protocol |
| GET | `/health` | Health check |
| POST | `/sleep/trigger` | Trigger sleep cycle |
| GET | `/subtasks` | List subtasks |
| GET | `/subtasks/{id}` | Subtask detail |
| DELETE | `/subtasks/{id}` | Cancel a subtask |
| GET | `/schedules` | List schedules |
| POST | `/schedules` | Create a schedule |
| DELETE | `/schedules/{id}` | Deactivate a schedule |
| GET | `/admin/search-weights` | Get search weights |
| POST | `/admin/search-weights` | Set search weights |
| GET | `/rubric` | Current rubric |
| GET | `/rubric/history` | Rubric version history |
| GET | `/rubric/signals` | Outcome signals |
| GET | `/rubric/proposals` | List dimension proposals |
| POST | `/rubric/propose-dimension` | Propose a new dimension |
| POST | `/rubric/proposals/{id}/approve` | Approve a proposal |
| POST | `/rubric/rollback` | Rollback rubric version |
| POST | `/rubric/evolve` | Trigger rubric evolution |
| GET | `/dashboard/graph` | Graph visualization data |
| GET | `/dashboard/calibration` | Calibration dashboard data |
| GET | `/dashboard/activity` | Activity dashboard data |
| GET | `/dashboard/health` | Health dashboard data |
| GET | `/dashboard/rubric` | Rubric dashboard data |
| GET | `/dashboard/admission` | Admission control dashboard |
| GET | `/dashboard/admission/rejected` | Rejected admission entries |
| GET | `/dashboard/ledger` | Execution ledger dashboard data |
| GET | `/dashboard/heartbeat` | Heartbeat dashboard data |
| GET | `/dashboard/density` | Graph density dashboard data (F040) |
| GET | `/heartbeat/status` | Heartbeat status, checks, budget |
| POST | `/heartbeat/trigger` | Force immediate heartbeat tick |
| PUT | `/heartbeat/config` | Update heartbeat intervals/budget at runtime |
| POST | `/heartbeat/check/{name}/trigger` | Force a specific check to run |
| POST | `/heartbeat/check/{name}/reset` | Reset circuit breaker for a failed check |
| GET | `/heartbeat/findings` | All tracked findings with state/age |
| POST | `/heartbeat/findings/{fingerprint}/acknowledge` | Acknowledge a finding |
| POST | `/heartbeat/findings/{fingerprint}/resolve` | Resolve a finding |
| POST | `/heartbeat/findings/{fingerprint}/dismiss` | Dismiss a finding (strong negative) |
| PUT | `/heartbeat/escalation-policy` | Update escalation thresholds |
| GET | `/heartbeat/tuning-report` | Latest tuning report |
| POST | `/heartbeat/tune` | Force a tuning pass |
| GET | `/heartbeat/checks/dynamic` | List all dynamic checks |
| POST | `/heartbeat/checks/dynamic` | Create a new dynamic check |
| PATCH | `/heartbeat/checks/dynamic/{name}` | Update a dynamic check |
| DELETE | `/heartbeat/checks/dynamic/{name}` | Delete a dynamic check |
| POST | `/heartbeat/checks/dynamic/{name}/trigger` | Force-run a dynamic check |

### Agent Tools

| Tool | Frame Access | Description |
|------|-------------|-------------|
| `record_decision` | decision, task, debug, conversation, question | Record a decision with confidence + reasoning |
| `recall_deep` | all | Search memory (decisions, facts, episodes) |
| `recall_recent` | all | Retrieve recent memory items |
| `learn_fact` | conversation, question, creative, task | Store a new fact |
| `learn_skill` | conversation, question, task | Register a skill from URL, local path, or inline markdown |
| `get_procedure` | all | Retrieve a specific procedure by ID |
| `create_censor` | all | Create a guardrail censor |
| `cache_retrieve` | all | Retrieve original content from SmartCompressed results |
| `bash` | task, debug, conversation, question | Execute shell commands |
| `read_file` | task, debug, question | Read file contents |
| `write_file` | task, creative | Write/create files |
| `spawn_task` | conversation, debug | Spawn a background subtask |
| `schedule_task` | conversation, debug | Schedule a recurring/one-shot task |
| `list_tasks` | conversation, question, decision, debug | List subtasks and schedules |
| `cancel_task` | conversation, question, decision, debug | Cancel a subtask or schedule |
| `web_search` | all | Search via multi-tier routing (Tavily/Exa/Brave) |
| `web_fetch` | all | Fetch and extract web content |
| `run_python` | conversation, question, debug, task | Execute Python with memory functions in scope |
| `send_file` | task, conversation, debug | Send files to Telegram (images as photos, rest as documents) |
| `heartbeat_check_create` | conversation, debug | Create a new dynamic heartbeat check (supports on_complete callback) |
| `heartbeat_check_manage` | conversation, debug | List, enable, disable, delete, or update dynamic checks |

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
