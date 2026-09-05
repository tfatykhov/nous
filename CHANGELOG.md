# Changelog

All notable changes to Nous. Release notes for each version also live on the [GitHub releases page](https://github.com/tfatykhov/nous/releases).

# v1.0.0 — The Working Agent

**704 commits, 384 pull requests, 1,234 files changed since v0.2.0** (March 19 → September 5, 2026)

v0.1.0 was "The Thinking Agent" — decisions, deliberation, cognitive framing. v0.2.0 was "The Remembering Agent" — what to remember, how to recall it, how to forget. v1.0.0 is **The Working Agent**: Nous now runs unattended — a heartbeat that watches and acts, durable multi-step DAGs that survive restarts, background subtasks and schedules — and shows its work in a companion app whose interfaces it composes itself. Underneath, memory stopped being assumed and started being **measured**: a retrieval evaluation harness, per-retrieval telemetry that attributes every dropped candidate to the gate that dropped it, and an audit trail for every sleep cycle.

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
- **Surface:** ~100 REST routes, 45 agent tools, 37 ORM tables, migrations through 072
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
