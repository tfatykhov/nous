# Nous Architecture — System Overview

> **Nous** (Greek: νοῦς, "mind") is a cognitive AI agent framework built on Marvin Minsky's
> *Society of Mind* architecture. It is not an LLM wrapper — it is a thinking system that
> uses an LLM (Claude) as its reasoning engine while managing perception, memory, learning,
> and metacognition through dedicated software subsystems.

---

## Core Principle

```
The LLM handles "Act" — generating responses and using tools.
Everything else — sensing, framing, recalling, deliberating, monitoring, learning —
is handled by deterministic software components.
```

This separation means Nous can:
- Control what context the LLM sees (not just dump everything in)
- Learn from conversations without the LLM deciding what to remember
- Make calibrated decisions with tracked outcomes
- Self-correct through guardrails that the LLM cannot override

---

## Architecture Diagram

```
                          ┌─────────────────────────────────────┐
                          │            CLIENTS                  │
                          │  Telegram Bot · REST API · MCP      │
                          └────────────┬────────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────────┐
                          │          AgentRunner                │
                          │   (api/runner.py)                   │
                          │                                     │
                          │  • Manages conversations            │
                          │  • Orchestrates tool-use loops      │
                          │  • Calls Anthropic Messages API     │
                          │  • Delegates to CognitiveLayer      │
                          └────────────┬────────────────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
                    ▼                  ▼                  ▼
        ┌───────────────┐  ┌───────────────┐  ┌───────────────────┐
        │   Cognitive    │  │    Brain      │  │      Heart        │
        │    Layer       │  │ (Decision     │  │  (Memory System)  │
        │ (Orchestrator) │  │ Intelligence) │  │                   │
        │                │  │               │  │ • Episodes        │
        │ • Frames       │  │ • Decisions   │  │ • Facts           │
        │ • Context      │  │ • Bridges     │  │ • Procedures      │
        │ • Intent       │  │ • Calibration │  │ • Censors         │
        │ • Deliberation │  │ • Quality     │  │ • Working Memory  │
        │ • Monitor      │  │ • Guardrails  │  │ • Subtasks        │
        │ • Usage Track  │  │ • Embeddings  │  │ • Schedules       │
        │ • Dedup        │  │               │  │ • Search          │
        └───────┬───────┘  └───────┬───────┘  └─────────┬─────────┘
                │                  │                     │
                └──────────────────┼─────────────────────┘
                                   │
                                   ▼
                          ┌─────────────────────────────────────┐
                          │         EventBus                    │
                          │   (Async event-driven wiring)       │
                          │                                     │
                          │  Events: session_ended, session_    │
                          │  timeout, sleep, task_completed...  │
                          └────────────┬────────────────────────┘
                                       │
                    ┌──────────┬───────┼────────┬──────────┐
                    ▼          ▼       ▼        ▼          ▼
              ┌──────────┐ ┌────────┐ ┌──────┐ ┌────────┐ ┌────────┐
              │ Episode  │ │ Fact   │ │Sleep │ │Decision│ │ Task   │
              │Summarizer│ │Extract.│ │Hdlr  │ │Reviewer│ │Schedul.│
              └──────────┘ └────────┘ └──────┘ └────────┘ └────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────────┐
                          │         PostgreSQL + pgvector       │
                          │                                     │
                          │  3 Schemas:                         │
                          │  • nous_system (agents, frames,     │
                          │    identities, conv state)          │
                          │  • brain (decisions, bridges,       │
                          │    reasons, tags, events, reviews)  │
                          │  • heart (episodes, facts, censors, │
                          │    procedures, subtasks, schedules, │
                          │    working_memory, search_index)    │
                          └─────────────────────────────────────┘
```

---

## Component Inventory

### 1. Entry Point — `main.py`

**Purpose:** Bootstrap and startup sequence.

**Initialization Order:**
```
Settings → Database → EmbeddingProvider → Brain → Heart → 
CognitiveLayer → EventBus → Handlers → AgentRunner → 
ToolDispatcher → REST App → Telegram Bot → Uvicorn
```

**Key Functions:**
- `create_components(settings)` — Builds all components in dependency order
- `build_app(settings)` — Creates Starlette ASGI app with lifespan manager
- `main()` — Entry point, parses settings and runs uvicorn

