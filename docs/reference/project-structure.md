# Project Structure

Annotated map of the main modules. It is curated, not every file is listed, and the source tree is the full list. Part of the [Nous development guide](../../CLAUDE.md).

```
nous/
├── docker-compose.yml          # Nous agent + Postgres + pgvector
├── Dockerfile                  # Python container with OAT support
├── sql/
│   ├── init.sql                # Base schema (24 tables, 3 schemas)
│   ├── migrations/             # Schema migrations, numbered from 006; the highest prefix is the latest
│   └── seed.sql                # Default agent, frames, guardrails
├── nous/                       # Python package (~30,000 lines)
│   ├── config.py               # Settings via pydantic-settings
│   ├── main.py                 # Entry point, component wiring, lifecycle
│   ├── telegram_bot.py         # Telegram interface (streaming + usage)
│   ├── events.py               # Event bus (async pub/sub)
│   ├── utils.py                # Shared utilities
│   ├── loop_watchdog.py        # Event-loop stall watchdog (stack dump + exit)
│   ├── log_redaction.py        # Logging setup for both entry points: keeps the bot token and the API key out of the logs
│   ├── cancellation.py         # cancel_requested(): a task's own cancellation, as opposed to one that came out of something it awaited
│   ├── storage/                # Database layer (async SQLAlchemy)
│   │   ├── database.py         # Connection pool, session management
│   │   ├── models.py           # ORM models for all 47 tables
│   │   └── migrator.py         # Schema migration runner
│   ├── brain/                  # Decision intelligence organ
│   │   ├── brain.py            # Core: record, query, review, calibrate
│   │   ├── bridge.py           # Structure + function descriptions
│   │   ├── calibration.py      # Brier scores, confidence tracking
│   │   ├── embeddings.py       # pgvector embedding provider
│   │   ├── graph_linker.py     # Cross-type auto-linking (common-template embedding)
│   │   ├── guardrails.py       # CEL expression guardrails
│   │   ├── intentions.py       # F099: brain.intentions — spec, wake-policy defaults, lineage (a fire only under an open container), in-transaction insert, legacy close
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
│   │   ├── result_inbox.py     # F098: channel-keyed result inbox (store, subtask/DAG writers, pre_turn formatting)
│   │   ├── result_reconciler.py  # F098: repairs lost inbox writes (subtask and DAG passes); Phase C adds the memory pass
│   │   ├── result_memory.py    # F098 Phase C: a finished subtask result becomes an episode + marked chunks (log, writer, reconciler pass)
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
│   │   ├── strategy_card_distiller.py # Reasoning Maps L1: distils a strategy card from a reviewed decision (NOUS_STRATEGY_CARDS_ENABLED)
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
│   │   ├── fault_detector.py   # ProcessFaultCheck + RetrievalCanaryCheck (#653, land-dark)
│   │   ├── work_queue.py       # F064.6 WorkQueueCheck (file_jsonl adapter)
│   │   ├── finding_store.py    # F034.1 finding lifecycle persistence
│   │   ├── tuner.py            # F034.3 self-tuning pass
│   │   └── schemas.py          # Finding, CheckResult, HeartbeatResult
│   ├── identity/               # Agent identity system (F018)
│   │   ├── manager.py          # Identity section CRUD
│   │   ├── protocol.py         # Initiation protocol
│   │   └── tools.py            # Identity-related tools
│   ├── observability/          # Telemetry writers — dashboards read these tables, the agent never does
│   │   ├── context_logger.py   # F035.4 per-turn context payload log
│   │   ├── retrieval_logger.py # F091 retrieval telemetry (RETRIEVAL_PATHS lives here)
│   │   ├── retrieval_trace.py  # F091 write-only trace collector + NullTrace
│   │   ├── process_recorder.py # #653 nous_system.process_run_log writer (fail-open)
│   │   ├── drift.py            # Drift detection
│   │   └── snapshots.py        # Graph hub snapshots
│   ├── dag/                    # F038/F087 DAG orchestration
│   │   ├── orchestrator.py     # Tick loop, dispatch, reaper, completion
│   │   ├── store.py            # DAGStore (conditional status writes)
│   │   ├── delivery.py         # F087 at-least-once terminal delivery (3 legs)
│   │   ├── approval.py         # Harness Phase 3 park-and-resume nodes
│   │   ├── fix_executor.py     # LLM fix dispatch
│   │   ├── schemas.py          # DAGNodeSpec, DAG schemas
│   │   └── _workspace.py       # F064.3 workspace containment
│   ├── a2ui/                   # F092 companion surfaces
│   │   ├── service.py          # SurfaceService: authoritative state + outbox deltas
│   │   ├── transport.py        # Resumable SSE (`Last-Event-ID` wins over `?since=`)
│   │   ├── compose.py          # F092.1 micro-app compose loop + repair rounds
│   │   ├── grammar.py          # Structural/grammar validation
│   │   ├── validator.py        # Catalog schema validation (rewrites `\p{...}`)
│   │   ├── sources.py          # Dashboard data sources (F095 agent_script)
│   │   ├── actions.py          # Action gate: allowlist → nonce → rate → censor
│   │   ├── push.py             # F097 FCM leg (data-only messages)
│   │   ├── dsl.py / builders/  # Template-first surface construction
│   │   └── catalogs/           # A2UI v1.0 vendored @ pinned commit d9086fb
│   ├── security/
│   │   └── secrets.py          # scan_secrets: shared by send_email (refuse) and the F098 result memory writer (skip)
│   └── api/                    # External interfaces
│       ├── rest.py             # Starlette REST API (52 endpoints)
│       ├── mcp.py              # MCP server (nous_chat, nous_decide, etc.)
│       ├── runner.py           # Agent runner (tool loop, streaming)
│       ├── tools.py            # Tool dispatcher + registration
│       ├── retrieval_pipeline.py # F051: run_recall_pipeline (shared by recall_deep + eval)
│       ├── execution_context.py # Harness 1a: who is calling (interactive, dag_node, …)
│       ├── tool_classes.py     # Harness 2a: every tool's class, declared once
│       ├── tool_policy.py      # Harness 2a: per-context policy at the choke point
│       ├── idempotency.py      # Harness 2b: logical-send keys for external sends
│       ├── companion_assets.py # Agent-hosted asset overlay behind /dashboard/v2
│       ├── compensation.py     # Harness 2.8: snapshots of side-effecting writes so a background mutation can be reverted (NOUS_COMPENSATION_ENABLED)
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
