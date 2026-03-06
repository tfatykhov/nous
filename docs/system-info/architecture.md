# Nous Architecture

> Complete system architecture derived from source code analysis (March 2026).

## System Overview

Nous is a cognitive AI agent built on Marvin Minsky's Society of Mind architecture. It wraps Anthropic Claude with persistent memory (Heart), decision intelligence (Brain), a cognitive processing layer, and an asynchronous event system — all backed by PostgreSQL with pgvector.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                             │
│                   (polls TG API → /chat REST)                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────────┐
│                     REST API (Starlette)                        │
│         /chat  /chat/stream  /mcp  /admin/*  /health            │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                      AgentRunner                                │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐    │
│  │ Tool Loop   │  │  Compaction   │  │  Decision Quality   │    │
│  │ (max 25)    │  │  (2-layer)    │  │  Gate               │    │
│  └─────┬───────┘  └──────────────┘  └─────────────────────┘    │
│        │                                                        │
│  ┌─────▼───────────────────────────────────────────────────┐    │
│  │              ToolDispatcher (15 tools)                   │    │
│  │  recall_deep | learn_fact | record_decision | bash       │    │
│  │  read_file | write_file | web_search | web_fetch         │    │
│  │  spawn_task | schedule_task | list_tasks | cancel_task    │    │
│  │  create_censor | recall_recent | run_python              │    │
│  └─────────────────────────────────────────────────────────┘    │
└──────────┬──────────────────────────────────┬───────────────────┘
           │                                  │
┌──────────▼──────────┐          ┌────────────▼───────────────────┐
│   CognitiveLayer    │          │         EventBus               │
│  ┌───────────────┐  │          │   asyncio.Queue (max 1000)     │
│  │ ContextEngine │  │          │   1s poll interval             │
│  │ (tiered T0-T4)│  │          └────────────┬───────────────────┘
│  ├───────────────┤  │                       │
│  │ FrameDetector │  │          ┌────────────▼───────────────────┐
│  │ (5 modes)     │  │          │        Event Handlers          │
│  ├───────────────┤  │          │  FactExtractor                 │
│  │ Deliberation  │  │          │  EpisodeSummarizer             │
│  │ (pre-turn)    │  │          │  DecisionReviewer              │
│  ├───────────────┤  │          │  KnowledgeExtractor            │
│  │ IntentEngine  │  │          │  SessionMonitor                │
│  ├───────────────┤  │          │  SleepHandler                  │
│  │ DedupEngine   │  │          └────────────────────────────────┘
│  ├───────────────┤  │
│  │ UsageTracker  │  │
│  └───────────────┘  │
└──────────┬──────────┘
           │
     ┌─────▼─────┐     ┌──────────┐     ┌─────────────────┐
     │   Heart    │     │  Brain   │     │ IdentityManager │
     │ (memory)   │     │(decisions│     │ (5 sections)    │
     └─────┬──────┘     │ quality) │     └────────┬────────┘
           │            └────┬─────┘              │
           │                 │                    │
     ┌─────▼─────────────────▼────────────────────▼──────┐
     │              PostgreSQL + pgvector                  │
     │     brain schema | heart schema | nous_system       │
     └────────────────────────────────────────────────────┘
```

---

## Component Reference

### 1. Main Entry Point (`main.py`, ~280 lines)

The boot orchestrator. Creates all components in dependency order and wires them together.

**Boot Sequence:**
1. `Settings` — load from environment (`NOUS_*` prefix)
2. `Database` — PostgreSQL async engine (SQLAlchemy)
3. `Migrator` — run pending schema migrations
4. `EmbeddingProvider` — Voyage AI embeddings (voyage-3-lite, 1024-dim)
5. `Brain` — decision intelligence facade
6. `Heart` — memory system facade
7. `EventBus` — async event queue
8. `IdentityManager` — agent identity CRUD
9. `CognitiveLayer` — frames, context, deliberation
10. **Event Handlers** (6): FactExtractor, EpisodeSummarizer, DecisionReviewer, KnowledgeExtractor, SessionMonitor, SleepHandler
11. `ToolDispatcher` — register all 15 agent tools
12. `AgentRunner` — Claude API orchestration
13. `SubtaskWorkerPool` — parallel background agent workers
14. `TaskScheduler` — cron/interval/one-shot scheduling
15. REST API — Starlette ASGI app + optional MCP mount

**Shutdown:** Reverse order — scheduler → worker pool → event bus → heart → brain → database.

**Key patterns:**
- All components receive `Settings` + their dependencies via constructor injection
- No global state; everything flows through the component graph
- `lifespan()` async context manager handles startup/shutdown

---

### 2. Configuration (`config.py`, ~180 lines)

Pydantic `BaseSettings` model with `NOUS_` environment variable prefix.

**Key Settings Groups:**

| Group | Settings | Defaults |
|-------|----------|----------|
| **Database** | `db_url`, `db_pool_size`, `db_max_overflow` | pool=5, overflow=10 |
| **LLM** | `anthropic_api_key`, `anthropic_auth_token`, `model` | claude-sonnet-4-20250514 |
| **Subtasks** | `subtask_model`, `subtask_max_timeout`, `subtask_tool_call_limit`, `subtask_max_pending` | claude-haiku-3-5-20241022, 600s, 20, 5 |
| **Embeddings** | `embedding_provider`, `voyage_api_key`, `embedding_dim` | voyage, 1024 |
| **Identity** | `agent_id`, `agent_name` | "nous", "Nous" |
| **Limits** | `max_tool_rounds`, `max_context_tokens`, `compaction_threshold` | 25, 180000, 120000 |
| **Memory** | `session_timeout_minutes`, `fact_extraction_model` | 30, claude-haiku |
| **Telegram** | `telegram_bot_token`, `telegram_allowed_users` | — |
| **Search** | `search_provider`, `exa_api_key`, `brave_api_key` | brave |
| **MCP** | `mcp_enabled` | false |
| **Email** | `email_enabled`, `smtp_*`, `email_from` | false |

**Model routing:**
- Main conversation: `model` (claude-sonnet-4)
- Subtasks: `subtask_model` (claude-haiku — ⚠️ currently broken, see issue #119)
- Fact extraction: `fact_extraction_model`
- Sleep/background: uses handler-specific models

---

### 3. Brain — Decision Intelligence (`brain/`, ~1,700 lines)

The Brain records, scores, reviews, and retrieves decisions.

**brain.py** (~600 lines) — Facade class:
- `record()` — store a decision with embedding
- `query()` — semantic search for similar past decisions
- `check()` — pre-decision check against existing decisions
- `review()` — periodic quality review pipeline
- `think()` — deliberation recording
- `link()` / `auto_link()` — connect related decisions
- `calibrate()` — compute calibration scores

**quality.py** (~290 lines) — `QualityScorer`:
- Scores on 5 signals: stakes alignment, confidence calibration, reasoning diversity, context richness, tag specificity
- Each signal 0.0–1.0 with explanation
- Composite weighted score; threshold default 0.5

**guardrails.py** (~200 lines) — `GuardrailEngine`:
- Checks against active censors
- Three levels: `warn`, `block`, `absolute`
- Pattern matching via substring or regex

**calibration.py** (~180 lines) — `CalibrationEngine`:
- Predicted confidence vs actual outcomes
- Brier scores over time
- Calibration reports by tag/category

**bridge.py** (~150 lines) — `BridgeExtractor`:
- Cross-domain decision linking
- Embedding similarity for analogous decisions

**embeddings.py** (~120 lines) — `EmbeddingProvider`:
- Voyage AI (voyage-3-lite, 1024-dim)
- Batch embedding support
- Used by both Brain and Heart

---

### 4. Heart — Memory System (`heart/`, ~2,800 lines)

All persistent memory: episodes, facts, censors, procedures, working memory, subtasks, schedules.

**heart.py** (~400 lines) — Facade delegating to sub-modules.

**search.py** (~350 lines) — `HybridSearch`:
- Keyword (trigram, weight 0.3) + embedding (weight 0.7)
- Score normalization and ranking
- Searches facts, episodes, decisions, procedures, censors

**facts.py** (~280 lines) — `FactStore`:
- Categories: preference, technical, person, tool, concept, rule
- Embedding search, confidence tracking, deduplication

**episodes.py** (~250 lines) — `EpisodeStore`:
- Lifecycle: create → update → summarize → close
- Session-based grouping with structured summaries

**censors.py** (~200 lines) — `CensorStore`:
- Three action levels, domain scoping, active/inactive

**procedures.py** (~180 lines) — `ProcedureStore`:
- Learned workflows with steps, versioning, confidence

**working_memory.py** (~250 lines) — `WorkingMemoryManager`:
- Per-session scratch space, thread-based organization
- Auto-cleanup on session end

**subtasks.py** (~300 lines) — `SubtaskStore`:
- Lifecycle: pending → running → completed/failed/cancelled
- Parent-child tracking

**schedules.py** (~280 lines) — `ScheduleStore`:
- One-shot, interval, cron-based scheduling
- Fire count tracking, next-fire-at computation

---

### 5. Cognitive Layer (`cognitive/`, ~1,900 lines)

Thinking layer between raw LLM calls and memory.

**layer.py** (~350 lines) — `CognitiveLayer` facade:
- `prepare_turn()` — full context assembly
- `post_turn()` — insight extraction, working memory update

**context.py** (~400 lines) — `ContextEngine`:
- **Tiered context assembly:**
  - **T0 (Critical):** Identity, censors, working memory, user profile
  - **T1 (High):** Rules, preferences, tool instructions, output formatting
  - **T2 (Medium):** Related decisions from Brain
  - **T3 (Standard):** Semantic search (episodes, facts)
  - **T4 (Low):** Temporal recall (recent episodes)
- Token budget management per tier

**frames.py** (~200 lines) — `FrameDetector`:
- 5 frames: conversation, task, research, decision, debug
- Determines tool gating

**deliberation.py** (~300 lines) — `DeliberationEngine`:
- Pre-turn reasoning before LLM call
- Generates traces stored in Brain

**intent.py** (~200 lines) — `IntentEngine`:
- Keyword + pattern classification (no LLM call)
- Extracts action, entities, urgency

**dedup.py** (~180 lines) — `DedupEngine`:
- Prevents redundant context via embedding similarity

**monitor.py** (~80 lines) — `ConversationMonitor`:
- Tracks turn count, tokens, tool calls; triggers compaction

**usage_tracker.py** (~100 lines) — `UsageTracker`:
- Per-session/per-model API usage and cost estimation

---

### 6. API Layer (`api/`, ~3,200 lines)

**runner.py** (~900 lines) — `AgentRunner`:
- Core loop: `run_turn(message, session_id)` → response
- **Pipeline:** pre-turn → LLM call → tool loop (max 25) → post-turn
- Compaction when history exceeds threshold
- Streaming via `run_turn_stream()`

**tools.py** (~400 lines) — `ToolDispatcher`:
- Registry, dispatch, frame gating, censor checks
- Tool result formatting and truncation

**builtin_tools.py** (~500 lines) — Core tools:
- Memory: record_decision, learn_fact, recall_deep, recall_recent, create_censor
- Tasks: spawn_task, schedule_task, list_tasks, cancel_task
- Code: run_python

**web_tools.py** (~350 lines) — External tools:
- bash, read_file, write_file, web_search, web_fetch
- Security: command blocking, path validation

**compaction.py** (~350 lines):
- Layer 1: tool output pruning
- Layer 2: LLM-based history summarization
- Fires `conversation_compacting` event

**rest.py** (~500 lines) — Starlette ASGI:
- `POST /chat`, `POST /chat/stream`
- Admin endpoints: facts, episodes, decisions, censors, identity, metrics, sessions, sleep, subtasks, schedules
- `GET /health`, `GET /status`

**mcp.py** (~200 lines) — Optional MCP server:
- 5 tools: store_identity, complete_initiation, recall, decide, status

**models.py** (~80 lines) — Pydantic request/response models

---

### 7. Event System (`events.py`, ~120 lines)

**EventBus** — asyncio.Queue (max 1000), 1s poll.

**Event types:** session_ended, episode_summarized, conversation_compacting, sleep_start/stop, decision_recorded

---

### 8. Event Handlers (`handlers/`, ~2,400 lines)

| Handler | Listens To | Action |
|---------|-----------|--------|
| **FactExtractor** | `episode_summarized` | LLM extracts facts from summaries, deduplicates, stores |
| **EpisodeSummarizer** | `session_ended` | LLM summarizes conversation, stores episode |
| **DecisionReviewer** | `session_ended` | Quality scoring, confidence calibration |
| **KnowledgeExtractor** | `conversation_compacting` | Extracts facts before compaction |
| **SessionMonitor** | timer (60s) | Detects inactivity, fires session_ended |
| **SleepHandler** | `sleep_start` | 5-phase cycle: replay → consolidate → prune → reflect → generalize |

---

### 9. Subtask & Scheduling System (~850 lines)

**SubtaskWorkerPool** — parallel agent workers, `max_pending` (5), `tool_call_limit` (20), `max_timeout` (600s). Each subtask gets own AgentRunner. Supports model/frame override.

**TaskScheduler** — one-shot, interval, recurring. Checks every 30s. Spawns subtask per fire.

**TimeParser** — "in 2 hours", "tomorrow at 9am", "every monday at 10am"

---

### 10. Identity System (`identity/`, ~750 lines)

5 sections: character, values, protocols, preferences, boundaries. Version tracking, 60s TTL cache, initiation protocol for first-run setup.

---

### 11. Telegram Bot (`telegram_bot.py`, ~800 lines)

Long-polling → `/chat` REST. Markdown → HTML conversion. Message splitting >4096 chars. User allowlist.

---

### 12. Storage Layer (`storage/`, ~1,200 lines)

SQLAlchemy async + PostgreSQL. Forward-only migrations. 22 ORM models across 3 schemas (nous_system, brain, heart).
