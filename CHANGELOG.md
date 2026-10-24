# Changelog

All notable changes to Nous. Release notes for each version also live on the [GitHub releases page](https://github.com/tfatykhov/nous/releases).

# Unreleased

## ⏱️ Scheduling
- **F101: schedule concurrency and catch-up policies** (inspired by Paperclip routines). Each recurring schedule has a `concurrency_policy`: `coalesce_if_active` (the default), `skip_if_active` or `always_enqueue`. It also has a `catch_up_policy`: `skip_missed` (the default) or `enqueue_missed_with_cap`. A fire that comes due while the previous run is still active is now coalesced and recorded, where before it was deferred and fired late. The previous run can be the run's subtask or a DAG it created. Stale runs never block a schedule. `schedule_task` takes the policies and `list_tasks` shows them. `POST /schedules/{id}/run` starts a manual run, which obeys the policy. Migration 091 adds the columns and `execution_dags.schedule_id`. The new behaviour sits behind `NOUS_SCHEDULE_POLICIES_ENABLED` (default on); set it to false for the old behaviour.

# v1.1.0 — The Accountable Agent

**129 commits, 129 pull requests, 685 files changed since v1.0.0** (September 5 → October 22, 2026)

v0.1.0 was "The Thinking Agent", v0.2.0 "The Remembering Agent", and v1.0.0 the stable version, with the architecture complete and running in production. v1.1.0 makes Nous **accountable for the work it does on its own**. When it says it did something, that claim is checked against a durable record of the calls that really ran. A send reaches its recipient at most once, and a risky step can wait for the owner's answer. Its background work is scored by a separate, deterministic scorer it cannot write to. It records why it started each piece of work and follows up when the results arrive, while outward actions go to the owner as proposals. It learns lessons from its recurring jobs, and when a job keeps failing it checks the job, tries a fix and keeps the fix only if later runs fail less.

---

## ✨ Highlights

- **A harness for unattended work.** A durable execution ledger records side-effecting calls. Every tool's risk class is declared once, sends are idempotent, and claims are verified against the calls whose arguments show the effect. Plans can park on an owner approval and resume when it arrives, and an undo journal snapshots the before-state of what the agent changes.
- **Intentions and continuation (F099).** Every spawn records why it was started. When the results come back, a continuation turn follows up, and an outward action becomes a proposal the owner approves from Telegram, the companion or REST. Ships dark.
- **Scored by a separate scorer.** Each finished background subtask gets one deterministic score from a separate scoring job, built to run under its own database role so the agent can read its scores but not change them. Two things count as verification: a pull request Nous opened that the owner merged, and weekly owner spot-checks, which earn credit for agent-written checks of each kind. The dashboard's new Autonomy view shows the scoreboard.
- **Procedure learning that has to earn it.** A lesson gate makes every new procedure cite its evidence and refuses a lesson an existing procedure already teaches. Recurring background jobs become a new source of lessons, and a job that keeps failing is checked and fixed, with the fix judged on later runs.
- **A browser of its own (F100).** `browser_task` runs a separate agent loop that drives headless Chromium. The loop runs behind an egress proxy and a kernel-level network filter, and the caller gets back only a bounded summary. Ships dark.
- **Android companion (F097).** A native Android beta with server push. Companion assets now survive a rebuild.
- **Strategy cards.** Lessons distilled from resolved decisions, found by their closeness to the query and held to the same floor as procedures. Ships dark.
- **Faults surface instead of hiding.** A fault detector for faculties that fail silently, and an event-loop watchdog that dumps every stack and exits on a stall. Background loops now survive cancellations nobody sent them.

---

## 🧾 Harness: Honest Execution

### Harness Phases 0–3
- **Phase 0:** an SMTP timeout, and `send_email` classed as external
- **Phase 1a:** an `ExecutionContext` per turn; tools called outside the offered set are measured, with warn or enforce modes
- **Phase 1b:** a durable execution ledger for side-effecting tool calls (migration 074)
- **Phase 2a:** tool classes declared once (`none` < `write` < `external` < `irreversible`, plus spawns), with a per-context policy at the dispatch choke point (warn by default)
- **Phase 2b:** idempotent sends, so a logical send reaches its recipient at most once (migration 075)
- **Phase 2c:** evidence-aware claim verification: a claim counts as backed when any capable call's arguments show the effect
- **Phase 2.8:** a compensation registry that can undo a successful call, and a proceed-default for approvals (migration 080; off by default)
- **Phase 3:** park-and-resume, where approval nodes wait durably on a person's answer (migration 076; off by default)

### Ledger, Undo and Visibility
- **Harness tab** on the dashboard: approvals and the durable ledger
- **Ledger detail** *(opt-in)*: each call's redacted command and output, shown on the Ledger page (migration 086). An error row keeps a redacted 300-character excerpt.
- **Passive undo journal** (on by default): before-state snapshots of the files, schedules and checks the agent changes, with an owner-run restore
- `write_file` keeps the file's owner, bounds its path lock and snapshots only what can be reverted; `read_file` pages contiguously and holds its bounds

---

## 🔁 F099 — Intentions and Continuation

Every background spawn carries its reason and its result, and the agent can follow up on its own work under the owner's control. All of it lands dark behind `NOUS_INTENTIONS_ENABLED` and `NOUS_CONTINUATION_ENABLED`.
- **Phase 0:** carry the reason (the Plan decision on subtasks, a DAG's original request) and carry the result (callbacks and DAG check nodes get the check's findings)
- **Phase 1:** intentions recorded, one row per spawn (migration 083)
- **Phase 2:** an enforcement substrate that gives internal-only lineages a narrowed tool set. Then a continuation store (claim, gate, fenced commit, lease, TTL, repair) and a continuation runner (turn, loop, owner push, wake pass). Then proposals and owner actions: `propose_action`, deterministic approval, Telegram buttons and REST. Then the owner's cancel, with no root that can hang (migrations 084, 085).
- **Phase 3:** proposal, question and active-intention cards on the companion; an Intentions dashboard with tree and metrics (`GET /dashboard/intentions`); continuation decisions graded from lineage signals for calibration
- **Lineage work:** a lineage shell with an allowlisted environment that reads only inside its workspace, and build and test commands (git reads, pytest, ruff, make)
- **Gate hardening:** approved-action status is reported truthfully; heartbeat triage may not act outward; subtask reports and continuation notes are checked against their tool log; a masked-failure heuristic unwraps `-c`/`eval` strings
- Quiet hours do not hold an owner push while the owner is chatting

### F098 — Result Inbox
Subtask and DAG results are routed to the conversation by a stable channel through one inbox table, claimed exactly once at pre-turn, and a result-memory writer keeps what they found (migrations 081, 082). Phases A and C land dark; Phase B was superseded by F099.

---

## 📏 Scoring and the Autonomy View

- **Task scores:** one deterministic score per finished background subtask, with count-only roll-ups for DAG runs and intention chains, in a `scoring` schema of its own (migration 087). They are written only by `python -m nous.observability.task_score_job`, which the new `nous-scorer` compose service runs every 15 minutes.
- **Scorer heartbeat** (migration 088), so a scorer that stopped is visible
- **Separate database roles** for the application, the scorer and the administrator (`scripts/ops/db_roles.sql`, [database roles reference](docs/reference/database-roles.md)), plus an owner gate on the scoring code in CI
- **Scorer v2–v5:**
  - a subtask's own node check can take a "done" away;
  - a node reset archives the attempt it discards, and the scorer reads it (migration 089);
  - a pull request Nous opened and the owner merged counts as verified, while DAG check nodes no longer do;
  - weekly owner spot-checks earn credit for agent-written checks of each kind (migration 090)
- **Autonomy view** on the dashboard: the autonomy inventory and scoreboard (`GET /dashboard/autonomy`), and a **Check and fix** panel listing the jobs being checked, judged, kept or stopped
- Heartbeat triage turns are local-only and may not spawn or re-arm work

---

## 📈 Learning From Experience

### F012.1 — Evidence-Gated Procedure Learning
A lesson gate in front of every new procedure: a cue, a lesson and evidence citing the inputs it came from, with `NO_ENTRY` preferred over an invented lesson. A lesson an existing procedure already teaches is not stored again, and a cluster needs two cited sources.

### Lessons From Recurring Jobs
- **Job lessons** *(opt-in, `NOUS_JOB_LESSONS_ENABLED`)*: bad runs of a recurring background job are contrasted with its good runs, and the lesson goes through the same gate. A lesson is kept only if later runs fail less.
- **Check and fix** *(opt-in, `NOUS_JOB_FIXES_ENABLED`)*: when 3 of a recurring job's latest 5 scored runs went wrong, a check run diagnoses it and may change the job's instructions. The change is judged on the runs that follow and is kept, or restored and reported to the owner.
- Procedures fixed recently get a grace period before scoring can skip them

### Strategy Cards (Reasoning Maps, Layer 1)
Lessons distilled from resolved decisions (migration 079), found by closeness to the query, held to the procedure floor, never blocking or replacing a skill of the same name. Off by default (`NOUS_STRATEGY_CARDS_ENABLED`).

### Decisions and Calibration
- Background turns may resolve decisions, but only as noise or superseded
- One `decision_reviewed` audit row per review; an automatic review never replaces a review
- **F058:** the stale confidence calibration factor retired (0.7627 → 1.0), with calibration provenance (migration 073) and an era split in the eval

---

## 🌐 F100 — Browser Task

`browser_task` is a separate agent loop on its own pinned model that drives headless Chromium (Playwright) and returns only a bounded text summary. Network access goes through an egress proxy that checks the allowlist and every resolved address, backed by a kernel-level filter. The image now ships Playwright and Chromium, and the agent runs under an init process. Off by default (`NOUS_BROWSER_TASK_ENABLED`).

---

## 📱 Companion

### F097 — Native Android Companion
An Android beta of the companion with server push, plus a toggle for the Telegram card ping now that push works.
- Agent-hosted companion images survive a rebuild
- Buttons look and respond like buttons; findings-card buttons give visible feedback, fully triaged cards close themselves, and there is a close button
- The companion goes live, and resets its backoff, only when the stream opens

---

## 🛡️ Reliability and Operations

- **Fault detector** *(opt-in)*: flags stalled sleep phases, error streaks, collapsing change ratios and failing retrieval canaries (migration 078)
- **Event-loop watchdog** (on by default): dumps every stack and exits on a stall
- `run_python`: the deadline tracer can no longer wedge the process, and a timed-out call leaves no process of its script behind
- The DAG orchestrator tick is decoupled from the heartbeat loop and survives a failed or cancelled tick; a shell command that timed out is really stopped
- Five background loops and the four maintenance loops survive a cancellation nobody sent them
- `NOUS_DB_LOCK_TIMEOUT_SECONDS` bounds a lock wait (off by default)
- The bot token and the model API key are kept out of the logs
- Background calls cache only the prefix every call shares; structured background calls work on models that reject a forced tool choice
- **Claude Haiku 5.5** at every Haiku call site, and the Haiku defaults move to it
- **Build:** ruff and make in the image, a persistent Claude Code config directory with pre-installed plugins, pytest-asyncio for lineage test runs, and a ruleset on the default branch

---

## 🔧 Fixes

- **Subtasks and DAGs:**
  - an inline subtask is run by its caller only;
  - a node whose launch could not be recorded is launched again;
  - a subtask cancelled mid-run keeps its token usage;
  - a terminal check node leaves no state behind;
  - `dag_create` accepts fix-node DAGs and reports every violation at once;
  - a final report with string list fields or a blank confidence no longer fails;
  - subtask confidence is fail-open
- **Tools:**
  - `bash` waits 120 s by default in background turns that can afford it, and a timeout says what to retry with;
  - a refusal from `write_file` names the allowed directories;
  - Telegram notifications say when they are truncated
- **Heartbeat:**
  - known false positives close at ingest with an expiry;
  - an expired question is recorded as unanswered, not as consent;
  - drift checks are residualized against sleep prunes;
  - the low-effectiveness procedure check needs enough scored activations;
  - promise scans skip subtask results and closed episodes
- **Memory:**
  - tier-1 facts are never cluster-consolidation members;
  - a greeting followed by a request is planned as the request;
  - keyword-leg hits are counted per leg, with an any-word fallback behind a flag (off after an A/B)

---

## 📊 By the Numbers

- **Python:** 231 modules, ~134,500 lines in `nous/` (+ ~15,600 in `nous_eval/`)
- **Tests:** 545 files, ~229,000 lines, 10,578 test functions
- **Dashboard + companion:** 143 Svelte/TS files, ~26,100 lines (+ ~7,700 test lines)
- **Surface:**
  - ~116 REST routes
  - 35 agent tools (new: `browser_task`)
  - 58 tables in four schemas (`brain`, `heart`, `nous_system`, `scoring`)
  - migrations through 090
- **Since v1.0.0:** 129 commits, 129 PRs, 685 files changed (+212,431 / −6,888)

---

## Upgrade Notes

- Migrations apply automatically on startup (073 → 090).
- `docker-compose.yml` adds a `nous-scorer` service that runs the scoring job every 15 minutes. Without `NOUS_SCORER_DB_USER` / `NOUS_SCORER_DB_PASSWORD` it uses the application's login. To keep the agent from writing its own scores, run `scripts/ops/db_roles.sql` once and set the role logins; see [database roles](docs/reference/database-roles.md).
- The image now ships Playwright with headless Chromium, ruff and make, and the agent runs under an init process (`init: true`).
- Haiku call sites default to `claude-haiku-5-5`.
- **Dark by default:**
  - `NOUS_INTENTIONS_ENABLED`, `NOUS_CONTINUATION_ENABLED`
  - `NOUS_RESULT_INBOX_ENABLED`, `NOUS_RESULT_MEMORY_ENABLED`
  - `NOUS_STRATEGY_CARDS_ENABLED`
  - `NOUS_FAULT_DETECTOR_ENABLED`
  - `NOUS_DAG_APPROVAL_NODES_ENABLED`, `NOUS_COMPENSATION_ENABLED`
  - `NOUS_EXECUTION_LEDGER_DETAIL_ENABLED`
  - `NOUS_BROWSER_TASK_ENABLED`
  - `NOUS_JOB_LESSONS_ENABLED`, `NOUS_JOB_FIXES_ENABLED`
  - `NOUS_CHECK_AUDIT_ENABLED`
- **On by default:** the undo journal, the event-loop watchdog, and the tool policy in `warn` mode.
- Flag defaults and rationale now live in [docs/reference/environment-variables.md](docs/reference/environment-variables.md) (moved out of `CLAUDE.md`).
- Deploy: `docker compose build && docker compose up -d`.
- `pyproject.toml` and `nous.__version__` now read `1.1.0`.

---

# v1.0.0 — Stable Version

**704 commits, 384 pull requests, 1,234 files changed since v0.2.0** (March 19 → September 5, 2026)

v0.1.0 was "The Thinking Agent" — decisions, deliberation, cognitive framing. v0.2.0 was "The Remembering Agent" — what to remember, how to recall it, how to forget. v1.0.0 is the **stable version**: the architecture is complete and running in production. Nous runs unattended — a heartbeat that watches and acts, durable multi-step DAGs that survive restarts, background subtasks and schedules — and shows its work in a companion app whose interfaces it composes itself. Underneath, memory stopped being assumed and started being **measured**: a retrieval evaluation harness, per-retrieval telemetry that attributes every dropped candidate to the gate that dropped it, and an audit trail for every sleep cycle.

---

## ✨ Key Features

- **Persistent memory** — episodic, semantic, procedural and working memory in one Postgres + pgvector database; verbatim episode chunks next to summaries; admission control before a fact is kept (F023); supersession and principled forgetting (F027); document and multimodal ingestion — PDFs, images, text files (F024, F069).
- **Decision intelligence** — every decision recorded with confidence and reasoning, reviewed for outcome, calibrated (Brier score, temperature scaling — F058), and guarded by CEL guardrails; `resolve_decision` closes the loop from inside a conversation.
- **The cognitive loop** — frame selection, intent-driven retrieval, token-budgeted context assembly, post-turn self-monitoring, end-of-session reflection and a nightly sleep cycle that consolidates, links and prunes — with an audit trail per cycle (F035.6).
- **Hybrid retrieval** — full-text + vector search fused by Reciprocal Rank Fusion (F025), graph-augmented recall with spreading activation (F022), MMR diversity (F030), optional cross-encoder reranking (F042) and query expansion (F050), all measured on a retrieval evaluation harness (F051).
- **Knowledge graph** — facts, decisions, episodes, procedures and chunks linked across types, densified during sleep (F040), with contradictions surfaced at recall time.
- **Proactive heartbeat** — scheduled checks (health, email, Drive, prompt-defined dynamic checks), a finding lifecycle with escalation and digests, self-tuning parameters, Telegram delivery (F034).
- **Orchestration** — background subtasks, cron and one-shot schedules, and multi-node DAGs with durable at-least-once result delivery, a wall-clock reaper and token accounting (F038, F087).
- **Skills and procedures** — `learn_skill` from URLs, files or inline markdown; K-line procedure learning from repeated successful decisions (F012); full skill bodies delivered at recall (F079, F081).
- **Execution integrity** — an execution ledger, action gating, claim verification (F026) and censor middleware that can run read-only checks before an action proceeds (F031, F078).
- **Companion app** — the agent renders structured, interactive UI instead of prose: approval gates, findings cards, and self-composed micro-apps with charts, metric cards, scorecards and tables that refresh live (F092–F096).
- **Interfaces** — REST API (~100 routes), MCP server, and a streaming Telegram bot with attachments and file delivery; multi-tier web search (Tavily → Exa → Brave, F033).
- **Observability** — a Svelte 5 dashboard: memory graph, decisions and calibration, retrieval telemetry with drop attribution (F091), consolidation audit, prompt-cache efficiency, execution ledger, heartbeat, DAGs, identity and user-profile editing.
- **Operations** — one `docker compose up`; 72 migrations applied automatically on startup; every behavior-changing capability behind a documented flag, shipped land-dark and flipped deliberately; prompt-cache optimization at ~88% hit rate in production (F036); 6,344 tests.

---

## 🖥️ Companion App & Micro-Apps

### F092 — A2UI Companion (Phases 0–4)
The agent renders structured, interactive UI instead of prose. A2UI v1.0 vendored; surfaces persisted with an outbox delta log and pushed over resumable SSE; a Svelte 5 renderer at `/companion`; every user action passes allowlist → nonce → CSRF → rate limit → censor gate → durable audit.
- **Phase 2:** `callAgentFunction` RPC, decision sweep, memory graph and DAG monitor surfaces
- **Phase 3 (F092.1):** ephemeral micro-apps — `compose_surface` builds a read-only, navigable app for any intent, with refine options and live refresh
- **Phase 4:** PWA install, multi-app switcher, per-app deep links, close-all
- **F092.2:** agent-bound actions — a tap becomes a background agent turn that recomposes the app in place (land-dark)
- **F095:** agent-authored data sources — the agent writes the script that produces an app's data, so any domain is live with no new code or env var (land-dark)
- **Activity indicator (#637):** live header stamp with elapsed time, progress rail, pressed control, section dimming; completion keyed to server-issued outbox revisions, never timestamps

### F093 + F094 — Design System & Visualization Vocabulary
Five closed themes, `Repeat` templates, section layouts, and hand-rolled SVG `Sparkline` / `LineChart` / `BarChart` with a `to_series` generalizer, gap-preserving downsampling and normative zero-basing for bars.

### F096 — Report Vocabulary
`MetricCard`, `ScoreCard`, `DeltaList`, `DataTable`, `ChipRow`; section captions, card grids, header notes, sparkline trendlines and focus windows — a "how are things going" report now renders as a live app instead of emailed HTML.

---

## 🫀 Proactive Autonomy

### F034 — Heartbeat (Phases 1–6)
A tick loop that runs checks (health, email, Google Drive, self-initiated), triages findings through a cognitive session, and pushes to Telegram.
- **F034.1** finding lifecycle — fingerprint dedup, state machine, escalation, daily digest, outcome signals
- **F034.2** intelligent checks — embedding search, LLM email classification, drive significance
- **F034.3** self-tuning — outcome-driven parameter adjustment with cross-cycle rollback and pinned params
- **F034.5** dynamic checks — prompt-driven checks created and managed in conversation
- **F034.6** `on_complete` callbacks with three-layer failure handling

### F038 — Unified DAG Orchestration
`DAGStore` + `DAGOrchestrator` + DAG tools + dashboard tab; node completion checks (F038.1); configurable node timeouts (F046); stall detection, per-frame concurrency caps, workspace safety, schedule continuation and a work-queue adapter (F064.x).

### F087 — DAG Durability Spine
Reaching terminal and being delivered are separate transitions: at-least-once result delivery across restarts (bus, agent-authored summary, Telegram), a wall-clock reaper for orphaned nodes, live token accounting, fail-loud wiring when the clock is not running.

### F090 — Callback Execution & Finished-DAG Visibility
Callback nodes execute as real subtasks (flag-gated); `dag_manage action=recent`; Phase-2 gate signals on the dashboard.

### Subtasks, Schedules & Sessions
Subtask hardening — schema, contract, dashboard (#421); **F049** session and memory lifecycle hygiene; **F048** background streaming with TCP keep-alive so long generations no longer drop.

---

## 🧠 Memory — Write Path

### F023 — Memory Admission Control
Five-dimension scoring gate (utility, confidence, novelty, recency, type prior) before a fact enters long-term memory, with shadow mode, persisted per-dimension scores and an admission dashboard.

### F027 — Supersession Detection & Principled Forgetting
Contradiction classification at learn time, supersession lineage, and a sleep-cycle contradiction-resolution phase using structured outputs.

### F084 / F085 / F086 — Write-Path Adjudication, Keyed Facts, Exemplars
- **F084:** dense documents route through enumerative extraction into atomic keyed facts; store-time key-conflict supersession
- **F085:** bidirectional entity indexing + canonical keys, a keyed retrieval leg, bounded iterative composition (R3v2)
- **F086:** parse-only exemplar extraction and a kNN exemplar leg for classification-shaped queries

All three land dark; acceptance is measured in an external harness before any flag flips.

### More Capture, Less Loss
- **F067** episode chunks — verbatim transcript chunks stored alongside lossy summaries, with parent-episode recall
- **F069** `ingest_document` — chunk and persist whole documents
- **F024** inbound multimodal attachments (Telegram + REST): images, PDFs (pypdf with a Claude transcription fallback), text files
- **F075** temporal extraction — date-anchored facts and an event-date recency resolver
- **F039** correction learning (MemAlign-inspired) — user corrections become stored principles
- **F047** actionability classification at learn time
- **S2** extraction input hardening — delimiters, a DATA/INSTRUCTION boundary, prompt-echo filter
- Memory-fidelity constants: transcript, lessons and summary bounds moved from hardcoded truncations to configurable, lossless capture

---

## 🔍 Memory — Retrieval

- **F025** Reciprocal Rank Fusion for hybrid search · **F030** MMR diversity re-ranking · **F042 / F043** cross-encoder reranking in recall and in sleep-cycle graph backfill · **F045** CE-aware cosine thresholds
- **F050** multi-query expansion via Haiku (dark-launch) · **F080** coherent ranking over a knowledge-only recall pool + §14 graph-primary procedure selection · **F079 / F081** procedure delivery with full skill bodies
- **F083** follow-up association — cross-session deictic references ("did that fix work?") resolved from memory
- **F070 / F070.1** chunk graph consolidation · **F071** cross-context dedup · R2 hybrid chunk search over `search_tsv` (land-dark)
- Spreading activation seeded by heart facts (fires on decision-less corpora), bounded activation scores, depth-1 parity option (C-S), Path-A cross-type expansion
- **F044** tinyHippo-Lite — synaptic tagging & capture over graph edges (telemetry-only)
- Pre-turn context: reliable fact injection (render depth, fact pin, supersession lineage, recall backstop), user-profile core/intent split, per-line identity dedup, demotion of superseded and noise decisions
- `recall_deep()` inside `run_python` now runs the real retrieval pipeline (it was a facts-only search)

---

## 🕸️ Graph

- **F040** graph densification — orphan backfill, reverse linking, per-relation thresholds, cluster discovery, density dashboard
- **F053** episode-graph erasure fixed (7,739 edges restored); co-mention / shared-entity associative linking; experiential co-occurrence edges; decision autolink backfill; orphan-edge sleep cleanup
- Edge-precision and orphan audits: the graph is measured rather than assumed — retrieval depends on edge existence, not edge weight

---

## 📏 Measurement & Evaluation

### F051 — Retrieval Evaluation Harness
Local-first evaluation on a separate Postgres image, per-source qrels, paired A/B with an F050 gate, persisted run history and a regression CLI; LongMemEval, LoCoMo and BEAM adapters; multi-turn replay; handler evals for summaries, dedup, admission and graph backfill.

### F091 — Retrieval Telemetry
Every candidate on all three retrieval paths (`recall_deep`, the per-turn context build, `run_python`) ends with a terminal disposition and the gate that assigned it; graph expansion is captured as seed → edge → neighbor; a retrieval dashboard shows drop attribution per retrieval.

### Also
- **F035.6** consolidation audit — a reviewable changelog of every sleep cycle
- **F058** confidence calibration (temperature scaling on decision write) and a `resolve_decision` tool that closes the calibration loop
- Cognitive-layer eval suite; a sleep-cycle health monitor that found three silently broken phases

---

## 🧭 Cognition & Context

- **F026** Execution Integrity — execution ledger, action gating, claim verification, change-aware duplicate detection
- **F024** Critic Agent Phase 0 (smart frame selector) · **F024-3b** self-modifying rubrics
- **F031** censor middleware with action payloads · **F078 / F078.1** censor system and a guarded `send_email` with a hot-reloadable allowlist
- **F036** prompt cache optimization — 3-tier system prompt, single breakpoint, stable tool set (prod cache hit rate ~88%)
- **F038** memory quality & context loading fixes; epistemic gate and recency resolver (flags OFF)
- Tool-input validation and salvage; every tool error path now flags `is_error`

---

## 🛠️ Runtime & Integrations

- **F033** multi-tier search routing (Tavily → Exa → Brave)
- Google Drive via service account, OAuth2 with auto-refresh, `send_file` to Telegram
- Structured-output compaction (fact ledger as a tool array), a compaction hallucination guard, restart-resilient active episodes
- Docker: non-root runner, GitHub CLI in the image; OS and Python packages exposed to the agent's environment section

---

## 📊 Dashboard

Dashboard v1 (vanilla JS, six views) shipped in March and was retired in June for **Dashboard v2** — a Svelte 5 SPA: graph (Cytoscape, centrality, cluster colouring, node detail), decisions, memory browser, calibration, activity, health, observability (event bus, causal traces, drift, context viewer), prompt cache, admission, execution ledger, heartbeat, DAG, density, retrieval, consolidation, and identity / user-profile editing. Mobile layout overhauled.

---

## 📊 By the Numbers

- **Python:** 171 modules, ~90,700 lines in `nous/` (+ ~15,000 in `nous_eval/`)
- **Tests:** 325 files, ~138,000 lines, 6,344 test functions
- **Dashboard + companion:** 135 Svelte/TS files, ~23,600 lines (+ ~6,300 test lines)
- **Surface:** ~100 REST routes, 34 agent tools, 37 ORM tables, migrations through 072
- **Since v0.2.0:** 704 commits, 384 PRs, 1,234 files changed (+423,892 / −4,143)

---

## Upgrade Notes

- Migrations apply automatically on startup (022 → 072).
- Every behavior-changing capability ships behind a flag; defaults are documented in `CLAUDE.md`. Land-dark features (F050, F084–F086, F092.2, F095, …) stay off until flipped deliberately.
- Deploy: `docker compose build && docker compose up -d` — rebuilds the dashboard bundle and restarts the agent.
- `pyproject.toml` and `nous.__version__` now read `1.0.0`.

---

## v0.2.0 — The Remembering Agent (2026-03-17)

Memory quality: graph-augmented recall (F022), K-Line learning (F012), skill discovery v2 (F011), context pruning and quality gates (F016/F017), tool output intelligence (F020), programmatic tool calling. Full notes: https://github.com/tfatykhov/nous/releases/tag/v0.2.0

## v0.1.0 — The Thinking Agent (2026-03-02)

Core architecture: Brain, Heart, Cognitive Layer, runtime (REST + MCP + Telegram), context engine, event bus. Full notes: https://github.com/tfatykhov/nous/releases/tag/v0.1.0
