# Nous System Documentation

> Technical documentation for the Nous cognitive agent, derived from source code analysis.

## Documents

### [Architecture](architecture.md)
Complete system architecture — all 12 components, database schemas, REST endpoints, tool reference, and configuration settings. Start here for the big picture.

### [Loop Flow](loop-flow.md)
How a message flows through the system: the 4-phase turn pipeline (pre-turn → LLM execution → post-turn → background), context assembly tiers, tool loop mechanics, compaction, and all background processing flows (session lifecycle, sleep cycle, subtask execution, scheduling).

## Quick Stats

| Metric | Value |
|--------|-------|
| Total source files | ~51 Python files |
| Lines of code | ~60,100 |
| Components | 12 major modules |
| Database tables | 22 across 3 schemas |
| Agent tools | 15 |
| MCP tools | 5 |
| REST endpoints | 23+ |
| Event handlers | 6 |
| Cognitive frames | 5 |
| Context tiers | 5 (T0–T4) |
| Max tool rounds | 25 per turn |
| Embedding dimensions | 1024 (Voyage AI) |

## Source Structure

```
nous/
├── main.py              # Boot orchestrator
├── config.py            # Settings (Pydantic)
├── events.py            # EventBus
├── utils.py             # Utilities
├── telegram_bot.py      # Telegram bridge
│
├── brain/               # Decision intelligence
│   ├── brain.py         # Facade
│   ├── quality.py       # QualityScorer
│   ├── guardrails.py    # GuardrailEngine
│   ├── calibration.py   # CalibrationEngine
│   ├── bridge.py        # BridgeExtractor
│   └── embeddings.py    # EmbeddingProvider
│
├── heart/               # Memory system
│   ├── heart.py         # Facade
│   ├── search.py        # HybridSearch
│   ├── facts.py         # FactStore
│   ├── episodes.py      # EpisodeStore
│   ├── censors.py       # CensorStore
│   ├── procedures.py    # ProcedureStore
│   ├── working_memory.py# WorkingMemoryManager
│   ├── subtasks.py      # SubtaskStore
│   └── schedules.py     # ScheduleStore
│
├── cognitive/           # Thinking layer
│   ├── layer.py         # CognitiveLayer facade
│   ├── context.py       # ContextEngine (tiered)
│   ├── frames.py        # FrameDetector
│   ├── deliberation.py  # DeliberationEngine
│   ├── intent.py        # IntentEngine
│   ├── dedup.py         # DedupEngine
│   ├── monitor.py       # ConversationMonitor
│   └── usage_tracker.py # UsageTracker
│
├── api/                 # Execution layer
│   ├── runner.py        # AgentRunner
│   ├── tools.py         # ToolDispatcher
│   ├── builtin_tools.py # Core tool implementations
│   ├── web_tools.py     # bash, file, web tools
│   ├── compaction.py    # 2-layer compaction
│   ├── rest.py          # REST API (Starlette)
│   ├── mcp.py           # MCP server
│   └── models.py        # Request/response models
│
├── handlers/            # Event handlers
│   ├── fact_extractor.py
│   ├── episode_summarizer.py
│   ├── decision_reviewer.py
│   ├── knowledge_extractor.py
│   ├── session_monitor.py
│   ├── sleep_handler.py
│   ├── subtask_worker.py
│   ├── task_scheduler.py
│   └── time_parser.py
│
├── identity/            # Agent identity
│   ├── manager.py       # IdentityManager
│   ├── protocol.py      # InitiationProtocol
│   └── tools.py         # MCP identity tools
│
└── storage/             # Persistence
    ├── database.py      # SQLAlchemy async engine
    ├── migrator.py      # Schema migrations
    └── models.py        # ORM models (22 tables)
```
