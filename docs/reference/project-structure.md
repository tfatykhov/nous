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
│   ├── telegram_bot.py         # Telegram interface (streaming + usage); F099 2d: owner actions (buttons, /approve, /reject, /answer, reply-to); sends the owner's secret on owner actions and chat
│   ├── ping.py                 # python -m nous.ping SESSION_ID MESSAGE: one message to /chat with the owner's secret, run in the bot's container
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
│   │   ├── commitments.py      # F111: brain.commitments (the obligation ledger) and promise_gate_log — CommitmentStore, C-0042 ids
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
│   │   ├── entity_profile.py   # F119 per-entity profiles: EntityProfileStore (one row per agent and entity key) and EntityProfileWriter (last sleep phase, verdict, dated cited lines, daily call limit)
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
│   │   ├── promise_gate.py     # F111: turn-close promise gate — PromiseDetector (stage 1), BackingResolver (artifact ids), PromiseGate
│   │   ├── finish_checks.py    # F113 fixed finish checks (pure): named_file_missing, effects_all_failed, evaluate (clean/flagged/not_checked), record_of, finish_check_counts
│   │   ├── promise_classifier.py # F111 PR B1: stage 2 — one Anthropic call per gate row's sentence, ClassifierScheduler (background, capped, drained)
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
│   │   ├── continuation_runner.py # F099 Phase 2c: ContinuationRunner (the loop, the turn, resolve_intention, the follow-up and fallbacks), build_arrival_prompt; Phase 3a: the card settle step; F114: set_goals, and the sweep keeps a goal session's root out of the slots reserved for the owner's chains
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
│   │   ├── pending_filter.py   # F116: kind, age and supersession filters of the self-initiated check's pending items (off/shadow/on each)
│   │   ├── tuner.py            # F034.3 self-tuning pass
│   │   ├── quiet_hours.py      # F099: in_quiet_hours / quiet_hours_end, shared by the heartbeat and the owner pushes
│   │   ├── commitment_watcher.py # F111 PR B1: the overdue watcher — one Telegram ping per overdue commitment, claim/finalize/release, dead-man line
│   │   ├── send_result.py      # F111 PR B1: SendResult (SENT / NOT_SENT / UNKNOWN) of a Telegram send
│   │   └── schemas.py          # Finding, CheckResult, HeartbeatResult
│   ├── identity/               # Agent identity system (F018)
│   │   ├── manager.py          # Identity section CRUD, and the default goal prompt's row (a section outside the identity's: get_goal_prompt, set_goal_prompt, clear_goal_prompt)
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
│   ├── mcp_client/             # F112 MCP client (NOUS_MCP_CLIENT_ENABLED, imported only when on): the agent uses tools of external MCP servers as mcp__<server>__<tool>
│   │   ├── config.py           # ServerConfig / ToolConfig (a row each), url_problem (https, no user, password or query), BlockedAddress, the pin's syntax, validate_headers (what a stored credential's headers may be)
│   │   ├── bridge.py           # Pure functions: prepare_tool (name, cleaned description and schema, caps), result flattening, cleaning and its wrapper line, safe_error, the credential scrub of a result (credential_needles, withhold)
│   │   ├── transport.py        # The one module opening sessions with the MCP SDK client: open_session over the guarded HTTP client (public addresses only, checked at connect; the client follows no redirect, the SDK only a same-origin one; no env proxy; response bodies capped, uncompressed; the server's credential set per request by the manager's auth), McpSession (tools/call as a plain request, no output validation; paged tools/list), is_server_error; drops its request lines from the httpx log
│   │   ├── secrets.py          # F112 PR 2c: stored credentials sealed with AES-256-GCM (SecretBox), the key read from NOUS_MCP_CLIENT_KEY_FILE (KeyFile), associated data binding a row to its agent, server, URL and kind; PR 2d: the OAuth token record (OAuthRecord)
│   │   ├── oauth.py            # F112 PR 2d: OAuth sign-in run by Nous (authorization code with PKCE S256): discover, register, exchange_code, refresh_tokens through a guarded client with no credential; OAuthFlows (sign-ins in memory, single-use state); uses the SDK's auth models and helpers only, never its auth provider; the client ID metadata document (client_metadata_url, client_metadata_document, metadata_client), used before dynamic registration at a provider that takes one
│   │   ├── registry.py         # McpRegistry: nous_system.mcp_servers and mcp_server_tools (migration 109) and the sealed mcp_server_secrets (migration 110); list, replace in one transaction, update, delete; a new URL deletes the server's credential
│   │   ├── manager.py          # McpClientManager: plan (what registers and why not, within the optional ceilings and the stored credentials), start and reload (all MCP tools registered again in sorted order, without connecting; a connection kept only while its URL and credential row are unchanged), probe (one listing through the guarded transport, transient headers only), _ServerAuth (the credential per request, its origin only, a 401 kept; PR 2d: an OAuth token refreshed before expiry and once after a 401), _refresh (the one refresh entry point: shielded task, per-server lock, UPDATE ... WHERE id), one supervisor task per connection, call timeout, no retry
│   │   └── seed.py             # `python -m nous.mcp_client.seed --name --url --allow [--background] [--write]`: lists a server's tools, prints each as the model would get it and the pin entry, stores the server and its permissions (unlisted tools set to deny)
│   ├── judge/                  # F118 outside judge (NOUS_OUTSIDE_JUDGE_MODE, off by default; outside the package only main.py, resolve_decisions' accessor, the task executor's accessor and the read route use it): a decision the agent resolved as success, partial or failure is judged later from its record, and the verdict is stored beside the agent's grade; with NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED a finished task's report too, and an hourly sweep
│   │   ├── __init__.py         # current / set_current: the running service, the only part nous/api/tools.py and nous/handlers/subtask_executor.py import
│   │   ├── slot.py             # JudgeInput, JudgeOutput, the Judge protocol (name, opaque version, grade never raises) and the provider registry
│   │   ├── instructions.py     # The built-in judge's fixed instructions, labels and output schema (version dj-2, pinned by a fingerprint); the report instructions, verdicts, labels and schema (version rj-1, pinned apart)
│   │   ├── model_judge.py      # BackgroundModelJudge: one call labelled outside_judge (no temperature, no thinking), the answer checked; BackgroundModelReportJudge: the same with the report instructions
│   │   ├── jev_judge.py        # JevJudge, the second judge (NOUS_OUTSIDE_JUDGE_SECOND=jev): one request per record to TypeSafe's System One API (a fixed URL, plain httpx, a client per request, no redirects, 10 s, the body read up to 64 KiB) with the record as the state and two questions (the verdict, and whether text in the record tries to set it: a label only); the answer checked, versioned as jq-3:<model>
│   │   ├── evidence.py         # The decision's record (its fields, its session's calls, ledger rows and spawned tasks up to the resolve), each string redacted and bounded and kept as the redaction of the whole text leaves it (or left out), whole items dropped to stay within 16,000 characters, the final text redacted again; assembled in a thread. build_report_evidence: a task finished as completed with a report, as it stood at its completion, from the start of the run that reported (its own calls, ledger rows and child tasks; the finish checks' verdict in the counts only)
│   │   ├── store.py            # nous_system.judge_verdicts (migration 121): insert (a failed verdict is replaced by the next attempt, at most three), already judged, today's calls, and the numbers of GET /dashboard/outside-judge (with two judges, how their verdicts on the same claims compare); the sweep's reads (sweep_decisions, sweep_reports, grader_of), the report numbers beside the finish checks and the owner's spot checks, and the agreement with grades from outcomes
│   │   ├── second_settings.py  # F118 PR 3 (NOUS_OUTSIDE_JUDGE_SETTINGS_ENABLED, dark; imported by main.py only with it on): the second judge's settings from the dashboard, nous_system.judge_second_settings (migration 122); resolve (a stored value over the server's, the limit never above NOUS_OUTSIDE_JUDGE_DAILY_CALLS), the key sealed with the MCP credentials' SecretBox under its own associated data, and SecondJudgeManager: each change under one lock in a task it holds (the confirmation when it starts the sending, a new key registered with the redaction first, the row committed, the new second judge started and swapped in, the old one closed before the answer), the background read again after a failed one, and the fixed-record check
│   │   └── service.py          # OutsideJudge: the queue (submit never waits or raises), one worker (started again by the next submit if it ended), the settle delay, the daily limit read from the table, close, an optional daily limit and close wait of its own (the second judge waits out a request it has sent); JudgeSet: the first judge and an optional second judge side by side, each with its own OutsideJudge (replace_second swaps the second and returns the old one, starting and closing nothing); an optional report judge (submit_report, first judge only) and, in the service with a report judge and in the second judge's (decisions only), the hourly sweep (sweep_once, at most five a pass, none past half the daily limit)
│   ├── goals/                  # F114 goals (NOUS_GOALS_ENABLED, dark; outside the package only the goal routes and main.py's goals block import it): a goal the owner approves is worked on in background sessions
│   │   ├── envelope.py         # A goal's limits: the small, medium and large presets, and custom values checked against migration 113's ranges
│   │   ├── store.py            # GoalStore: brain.goals, goal_sessions and the append-only goal_journal (migration 113); each change a targeted update guarded by the state it read, plus a journal row naming the actor; add_journal_row appends in the caller's transaction
│   │   ├── roots.py            # create_session_root: a session's first subtask and its root intention (origin goal, internal authority, so its wake policy is continue)
│   │   ├── spend.py            # session_spent: a session's spend is its root's token budget as continuation.root_limits counts it; goal_spent: a goal's total and today's (UTC, by the day each session started); sessions_started_today; jobs_launched_today: job launches of a goal's lineages from the tool-call log (None with it off), each logged command read as shell words, one launch per simple command (job_launches)
│   │   ├── quote.py            # A journal entry, plan step or file name the work chose as data: its own tag, invisible and control characters removed, every journal_entry, plan_step, file_ref and result_message delimiter escaped (fullwidth brackets too)
│   │   ├── charter.py          # build_session_task: a session's first task (the charter, then the plan and the journal quoted as data), at most 12,000 characters; render_goal_block: the same for a decision turn's prompt, with how to record progress and the evidence it can cite
│   │   ├── default_prompt.py   # The default goal prompt: DEFAULT_GOAL_PROMPT (how a goal's work runs, each sentence pinned by tests/test_goal_prompt_rules.py), effective and load (the owner's saved text when usable, else the built-in one; never raises), rungs and bodies (the texts every reader and builder tries, in order), section, and refusing_patterns and refused (which censors would refuse a text, read without side effects)
│   │   ├── driver.py           # GoalDriver (main.py builds one with the flag on and runs its loop): each tick claims the goal channels into the journal, acts on the owner's stop and cancel, closes sessions whose chain ended, ends chains with a task stuck past its limit, reads spend again, pauses goals on a stop condition (deadline, budget, stall, errors), starts the run-now requests and the goals that are due within their limits and the queue headroom; goal_roots for the continuation runner's reserved slots; tells the owner of each pause it makes (tell_owner_of_pauses); a session that claimed the goal done does not count toward a stall
│   │   ├── evidence.py         # check, citeable and files: the refs a goal session cites to close a plan step (subtask:, dag: close one; ledger: and file: under intentions/<root>/ support it), checked against its own chain as of the call
│   │   ├── tools.py            # goal_note and goal_plan: the extra tools of a goal session's decision turn, writing as the chain under the session row's then the goal row's lock; per-call and per-session limits; a done claim makes the goal wait for the owner
│   │   ├── grants.py           # F115 per-goal tools (NOUS_GOAL_TOOL_GRANTS_ENABLED, dark): decide (which turn may use a tool the owner allowed its goal: internal_only, a chain decision, task or workflow step that runs as a task, rooted at an open session of an active goal, the MCP identity unchanged), the GoalToolGrants view the runner reads (refreshed under one lock, cleared on a failed read), the first-use journal row, and the Goals screen's words (GRANT_PLACES, NEVER_SENTENCE, GRANT_FACTS)
│   │   ├── commands.py         # A goal whose tasks may run any command (NOUS_GOAL_COMMANDS_ENABLED, dark): decide_commands (which turn: the guard installed, an internal_only task or workflow step rooted at an open session of an active goal in the mode, a fresh view by its monotonic clock and by the wall clock, before the end by both clocks), the words the screen shows, the refusals and the journal rows of each change of mode
│   │   ├── activity.py         # What a goal's session is doing now (NOUS_GOAL_ACTIVITY_ENABLED, dark; GET /goals/{ref}/activity): read_activity reads the open (else newest) session, its chain's subtasks and workflows by their rows' status, the phase (phase_of, from rows only), the current item's name redacted and cut, and with the tool-call log on the chain's call counts, job launches (spend.job_launches), newest calls and Work screen session ids, in one transaction with a 5 s limit; never a call's text
│   │   ├── awaiting.py         # What a goal's sessions wait on the owner for (GET /goals awaiting_count, GET /goals/{ref} awaiting, the Goals badge of GET /dashboard/attention): the roots of its sessions with an intention awaiting_owner, open or closed, read through continuation.awaiting_ids and awaiting_items (the list the Intentions card shows); a done, failed or cancelled goal is not read
│   │   ├── pacing.py           # Goal pacing (NOUS_GOALS_PACING_ENABLED, dark): a goal that works once a day from a time of day in NOUS_GOALS_TIMEZONE; pure functions of instants (zone, day_start, slot, hold, wake_after_hold, wake_after_close, parse_at, format_at, pace_line) the driver reads at each start and close
│   │   ├── modes.py            # ModeSweeper: the ends no owner's change makes (its time passed, a finished goal, no end past its draft), each under the goal row's lock; at the driver's start, every goal to contained with the flag off, an end past the host's ceiling moved to it with the flag on
│   │   ├── chain.py            # GoalChain.turn_context (the runner's set_goal_turns: a goal root's turn gets the goal block and the goal tools) and report_channel (continuation.set_goal_report_channel: a goal root's REPORT goes to goal:<id>)
│   │   ├── owner_message.py    # tell_owner: the goal's own message to the owner (a done claim, a driver pause), keyed, in fixed sentences with the goal's quoted title
│   │   └── seed.py             # `python -m nous.goals.seed create|list|show|approve|pause|resume|run-now|stop|cancel|confirm|note|raise-budget|extend`: the owner's host command; changes refused with the flag off
│   ├── autonomy/               # F115 autonomy settings (NOUS_AUTONOMY_SETTINGS_ENABLED, NOUS_TOOL_GUARD_ENABLED, dark): the owner's limits of background work and the tools background work may use
│   │   ├── registry.py         # The eleven limits the owner may change (each a Settings field, its words, hard range and when a change applies), the cross rule, NOUS_AUTONOMY_CEILINGS
│   │   ├── store.py            # AutonomyStore: nous_system.autonomy_settings, autonomy_settings_log and tool_guard (migration 116); the host's values kept, the owner's applied in place on the shared Settings at start and on each change (written with its record in one transaction first)
│   │   ├── tool_guard.py       # ToolGuard: the process-wide snapshot of the tools switched off for background work (the owner's and NOUS_AUTONOMY_LOCKED_TOOLS); which turns it holds; read by the runner's _offered_tools and _authorize_tool_call
│   │   └── offered.py          # The kinds of background work the screen names, and why the code does not offer a tool to one (held equal to _offered_tools by a test)
│   └── api/                    # External interfaces
│       ├── rest.py             # Starlette REST API (the route list in `create_app`; see `docs/reference/rest-api.md`)
│       ├── commitment_routes.py # F111: POST /commitments/{id}/drop|done, the owner's /drop and /done
│       ├── companion_devices.py # Companion devices paired by a code: DeviceStore (nous_system.a2ui_devices, migration 115: each device's token as a SHA-256 only, revoke hooks), PairingDesk (the one open code, in memory), clean_name, and the /a2ui/devices routes (pairings, list, revoke, redeem, unpair)
│       ├── goal_routes.py # F114 PR 2: the owner's /goals routes (list, options, one goal, a new draft, one POST per owner action; F115 per-goal tools: GET, PUT and DELETE /goals/{ref}/tools; a goal that may run any command: POST /goals `commands`, POST /goals/{ref}/contained, GET /goals/commands; the default goal prompt: GET, PUT and DELETE /goals/prompt), every one behind the owner guard; a goal's actions are the store's own rules
│       ├── autonomy_routes.py # F115: the owner's /autonomy routes (the limits, the record of changes, the tools and their settings for background work), every one behind the owner guard
│       ├── judge_second_routes.py # F118 PR 3: the owner's /dashboard/outside-judge/second routes (read, change, save and remove the key, check), every one behind the owner guard, the body bounded and typed strictly; imports nothing of the judge (the manager is passed in); with NOUS_OUTSIDE_JUDGE_SETTINGS_ENABLED off the read answers {"enabled": false} and the changes 409
│       ├── mcp_client_routes.py # F112 PR 2a: the owner's /integrations/mcp routes (list, probe, add, change, refresh, remove MCP servers; PR 2c: store, replace and remove a server's key, never returned; PR 2d: OAuth start, callback page, paste fallback, sign-in state, sign-out), behind the proxy's X-Nous-Proxy-Secret; mounted only with NOUS_MCP_CLIENT_ENABLED
│       ├── intention_routes.py # F099 Phase 2d/2e: the owner-action routes (decide, answer, list, cancel), thin over ContinuationRunner; the POSTs require the owner's or the proxy's secret
│       ├── owner_guard.py # Owner routes require the owner's or the proxy's secret: the check and the route wrapper (OwnerGuard), and which proof grants which capability
│       ├── entry_gate.py # The chat routes and the inbound MCP server require the owner's or the proxy's secret (on /mcp, the owner's secret or the MCP key) under NOUS_OWNER_ENTRY_MODE (off, warn, enforce): the route and mount wrappers, the count by session class and the daily notice
│       ├── intention_dashboard.py # F099 Phase 3c: GET /dashboard/intentions queries (the tree and the metrics), read-only, Postgres-only
│       ├── autonomy_dashboard.py # Autonomy scoreboard: GET /dashboard/autonomy data (taps, approval cards, grades, task scores, inventory, warn classes), read-only
│       ├── about_dashboard.py # GET /dashboard/about data, for the About screen: the version, the optional build values (NOUS_BUILD_COMMIT, NOUS_BUILD_TIME), when the app started, Python, the database and pgvector versions, the highest migration, the agent and the configured model names; read-only, behind the owner guard
│       ├── loop_numbers_dashboard.py # GET /dashboard/loop-numbers data (NOUS_LOOP_NUMBERS_ENABLED): tokens per finished task, workflow step and goal session, and the share of finished ones no one outside the agent confirmed, for the window and the one before it; read-only, computed on read
│       ├── memory_write_health_dashboard.py # P4: GET /dashboard/memory-write-health data (rollup, rules evaluated now, acknowledgements) and the acknowledge body parser
│       ├── work_view_dashboard.py # F108 Phase 2: the Work view's and the model-call view's read-only endpoints (GET /dashboard/work/*: the sessions list, one session's trace, the patterns; GET /dashboard/model-calls: the list, one call's detail at /{call_id}, one stored text block's body at /{call_id}/blocks/{part}/{ord}): parameters and cursors, the SQL over agent_turn_log, tool_call_log, model_call_log, model_call_attempt, execution_ledger, heart.episodes and heart.subtasks (the model-call text through TextStore.describe and load_one), TOOL_LANES and WORK_KIND_GROUP
│       ├── job_fix_dashboard.py # The scoreboard's job_fixes section: every check-and-fix record, read-only, Postgres-only
│       ├── guard_dashboard.py   # The scoreboard's guards section (F107 phase 2/3): guarded schedules and heartbeat checks, guard outcomes and shadow misses in the window, read-only, Postgres-only
│       ├── mcp.py              # MCP server (nous_chat, nous_decide, etc.)
│       ├── runner.py           # Agent runner (tool loop, streaming)
│       ├── status_line.py      # The status line (NOUS_STATUS_LINE_ENABLED): the kinds of turn that get it, its text (round, tool calls, changes so far) and the helper that adds it to the outgoing request only
│       ├── session_referent.py # F110 PR 4: ChannelLastReply, the in-process store of each channel's last interactive reply that the runner hands to a new session's first turn (NOUS_SESSION_REFERENT_ENABLED)
│       ├── preamble.py         # CLAUDE_CODE_PREAMBLE: system block 0 of every Messages request (one definition)
│       ├── model_caps.py       # What a model accepts, by version: legacy Haiku, Haiku 5+ thinking disabled by default
│       ├── tools.py            # Tool dispatcher + registration; `get_procedure` renders the "Tool notes" procedure from `tool_note` and hides it while inactive or a flag is off
│       ├── retrieval_pipeline.py # F051: run_recall_pipeline (shared by recall_deep + eval)
│       ├── execution_context.py # Harness 1a: who is calling (interactive, dag_node, …)
│       ├── tool_classes.py     # Harness 2a: every tool's class, declared once
│       ├── tool_policy.py      # Harness 2a: per-context policy at the choke point; F099: the internal_only allowed set, denylist and per-call rules
│       ├── idempotency.py      # Harness 2b: logical-send keys for external sends
│       ├── companion_assets.py # Agent-hosted asset overlay behind /dashboard/v2: images and mp4/webm video
│       ├── compensation.py     # Harness 2.8: snapshots of side-effecting writes so a background mutation can be reverted (NOUS_COMPENSATION_ENABLED)
│       ├── builtin_tools.py    # bash, read_file, write_file
│       ├── db_query.py         # db_query: one read-only SELECT as the nous_reader login (scripts/ops/db_roles.sql); row, cell and output caps, ledger redaction
│       ├── job_marks.py        # The only code that touches files in a Claude Code job dir: small marks (cancelled_by, nous_root) opened without following links
│       ├── web_tools.py        # web_search, web_fetch (multi-tier routing)
│       ├── safe_fetch.py       # Fetching a model-supplied URL (web_fetch, learn_skill): public addresses only, each request pinned to the address checked, a client per request, size and time bounds; is_public_address is also the rule of the browser egress proxy and the MCP client
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