**Lifespan Events:**
- **Startup:** Creates components, runs migrations, auto-creates agent record, starts event bus + handlers
- **Shutdown:** Stops handlers, event bus, closes DB connections, closes HTTP client

---

### 2. Configuration — `config.py`

**Purpose:** Centralized settings via Pydantic `BaseSettings`.

**Key Settings Groups:**
| Group | Examples |
|-------|---------|
| Database | `db_host`, `db_port`, `db_user`, `db_password`, `db_name` |
| LLM | `anthropic_api_key`, `model` (default: `claude-sonnet-4-5-20250514`), `max_tokens` |
| Identity | `agent_id`, `agent_name`, `identity_prompt` |
| Brain | `auto_link_threshold`, `quality_block_threshold` |
| Features | `event_bus_enabled`, `episode_summary_enabled`, `fact_extraction_enabled` |
| Scheduling | `session_idle_timeout`, `sleep_timeout`, `schedule_check_interval` |
| Background | `background_model`, `subtask_max_concurrent`, `subtask_default_timeout` |
| MCP | `mcp_enabled` |
| Telegram | `telegram_bot_token`, `telegram_allowed_user_ids` |
| Web | `brave_search_api_key`, `exa_api_key` |

**Environment:** Uses `NOUS_` prefix for env vars, with `validation_alias` for Docker-compatible unprefixed vars.

---

### 3. Cognitive Layer — `cognitive/layer.py` (The Orchestrator)

**Purpose:** The "thinking loop" — everything between receiving user input and returning a response. This is the brain of Nous.

**Key Class:** `CognitiveLayer`

**Key Methods:**
- `pre_turn()` — Called before the LLM. Builds the full system prompt with all context.
- `post_turn()` — Called after the LLM. Handles learning, monitoring, and session management.
- `end_session()` — Generates reflection, extracts facts, cleans up.

**Sub-engines (composed inside CognitiveLayer):**

| Engine | File | Purpose |
|--------|------|---------|
| `FrameEngine` | `cognitive/frames.py` | Pattern-matches input to cognitive frame (no LLM) |
| `ContextEngine` | `cognitive/context.py` | Assembles system prompt within token budgets |
| `IntentClassifier` | `cognitive/intent.py` | Classifies intent and generates retrieval plan |
| `DeliberationEngine` | `cognitive/deliberation.py` | Decides what to retrieve and why |
| `MonitorEngine` | `cognitive/monitor.py` | Tracks response quality metrics |
| `ConversationDeduplicator` | `cognitive/dedup.py` | Removes redundant context |
| `UsageTracker` | `cognitive/usage_tracker.py` | Tracks memory usage effectiveness |

---

### 4. Brain — `brain/brain.py` (Decision Intelligence)

**Purpose:** Records, retrieves, links, and reviews decisions. Provides the "learning from experience" capability.

**Key Class:** `Brain`

**Key Capabilities:**
- **Record decisions** with structured reasoning (analysis, pattern, empirical, etc.)
- **Auto-link** related decisions via embedding similarity
- **Bridge decisions** to Heart memories (episodes, facts)
- **Review decisions** — mark as success/failure/partial with lessons learned
- **Calibration** — track predicted confidence vs actual outcomes
- **Quality gate** — reject low-quality decisions before storage

**Sub-components:**

| Component | File | Purpose |
|-----------|------|---------|
| `EmbeddingProvider` | `brain/embeddings.py` | OpenAI embeddings for similarity search |
| `BridgeEngine` | `brain/bridge.py` | Links decisions to related memories |
| `CalibrationEngine` | `brain/calibration.py` | Computes Brier scores and calibration curves |
| `GuardrailEngine` | `brain/guardrails.py` | Pre-record checks (quality, duplicates) |
| `QualityGate` | `brain/quality.py` | Scores decision quality (specificity, reasoning, confidence) |

---

### 5. Heart — `heart/heart.py` (Memory System)

**Purpose:** Manages all persistent memory — episodic, semantic, procedural, and working memory.

**Key Class:** `Heart`

