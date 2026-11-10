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
│   ├── undo_journal.py         # Passive before-state snapshots (files, schedules, heartbeat checks, heartbeat config) + owner-run restore (NOUS_UNDO_JOURNAL_ENABLED)
│   ├── telegram_bot.py         # Telegram interface (streaming + usage); F099 2d: owner actions (buttons, /approve, /reject, /answer, reply-to)
│   ├── owner_actions.py        # F099 Phase 2d: callback-data codec and refusal vocabulary shared by the publisher, the REST routes and the Telegram bot (stdlib only)
│   ├── events.py               # Event bus (async pub/sub)
│   ├── utils.py                # Shared utilities
│   ├── tool_markup.py          # Strips tool-call markup a model leaked into a stored value (</content><parameter ...>), used by the memory write paths and scripts/ops/clean_tool_markup.py
│   ├── loop_watchdog.py        # Event-loop stall watchdog (stack dump + exit)
│   ├── log_redaction.py        # Logging setup for both entry points: keeps the bot token and the API key out of the logs
│   ├── cancellation.py         # cancel_requested(): a task's own cancellation, as opposed to one that came out of something it awaited
│   ├── cancel_provenance.py    # Who cancelled a DAG or a Claude Code job: the record (owner, agent or harness), its renderers, the lone runner.sh call parser, the (by, via) pairs a job's cancelled_by may hold
│   ├── storage/                # Database layer (async SQLAlchemy)
│   │   ├── database.py         # Connection pool, session management
│   │   ├── models.py           # ORM models
│   │   └── migrator.py         # Schema migration runner
│   ├── brain/                  # Decision intelligence organ
│   │   ├── brain.py            # Core: record, query, review, calibrate
│   │   ├── bridge.py           # Structure + function descriptions
│   │   ├── calibration.py      # Brier scores, confidence tracking
│   │   ├── embeddings.py       # pgvector embedding provider (F108: set_recorder counts each embeddings request in a minute bucket)
│   │   ├── graph_linker.py     # Cross-type auto-linking (common-template embedding)
│   │   ├── guardrails.py       # CEL expression guardrails
│   │   ├── intentions.py       # F099: brain.intentions — spec, wake-policy defaults, lineage (a fire only under an open container), in-transaction insert, legacy close
│   │   ├── continuation.py     # F099 Phase 2: the continuation store: the inbox primitives, the same-transaction move of a continue result, owner-facing rows, the startup rollback; Phase 2c: the claim, the gate and the budgets (derived from rows), the fenced commit, failure and lease, the TTL sweep, the ask wake rule (the runner that calls it: `handlers/continuation_runner.py`); Phase 2d: staging, publish and expiry of proposals, the owner's decisions and answers
│   │   ├── intention_calibration.py # F099 Phase 3d: grades each arrival's Brain decision from lineage rows (owner approve/reject, cancel, how the chain ended), reviewer `f099`; flag NOUS_INTENTION_CALIBRATION_ENABLED
│   │   ├── quality.py          # Decision quality scoring
│   │   ├── schemas.py          # Pydantic models
│   │   └── spreading_activation.py  # Density-gated multi-hop graph traversal
│   ├── heart/                  # Memory system organ
│   │   ├── heart.py            # Core: learn, recall, episode lifecycle
│   │   ├── episodes.py         # Episodic memory
│   │   ├── facts.py            # Semantic memory
│   │   ├── procedures.py       # Procedural memory
│   │   ├── job_runs.py         # Job lessons: a recurring job's runs from heart.subtasks and the execution ledger (own record only)
│   │   ├── profile_card.py     # Bounded profile card: ProfileCardStore (one row per agent) and ProfileCardWriter (sleep phase, cited lines, fails closed)
│   │   ├── censors.py          # Guardrail censors
│   │   ├── censor_actions.py   # F031: Censor action executor (read-only tools)
│   │   ├── working_memory.py   # Short-term scratch space
│   │   ├── search.py           # Full-text + vector search
│   │   ├── subtasks.py         # Subtask CRUD operations
│   │   ├── result_inbox.py     # F098: channel-keyed result inbox (store, subtask/DAG writers, pre_turn formatting)
│   │   ├── result_reconciler.py  # F098: repairs lost inbox writes (the inbox passes: subtask and DAG); Phase C adds the memory pass; F099 adds the intentions pass (closes finished work and stopped schedules); F099 Phase 2c adds `repair_missing_results` and ContinuationWakePass
│   │   ├── result_memory.py    # F098 Phase C: a finished subtask result becomes an episode + marked chunks (log, writer, reconciler pass)
│   │   ├── schedules.py        # Schedule CRUD operations
│   │   ├── temporal.py         # Bi-temporal helpers (pure): interval_relation and close_before (supersession), format_span and sort_dated (reading)
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
│   │   └── schemas.py          # TurnContext, TurnResult, CarriedFrame (F110 carried frame), etc.
│   ├── handlers/               # Event bus handlers
│   │   ├── episode_summarizer.py  # Episode summary generation
│   │   ├── job_lessons.py         # JobLessonLearner: lessons from bad runs of recurring jobs
│   │   ├── job_fixes.py           # JobFixer: check-and-fix for a recurring job that keeps failing
│   │   ├── tool_notes.py          # F108 Phase 3: ToolNoteStore (the nous_system.tool_note table, migration 103), ToolNotesReview (the sleep step: the keep-or-retire judge, one model call, the strict answer check, the owner card for guard patterns) and the review prompt, and the "Tool notes" procedure (`sync_procedure`, `render_notes`, rendered by `get_procedure`, never stored): capped notes about repeated tool mistakes (3 per tool, 40 in all, 300 characters) and the declined and guard rows that keep a pattern from being re-reviewed for 14 days; the app is its only reader and writer
│   │   ├── fact_extractor.py      # Fact extraction from conversations
│   │   ├── knowledge_extractor.py # Pre-prune fact extraction
│   │   ├── decision_reviewer.py   # Automated decision review
│   │   ├── strategy_card_distiller.py # Reasoning Maps L1: distils a strategy card from a reviewed decision (NOUS_STRATEGY_CARDS_ENABLED)
│   │   ├── session_monitor.py     # Session timeout monitoring
│   │   ├── sleep_handler.py       # Sleep/reflection handler (its `tool_notes` phase runs `ToolNotesReview`; counts under `sleep_stats["tool_notes"]`)
│   │   ├── subtask_worker.py      # Async subtask execution
│   │   ├── schedule_guard.py      # F107: run-level guard for scheduled script jobs (a schedule's `metadata.guard` names an entry of the `NOUS_SCHEDULE_GUARD_COMMANDS` allowlist, which runs before the model turn; `exit0_quiet`: exit 0 + empty stdout + empty stderr completes the run without one in `mode: skip`; the default `shadow` mode only records it, and `acted_signals` defines whether the model acted)
│   │   ├── continuation_runner.py # F099 Phase 2c: ContinuationRunner (the loop, the turn, resolve_intention, the follow-up and fallbacks), build_arrival_prompt; Phase 3a: the card settle step
│   │   ├── continuation_publisher.py # F099 Phase 2c: OwnerPublisher, the owner push to Telegram (quiet-hours aware, once per row); Phase 2d: PROPOSAL rows with buttons, QUESTION rows with force_reply, escaped HTML; Phase 3a: the companion card leg
│   │   ├── task_scheduler.py      # Cron/one-shot scheduling
│   │   └── time_parser.py         # Natural language time parsing
│   ├── skills/                 # Skill discovery system (F011)
│   │   ├── parser.py           # SkillParser + SkillManifest
│   │   ├── builtin/            # F110: skills that ship with the code (spec-writing/SKILL.md, the spec-writing-verify skill), package data
│   │   └── bootstrap.py        # One-time local skill registration; F110 sync_builtin_skills (builtin/ follows NOUS_SPEC_SKILL_ENABLED)
│   ├── heartbeat/              # Proactive monitoring (F034)
│   │   ├── runner.py           # HeartbeatRunner tick loop + triage
│   │   ├── registry.py         # CheckRegistry + BaseCheck ABC
│   │   ├── checks.py           # HealthCheck, SelfInitiatedCheck, EmailCheck
│   │   ├── dynamic.py          # DynamicCheck + DynamicCheckLoader (F034.5)
│   │   ├── check_guard.py      # F107 phase 3: a dynamic check's run guard (metadata.guard, the schedule guard's allowlist and rules; timeout fit, is_action receipt, check_guard_* events)
│   │   ├── fault_detector.py   # ProcessFaultCheck + RetrievalCanaryCheck (#653, land-dark)
│   │   ├── memory_write_health_check.py # P4 MemoryWriteHealthCheck: hourly, quiet hours too, findings off chat (chat_delivery False) and never actionable
│   │   ├── work_queue.py       # F064.6 WorkQueueCheck (file_jsonl adapter)
│   │   ├── finding_store.py    # F034.1 finding lifecycle persistence
│   │   ├── tuner.py            # F034.3 self-tuning pass
│   │   ├── quiet_hours.py      # F099: in_quiet_hours / quiet_hours_end, shared by the heartbeat and the owner pushes
│   │   └── schemas.py          # Finding, CheckResult, HeartbeatResult
│   ├── identity/               # Agent identity system (F018)
│   │   ├── manager.py          # Identity section CRUD
│   │   ├── protocol.py         # Initiation protocol
│   │   └── tools.py            # Identity-related tools
│   ├── observability/          # Telemetry writers. Dashboards read these tables in full; the agent (`db_query`) reads only what `scripts/ops/db_roles.sql` section 5d lists: `tool_call_log` (redacted, capped previews and reasons, never the full result), `model_call_log` and `model_call_attempt` (never a prompt or response), and the numbers, ids and labels of `agent_turn_log` (never a message). The tool-notes review (`nous/handlers/tool_notes.py`) also reads `tool_call_log`, and `tool_note` (migration 103) is read and written by the app only
│   │   ├── context_logger.py   # F035.4 per-turn context payload log (its context estimate lives in model_calls.py); with NOUS_MODEL_CALL_LOG_ENABLED on it keeps its in-memory ring but writes no context_log row
│   │   ├── context_view.py     # F108: what the context endpoints read. RingContextView (the in-memory ring, as before) while NOUS_MODEL_CALL_LOG_ENABLED is off; TableContextView (model_call_log rows of the runner's own calls, as ContextLogEntry objects with a few added keys; payload and section text from the text store, else the ring) while it is on. main.py builds one and hands it to create_app
│   │   ├── model_call_text.py  # F108: the model-call text store. split_payload (a call's system blocks, messages, tool list and response as blocks), prepare_block (base64 replaced, redacted, cut, sha256 of the body), response_from_events (a stream's response rebuilt from its events), and TextStore (store: one transaction of hash-ordered block upserts that touch, then the call's refs; load; describe: a call's blocks without their bodies, with how many calls reuse each, for the dashboard; load_one: one block's body by its ref's primary key; an LRU of prepared blocks). main.py builds it when NOUS_MODEL_CALL_LOG_ENABLED and NOUS_MODEL_CALL_TEXT_ENABLED are both on and hands it to the ModelCallRecorder, which submits each call's text after its row commits
│   │   ├── model_calls.py      # F108: ModelCallRecorder (one model_call_log row per model call with its model_call_attempt rows, embedding and rerank calls counted per minute), CallContext and the purpose vocabulary, model_call_context (the fallback label), and the context estimate (analyze_context, analyze_sections, ContextStats, parse_system_sections, payload_preamble, system_sections). main.py builds the recorder when NOUS_MODEL_CALL_LOG_ENABLED is on and hands it to both Anthropic clients, the embedder, the browser task loop and the reranker
│   │   ├── recording.py        # F108: BoundedWriter, the one bounded fire-and-forget writer every work-view recorder shares (built in main.py when either work-view log is on), and RecorderStats, its counters
│   │   ├── tool_calls.py       # F108: ToolCallRecorder (one tool_call_log row per tool call, one agent_turn_log row per turn, text redacted and capped, with F110's frame_reason, critic_frame and had_error), the reason field (add_reason_field, split_reason) and work kinds
│   │   ├── tool_patterns.py    # F108 Phase 3: the one definition of a repeated or failed-again tool call (shared with the Patterns endpoint), error-text normalisation, guard classification and the pattern detection over tool_call_log
│   │   ├── work_view_retention.py # F108: the daily agent-scoped deletes main.py's retention loop runs: sweep_tool_calls (tool_call_log and agent_turn_log rows past NOUS_TOOL_CALL_RETENTION_DAYS), sweep_model_calls (model_call_log rows past NOUS_MODEL_CALL_RETENTION_DAYS, their attempts by cascade) and sweep_model_call_text (model_call_block_ref rows past NOUS_MODEL_CALL_TEXT_RETENTION_DAYS, then the model_text_block rows no call references and no store used for an hour; whatever the text flag says, and has_model_call_text tells main.py whether to start the loop with every log off)
│   │   ├── retrieval_logger.py # F091 retrieval telemetry (RETRIEVAL_PATHS lives here)
│   │   ├── retrieval_trace.py  # F091 write-only trace collector + NullTrace
│   │   ├── process_recorder.py # #653 nous_system.process_run_log writer (fail-open)
│   │   ├── task_scoring.py     # Task scores: the pure task scorer; holds every constant and SCORER_VERSION
│   │   ├── task_score_gatherer.py # Task scores: reads a scoring pass's signals in a bounded number of queries
│   │   ├── github_verify.py    # Task scores v4: reads a node's PR check from GitHub (author is the agent, opened after the DAG, merged by another login); one PR credits one leaf
│   │   ├── task_score_job.py   # Task scores: the one-shot scoring pass (`python -m nous.observability.task_score_job`), the only writer of `scoring.task_scores` and `scoring.scorer_heartbeat` (migration 088, its one-row-per-agent liveness record)
│   │   ├── autonomy_inventory.py # Autonomy inventory: per tool, successes by who was in the loop (owner present, approved, autonomous), and censor activity
│   │   ├── drift.py            # Drift detection
│   │   ├── memory_write_health.py # P4: the daily write-health rollup (migration 099), its six rules, the latched hard-delete detector and its acknowledgements (events of type memory_write_health_ack); one of the tables here the db_query reader may read (owner's call; the others are `tool_call_log`, `model_call_log` and `model_call_attempt`)
│   │   └── snapshots.py        # Graph hub snapshots
│   ├── audits/                 # Scorer v5: the owner's sampled audits of completion checks
│   │   └── check_audit.py      # CheckAuditSampler (weekly loop started from main.py, off unless NOUS_CHECK_AUDIT_ENABLED); `__init__.py` holds the shared `AUDIT_VERDICT_ACTION`
│   ├── dag/                    # F038/F087 DAG orchestration
│   │   ├── orchestrator.py     # Tick loop, dispatch, reaper, completion
│   │   ├── store.py            # DAGStore (conditional status writes)
│   │   ├── delivery.py         # F087 at-least-once terminal delivery (3 legs)
│   │   ├── approval.py         # Harness Phase 3 park-and-resume nodes
│   │   ├── attempts.py         # The attempt a node reset discards, archived in `dag_nodes.attempt_history` (migration 089) for the task scorer
│   │   ├── fix_executor.py     # LLM fix dispatch
│   │   ├── typed_checks.py     # Built-in completion checks (`python -m nous.dag.typed_checks pr <repo> --head <branch> [--job-file <path>]`): passes once the PR exists; with a launched job's id file it waits for the job and fails once the job ended without the PR
│   │   ├── schemas.py          # DAGNodeSpec, DAG schemas
│   │   └── _workspace.py       # F064.3 workspace containment
│   ├── browser/                # F100 browser_task: isolated browser-use loop
│   │   ├── session.py          # Playwright Chromium, tabs, allowlist validation and routing, teardown
│   │   ├── egress.py           # Egress proxy: allowlist + resolved-address check on every connection
│   │   ├── chromium-as-user.sh # Runs Chromium as the unprivileged nous-browser user (setpriv)
│   │   ├── executor.py         # browser_toolset member calls -> Playwright; confirmation guard
│   │   ├── loop.py             # Inner agent loop, pinned model, own /v1/messages caller (F108: module-level set_recorder records each step as a browser_step call)
│   │   └── tools.py            # browser_task registration
│   ├── a2ui/                   # F092 companion surfaces
│   │   ├── service.py          # SurfaceService: authoritative state + outbox deltas
│   │   ├── transport.py        # Resumable SSE (`Last-Event-ID` wins over `?since=`)
│   │   ├── compose.py          # F092.1 micro-app compose loop + repair rounds
│   │   ├── grammar.py          # Structural/grammar validation
│   │   ├── validator.py        # Catalog schema validation (rewrites `\p{...}`)
│   │   ├── sources.py          # Dashboard data sources (F095 agent_script)
│   │   ├── actions.py          # Action gate: allowlist → nonce → rate → censor
│   │   ├── push.py             # F097 FCM leg (data-only messages)
│   │   ├── intention_keys.py   # F099 Phase 3a: the reserved intention: dedup keys (leaf)
│   │   ├── dsl.py / builders/  # Template-first surface construction (`builders/check_audit.py`: the owner's weekly spot-check card, built by the server only and not in `TEMPLATES`; `builders/memory_write_health.py`: P4's card, an Acknowledge button per tripped rule, server-built, priority 0, never a ping; `builders/tool_notes_guard.py`: F108's quiet card of the window's guard-caused tool patterns, server-built, no button, never a ping)
│   │   └── catalogs/           # A2UI v1.0 vendored @ pinned commit d9086fb
│   ├── security/
│   │   └── secrets.py          # scan_secrets: shared by send_email (refuse) and the F098 result memory writer (skip)
│   └── api/                    # External interfaces
│       ├── rest.py             # Starlette REST API (the route list in `create_app`; see `docs/reference/rest-api.md`)
│       ├── intention_routes.py # F099 Phase 2d: the four owner-action routes (decide, answer, list), thin over ContinuationRunner
│       ├── intention_dashboard.py # F099 Phase 3c: GET /dashboard/intentions queries (the tree and the metrics), read-only, Postgres-only
│       ├── autonomy_dashboard.py # Autonomy scoreboard: GET /dashboard/autonomy data (taps, approval cards, grades, task scores, inventory, warn classes), read-only
│       ├── memory_write_health_dashboard.py # P4: GET /dashboard/memory-write-health data (rollup, rules evaluated now, acknowledgements) and the acknowledge body parser
│       ├── work_view_dashboard.py # F108 Phase 2: the Work view's and the model-call view's read-only endpoints (GET /dashboard/work/*: the sessions list, one session's trace, the patterns; GET /dashboard/model-calls: the list, one call's detail at /{call_id}, one stored text block's body at /{call_id}/blocks/{part}/{ord}): parameters and cursors, the SQL over agent_turn_log, tool_call_log, model_call_log, model_call_attempt, execution_ledger, heart.episodes and heart.subtasks (the model-call text through TextStore.describe and load_one), TOOL_LANES and WORK_KIND_GROUP
│       ├── job_fix_dashboard.py # The scoreboard's job_fixes section: every check-and-fix record, read-only, Postgres-only
│       ├── guard_dashboard.py   # The scoreboard's guards section (F107 phase 2/3): guarded schedules and heartbeat checks, guard outcomes and shadow misses in the window, read-only, Postgres-only
│       ├── mcp.py              # MCP server (nous_chat, nous_decide, etc.)
│       ├── runner.py           # Agent runner (tool loop, streaming)
│       ├── session_referent.py # F110 PR 4: ChannelLastReply, the in-process store of each channel's last interactive reply that the runner hands to a new session's first turn (NOUS_SESSION_REFERENT_ENABLED)
│       ├── preamble.py         # CLAUDE_CODE_PREAMBLE: system block 0 of every Messages request (one definition)
│       ├── model_caps.py       # What a model accepts, by version: legacy Haiku, Haiku 5+ thinking disabled by default
│       ├── tools.py            # Tool dispatcher + registration; `get_procedure` renders the "Tool notes" procedure from `tool_note` and hides it while inactive or a flag is off
│       ├── retrieval_pipeline.py # F051: run_recall_pipeline (shared by recall_deep + eval)
│       ├── execution_context.py # Harness 1a: who is calling (interactive, dag_node, …)
│       ├── tool_classes.py     # Harness 2a: every tool's class, declared once
│       ├── tool_policy.py      # Harness 2a: per-context policy at the choke point; F099: the internal_only allowed set, denylist and per-call rules
│       ├── idempotency.py      # Harness 2b: logical-send keys for external sends
│       ├── companion_assets.py # Agent-hosted asset overlay behind /dashboard/v2
│       ├── compensation.py     # Harness 2.8: snapshots of side-effecting writes so a background mutation can be reverted (NOUS_COMPENSATION_ENABLED)
│       ├── builtin_tools.py    # bash, read_file, write_file
│       ├── db_query.py         # db_query: one read-only SELECT as the nous_reader login (scripts/ops/db_roles.sql); row, cell and output caps, ledger redaction
│       ├── job_marks.py        # The only code that touches files in a Claude Code job dir: small marks (cancelled_by, nous_root) opened without following links
│       ├── web_tools.py        # web_search, web_fetch (multi-tier routing)
│       ├── injection_screen.py # F103: flag-only prompt-injection + secret screen over web_fetch / browser_task results
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
│   ├── beam/                   # PR-M: `python -m nous_eval.beam {ingest,answer,score}` — BEAM via the prod write path, pre-turn or recall_deep answers, BEAM's judge
│   ├── halumem/                # `python -m nous_eval.halumem {run,score}` — HaluMem dialogues via the prod write path (beam harness), per-session stored facts / update probes / answers in HaluMem's adapter format, scored by HaluMem's own evaluation.py
│   ├── served/                 # `python -m nous_eval.served {recall,preturn,compare}` — judged precision of what recall_deep and the pre-turn build serve, one arm per run on a clone, paired bootstrap
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
