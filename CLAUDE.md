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
│   │   ├── models.py           # ORM models for all 27 tables
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
│   │   └── schemas.py          # Finding, CheckResult, HeartbeatResult
│   ├── identity/               # Agent identity system (F018)
│   │   ├── manager.py          # Identity section CRUD
│   │   ├── protocol.py         # Initiation protocol
│   │   └── tools.py            # Identity-related tools
│   └── api/                    # External interfaces
│       ├── rest.py             # Starlette REST API (47 endpoints)
│       ├── mcp.py              # MCP server (nous_chat, nous_decide, etc.)
│       ├── runner.py           # Agent runner (tool loop, streaming)
│       ├── tools.py            # Tool dispatcher + registration
│       ├── builtin_tools.py    # bash, read_file, write_file
│       ├── web_tools.py        # web_search, web_fetch (multi-tier routing)
│       ├── search_providers.py # SearchProvider protocol + Tavily, Exa, Brave
│       ├── search_router.py   # Query classification + cascading fallback
│       ├── compaction.py       # History compaction engine
│       ├── smart_compress.py   # Smart compression for tool results
│       ├── tool_cache.py       # Tool result caching
│       └── models.py           # API request/response models
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
| 001 | Postgres scaffold (19 base + 4 migration tables, 3 schemas) | #1 |
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
| F033 | Multi-Tier Search Routing (Tavily primary, Exa research, Brave fallback, query classification) | — |
| F034 | Heartbeat Proactive Monitoring (tick loop, health/email/self-initiated checks, triage, Telegram) | #236 |
| 012.3 | Programmatic Tool Calling (run_python with memory functions in scope) | — |

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

- Three schemas: `brain`, `heart`, `nous_system` (27 tables total)
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
| `NOUS_SUBTASK_DEFAULT_TIMEOUT` | `120` | Default subtask timeout (seconds) |
| `NOUS_SUBTASK_MAX_TIMEOUT` | `600` | Maximum allowed timeout |
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
| `NOUS_STALENESS_HALF_LIFE_DAYS` | `14` | Half-life in days for staleness decay |
| `NOUS_RRF_K` | `60` | RRF smoothing constant for hybrid search rank fusion |
| `NOUS_TOOL_TIMEOUT` | `120` | Max seconds for any single tool execution |
| `NOUS_KEEPALIVE_INTERVAL` | `10` | Seconds between keepalive events during tool execution |
| `NOUS_GRAPH_RECALL_ENABLED` | `true` | Enable graph expansion in recall_deep |
| `NOUS_GRAPH_RECALL_MAX_EXPAND` | `5` | Max seed results to expand |
| `NOUS_GRAPH_RECALL_DECAY` | `0.7` | Score decay per graph hop |
| `NOUS_GRAPH_RECALL_MAX_NEIGHBORS` | `3` | Max neighbors per seed |
| `NOUS_CROSS_TYPE_LINKING_ENABLED` | `true` | Enable cross-type auto-linking |
| `NOUS_CROSS_TYPE_THRESHOLD` | `0.80` | Cross-type similarity threshold |
| `NOUS_CONTRADICTION_DETECTION` | `true` | Enable LLM contradiction detection |
| `NOUS_CONTRADICTION_MODEL` | `claude-haiku-4-5-20251001` | Model for contradiction classification |
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
| `NOUS_MMR_ENABLED` | `false` | Enable MMR diversity re-ranking in recall_deep |
| `NOUS_MMR_DIVERSITY_WEIGHT` | `0.7` | MMR relevance vs diversity weight (1.0=pure relevance, 0.0=pure diversity) |
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
| GET | `/heartbeat/status` | Heartbeat status, checks, budget |
| POST | `/heartbeat/trigger` | Force immediate heartbeat tick |
| PUT | `/heartbeat/config` | Update heartbeat intervals/budget at runtime |
| POST | `/heartbeat/check/{name}/trigger` | Force a specific check to run |
| POST | `/heartbeat/check/{name}/reset` | Reset circuit breaker for a failed check |

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
| `web_search` | all | Search via Brave API |
| `web_fetch` | all | Fetch and extract web content |
| `run_python` | conversation, question, debug, task | Execute Python with memory functions in scope |
| `send_file` | task, conversation, debug | Send files to Telegram (images as photos, rest as documents) |

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