**Key Capabilities:**
- **Episodes** — Conversation summaries with topic extraction
- **Facts** — Categorized knowledge (preference, technical, person, tool, concept, rule)
- **Procedures** — Step-by-step instructions
- **Censors** — Guardrails that filter/block/warn on content patterns
- **Working memory** — Temporary session-scoped context (threads)
- **Subtasks** — Background task queue with worker execution
- **Schedules** — Recurring and one-shot timed tasks
- **Search** — Unified search across all memory types with hybrid scoring
- **Conversation state** — Persists active conversations across restarts

**Sub-components:**

| Component | File | Purpose |
|-----------|------|---------|
| `EpisodeManager` | `heart/episodes.py` | CRUD for conversation episodes |
| `FactManager` | `heart/facts.py` | CRUD + search for facts |
| `CensorManager` | `heart/censors.py` | Censor CRUD + pattern matching |
| `ProcedureManager` | `heart/procedures.py` | Procedure CRUD |
| `WorkingMemoryManager` | `heart/working_memory.py` | Session-scoped temp storage |
| `SubtaskManager` | `heart/subtasks.py` | Background task queue |
| `ScheduleManager` | `heart/schedules.py` | Schedule CRUD + due-check |
| `SearchEngine` | `heart/search.py` | Unified cross-type search |

---

### 6. EventBus — `events.py`

**Purpose:** In-process async event system that decouples components. Handlers subscribe to event types and are called concurrently when events fire.

**Key Class:** `EventBus`

**Design:**
- Async queue-based (max 1000 events)
- Error-isolated — one handler failure never affects others
- DB persistence — all events stored in audit table
- Background task — processes events asynchronously

**Event Types:**
| Event | Trigger | Handlers |
|-------|---------|----------|
| `session_ended` | User ends chat or timeout | EpisodeSummarizer, FactExtractor, DecisionReviewer |
| `session_timeout` | Idle timeout (30min default) | SessionMonitor → CognitiveLayer.end_session |
| `session_sleep` | Long idle (2hr default) | SleepHandler → generates dream/reflection |
| `task_completed` | Subtask finishes | SubtaskWorker notification |
| `schedule_fired` | Scheduled task triggers | TaskScheduler → enqueue subtask |

---

### 7. Handlers — `handlers/`

**Purpose:** Event-driven background processors that listen to EventBus events.

| Handler | File | Listens To | Does |
|---------|------|-----------|------|
| `EpisodeSummarizer` | `episode_summarizer.py` | `session_ended` | LLM-generates episode summaries from transcripts |
| `FactExtractor` | `fact_extractor.py` | `session_ended` | LLM-extracts facts from conversation transcripts |
| `KnowledgeExtractor` | `knowledge_extractor.py` | `session_ended` | Extracts topic knowledge from conversations |
| `DecisionReviewer` | `decision_reviewer.py` | `session_ended` + periodic sweep | Auto-reviews decisions with verifiable outcomes |
| `SessionMonitor` | `session_monitor.py` | Periodic check | Detects idle sessions and triggers timeouts |
| `SleepHandler` | `sleep_handler.py` | `session_sleep` | Generates "dream" reflections during long idle |
| `SubtaskWorker` | `subtask_worker.py` | Continuous | Picks up queued subtasks, runs them via separate LLM calls |
| `TaskScheduler` | `task_scheduler.py` | Periodic check | Fires due schedules by enqueuing them as subtasks |

---

### 8. API Layer — `api/`

| Component | File | Purpose |
|-----------|------|---------|
| `AgentRunner` | `runner.py` | Main conversation executor — manages tool loop, history, streaming |
| `ToolDispatcher` | `tools.py` | Registers and dispatches tool calls |
| Nous Memory Tools | `tools.py` | `record_decision`, `learn_fact`, `recall_deep`, `create_censor` |
| Builtin Tools | `builtin_tools.py` | `bash`, `read_file`, `write_file`, `spawn_task`, `schedule_task`, `list_tasks`, `cancel_task`, `run_python` |
| Web Tools | `web_tools.py` | `web_search` (Brave + Exa fallback), `web_fetch` |
| `ConversationCompactor` | `compaction.py` | LLM-summarizes history when it exceeds token limits |
| REST API | `rest.py` | 23 HTTP endpoints for external access |
| MCP Server | `mcp.py` | Model Context Protocol server (5 tools) |
| Data Models | `models.py` | Pydantic models for API request/response |

**REST Endpoints:**
```
POST /chat              — Send message, get response
POST /chat/stream       — SSE streaming response
DELETE /chat/{session}  — End a chat session
GET  /status            — Agent status + calibration
GET  /decisions         — List decisions
GET  /decisions/{id}    — Get single decision
GET  /decisions/unreviewed — Unreviewed decisions
POST /decisions/{id}/review — Review a decision
GET  /episodes          — List episodes
GET  /facts             — Search facts
GET  /censors           — List active censors
GET  /frames            — List cognitive frames
GET  /calibration       — Decision calibration data
GET  /identity          — Get identity config
PUT  /identity/{section} — Update identity section
POST /reinitiate        — Re-run initiation protocol
GET  /subtasks          — List subtasks
GET  /subtasks/{id}     — Get subtask details
DELETE /subtasks/{id}   — Cancel subtask
GET  /schedules         — List schedules
POST /schedules         — Create schedule
DELETE /schedules/{id}  — Deactivate schedule
GET  /health            — Health check
```

**MCP Tools:**
```
nous_chat    — Send a message, get a response
nous_recall  — Search across all memory types
nous_status  — Get agent status and calibration
nous_teach   — Add a fact or procedure
nous_decide  — Force a decision (uses decision frame)
```

---

### 9. Identity System — `identity/`

**Purpose:** Manages the agent's persistent identity — character, values, protocols, preferences, boundaries.

| Component | File | Purpose |
|-----------|------|---------|
| `IdentityManager` | `manager.py` | CRUD for identity sections |
| `InitiationProtocol` | `protocol.py` | First-run onboarding flow |
| Identity Tools | `tools.py` | `store_identity`, `complete_initiation` tools |

**Identity Sections:**
- `character` — Name, personality, behavioral traits
- `values` — Core principles and priorities
- `protocols` — Decision-making and memory processes
- `preferences` — User preferences and rules
- `boundaries` — Hard limits and restrictions

---

### 10. Storage — `storage/`

**Purpose:** Database abstraction layer.

| Component | File | Purpose |
|-----------|------|---------|
| `Database` | `database.py` | Async SQLAlchemy engine + session factory |
| `Migrator` | `migrator.py` | Alembic migration runner |
| ORM Models | `models.py` | 22 SQLAlchemy models across 3 schemas |

**Database Schemas:**

```
nous_system (4 tables)
├── agents            — Agent registry
├── frames            — Cognitive frame definitions
├── identities        — Identity section storage
└── conversation_states — Persisted active conversations

brain (7 tables)
├── decisions         — Decision records with embeddings
├── decision_tags     — Tag associations
├── decision_reasons  — Structured reasoning
├── decision_bridge   — Links to Heart memories
├── decision_reviews  — Review outcomes
├── events            — Audit event log
└── calibration_snapshots — Calibration data

heart (11 tables)
├── episodes          — Conversation summaries
├── facts             — Categorized knowledge
├── censors           — Guardrail rules
├── procedures        — Step-by-step instructions
├── working_memory    — Session-scoped temp data
├── subtasks          — Background task queue
├── schedules         — Recurring/one-shot timers
├── search_index      — Unified search with embeddings
├── fact_tags         — Fact tag associations
├── episode_topics    — Episode topic extraction
└── procedure_tags    — Procedure tag associations
```

---

### 11. Telegram Integration — `telegram_bot.py`

**Purpose:** Telegram bot interface — maps Telegram messages to Nous conversations.

**Key Features:**
- Maps Telegram chat IDs to Nous session IDs
- User allowlisting via `telegram_allowed_user_ids`
- Message queue with typing indicators
- Scripting indicator (💻) when Nous is executing code/tools
- Push notifications for subtask completion
- Group chat support with bot mention detection

---

### 12. Utilities — `utils.py`

**Purpose:** Shared helpers used across modules.

**Key Functions:**
- `count_tokens()` — tiktoken-based token counting
- `text_overlap()` — Containment coefficient for deduplication
- `truncate_text()` — Smart truncation respecting word boundaries
- `sanitize_for_embedding()` — Clean text for embedding generation
