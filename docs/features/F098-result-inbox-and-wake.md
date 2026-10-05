# F098 — Result Inbox & Wake Turn (subtask/DAG → main-loop messaging)

Status: Draft → Phase A in build
Author: Nous (with Tim), 2026-10-04
Verified against: main @ 5e95767
Decision: 1c3472a7 (fix inside Nous with a channel-keyed inbox + wake turn; do NOT adopt Cisco CSTP)
Related: F087 (DAG delivery spine), F090 (DAG coordination), F061/F062 (subtask hardening / spawn_sync), 011.2 (subtask result injection), Doc 019 R2/R5

## 1. Problem (verified in code + DB)

1. **Results are keyed to an ephemeral session, not to the conversation.**
   `heart.subtasks.get_undelivered(parent_session_id)` (nous/heart/subtasks.py:346) is called from
   `cognitive/layer.py:769` with the *current* session id. Telegram sessions expire after 1800 s idle
   (`telegram_bot.py:40 SESSION_TTL_SECONDS`) and the chat→session map lives only in the bot process
   (`telegram_bot.py:491 self._sessions`). Any subtask that finishes after the session rolls over is
   never injected. Last 30 days: 14 conversation-spawned subtasks with a parent session, **10 never delivered**.
2. **Nothing wakes the main loop.** A finished subtask sits with `delivered=false` until Tim happens to
   send another message in the same session. `subtask_worker._notify_telegram` (subtask_worker.py:500)
   sends a raw bot message with the result cut to 500 chars — not in Nous's voice, not in history.
3. **DAG terminal events have no consumer.** `nous/dag/delivery.py:233` emits `dag.completed` /
   `dag.failed`; `grep -rn 'bus.on("dag.'` → 0 hits. DAG outcomes reach Telegram as a template push and a
   separate `dag-summary-*` session, never the conversation.
4. **Results never become memory.** Both subtask run paths pass `skip_episode=True`
   (subtask_executor.py:259, legacy subtask_worker.py:358). Results live only in `heart.subtasks.result`,
   which recall_deep does not search. 1,634 completed subtasks in 30 days; 1,630 never marked delivered.

## 2. Goals / non-goals

Goals
- G1 Every result that *should* reach the conversation reaches it exactly once, regardless of session rollover.
- G2 Subtask and DAG results flow through ONE reader path (single inbox), with a small typed envelope.
- G3 Optionally (flag), a finished result wakes a short Nous turn that reports in Nous's own voice.
- G4 Optionally (flag), meaningful results become searchable memory.
- G5 Measurable: delivery rate is a metric with a regression test for the 30-min expiry case.

Non-goals
- No payload compression / CSTP. Results are small text; the bug is routing + wake, not encoding.
- No cross-agent protocol (A2A) — internal typed rows only (Doc 019 R4).
- No sibling-to-sibling messaging (that is F090 Phase 2 worklog, separately gated).
- No retro-delivery of historical results (see §4.6).

## 3. Design overview

```
subtask terminal ─┐                                   ┌─> layer.py step 3b: inject (channel OR session match)
                  ├─> heart.result_inbox (row) ───────┤
dag.completed/    ┘     channel, envelope, summary    └─> [flag] WakeDispatcher: debounce → run_turn in channel → Telegram
dag.failed (bus)                                       └─> [flag] memory writer (episode + chunks, noise-filtered)
```

### 3.1 Channel identity
- `channel` = stable string for where the conversation lives: `telegram:<chat_id>`, `api:<user_id|default>`,
  `companion:<...>`. Never a session id.
- Telegram bot adds `"chat_id": <int>` to both `/chat` and streaming payloads (telegram_bot.py:695, :794).
- REST (`nous/api/rest.py:142/198`) derives `channel`: explicit `channel` field → `telegram:<chat_id>` when
  platform==telegram and chat_id present → `telegram:<settings.telegram_chat_id>` when platform==telegram
  without chat_id (back-compat with an old bot) → `None` otherwise.
- `channel` is threaded through `run_turn`/`stream_chat` (runner.py ~1183 / ~1887, alongside `platform`) into
  `ExecutionContext` (new optional field `channel`) so tools can read it.
- Server keeps `channel → latest session_id, last_active` in a small table (`heart.channel_sessions`) updated
  on every turn. This is what the wake turn uses (§3.4) and survives restarts (the bot's in-memory map does not).

### 3.2 Origin capture
- `heart.subtasks` gets `parent_channel TEXT NULL`. `spawn_task` (api/tools.py ~3016) passes
  `parent_channel=ctx.channel` next to `parent_session_id`.
- `nous_system.execution_dags` gets `origin_channel TEXT NULL` and `origin_session_id TEXT NULL`, set by
  `dag_create` when called from a conversation turn. Scheduler/heartbeat-created DAGs leave them NULL.
- Subtasks spawned by schedules (task_scheduler.py:189 uses `created_by_session`) inherit the channel stored on
  the schedule if present; otherwise NULL. (Add `created_by_channel` to schedules only if trivial; else defer.)

### 3.3 Result inbox (the envelope)
New table `heart.result_inbox` (migration 081):

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| agent_id | text not null | |
| channel | text null | routing key (preferred) |
| session_id | text null | legacy/secondary routing key |
| source_kind | text not null | `subtask` \| `dag` |
| source_id | uuid not null | subtask id / dag id |
| msg_type | text not null | `INFORM` (success) \| `FAILURE` \| `BLOCKED` (approval-stopped DAG) |
| correlation_id | text null | dag id for DAG nodes; subtask id otherwise |
| reply_to | text null | channel to answer on (usually = channel) |
| title | text not null | task/DAG name, ≤200 chars |
| body | text not null | result text, capped (e.g. 4,000 chars) with full text left in source row |
| created_at | timestamptz | |
| delivered_at | timestamptz null | set when injected into a turn |
| delivered_session_id | text null | audit |
| wake_attempted_at | timestamptz null | wake-turn bookkeeping |
| UNIQUE (source_kind, source_id) | | idempotent writers — prefer duplicates suppressed, never skips |

Partial index on `(agent_id, channel) WHERE delivered_at IS NULL` and `(agent_id, session_id) WHERE delivered_at IS NULL`.

Writers:
- **Subtask**: in both terminal paths (subtask_executor + legacy subtask_worker) after the status write commits,
  insert a row when `parent_channel IS NOT NULL OR parent_session_id IS NOT NULL OR notify`. Skip DAG-node
  subtasks (they report via their DAG) — detect via existing dag_node linkage/metadata.
- **DAG**: a new bus handler `ResultInboxDagListener` registers `bus.on("dag.completed")` and `bus.on("dag.failed")`.
  It inserts a row with `channel = origin_channel` (or `telegram:<settings.telegram_chat_id>` for scheduled DAGs
  when `NOUS_RESULT_INBOX_DAG_SCHEDULED=true`, default false) and `body` = F087 summary/template text.
  Because `EventBus.emit` can drop on QueueFull, the F087 delivery sweep is the durability backstop: the listener
  is idempotent (UNIQUE constraint) and the orchestrator's delivery path may also call the same insert directly.

Reader (cognitive/layer.py step 3b): when `NOUS_RESULT_INBOX_ENABLED`:
- select undelivered rows where `channel = :channel` OR `session_id = :session_id`, ordered by created_at,
  bounded by age (`created_at > now() - NOUS_RESULT_INBOX_MAX_AGE_HOURS`, default 72) and count (default 10;
  if more, inject the 10 newest + a one-line "N older results — use list_tasks/dag_manage").
- format with the envelope (type, title, source id, time) inside `<result_message>` delimiters + a header that
  the content is data, not instructions (cross-source prompt-injection guard, as F090 worklog).
- mark delivered in a nested try (same semantics as today); keep the dynamic-tier routing fix (Audit CL-1).
- Also mark the corresponding `heart.subtasks.delivered=true` so legacy metrics stay coherent.
- When the flag is OFF, the legacy `get_undelivered(session_id)` path runs unchanged.

### 3.4 Wake turn (flag `NOUS_RESULT_WAKE_ENABLED`, default **false**)
`WakeDispatcher` (new handler) listens for inbox inserts (in-process bus event `result_inbox.created`, plus a
periodic sweep every 60 s for rows with `delivered_at IS NULL AND wake_attempted_at IS NULL`):
- Debounce per channel (`NOUS_RESULT_WAKE_DEBOUNCE_S`, default 20) so near-simultaneous results batch into one turn.
- Only for `telegram:*` channels; quiet hours honoured (reuse heartbeat quiet-hours settings); rate limit
  `NOUS_RESULT_WAKE_MAX_PER_HOUR` (default 6) per channel; never wake for rows whose source subtask had `notify=false`
  AND no parent_channel (background noise).
- Skip if the channel had a user turn in the last N seconds that is still running (avoid racing a live turn).
- Runs `run_turn` in `channel_sessions.latest session_id` if active within TTL, else a fresh session that becomes
  the channel's latest. The injected user-side text is a fixed system note ("Background results arrived; report
  them briefly to Tim") — the inbox reader injects the actual results, so they are marked delivered once.
- Sends the reply to Telegram via the Bot API (same mechanism as `delivery.py:_leg_telegram`), formatted with the
  bot's markdown→HTML converter if importable, else plain.
- When wake is ON, `subtask_worker._notify_telegram` is suppressed for rows that will wake (avoid double pings); the
  F087 DAG Telegram template push is suppressed for DAGs with an origin_channel.
- Mark `wake_attempted_at` before running; on failure leave `delivered_at` NULL so the next user turn still injects.

### 3.5 Memory (flag `NOUS_RESULT_MEMORY_ENABLED`, default false)
Noise filter: only rows from subtasks with `notify=true`, or spawned from a conversation (parent_channel set),
or DAG terminal results. Write a short summary episode (title + first ~600 chars, tags `subtask-result`/`dag-result`)
and the full text as episode chunks via the same chunker `ingest_document` uses (source_kind='document' or a new
'result'). Idempotent on source id.

### 3.6 Metrics
- `result_inbox_delivery_rate` = delivered / created, by source_kind, over 7/30 days (expose on the existing
  harness dashboard or `/health/detail` — wherever similar counters live).
- `result_inbox_latency_s` p50/p95 (created_at → delivered_at).
- Wake counts: woken, debounced, rate-limited, quiet-hours-deferred, failed.

## 4. Edge cases
1. **Session expiry (the core bug):** subtask spawned in session S1 on `telegram:X`, S1 expires, Tim writes in S2
   → result injected in S2. Regression test required.
2. **Two sessions on same channel concurrently** (API + Telegram race): delivered once (row-level UPDATE ... WHERE
   delivered_at IS NULL RETURNING id — only rows actually claimed are injected).
3. **Bus drop** for DAG events: F087 sweep backstop + idempotent insert.
4. **DAG-node subtasks** do not create their own inbox rows.
5. **Huge results**: body capped; full text stays in source row; message says how to fetch it.
6. **Historical backlog**: migration does NOT backfill inbox rows from the 1,630 undelivered subtasks.
   Turning the flag on must not flood context. Age bound (72 h) is a second guard.
7. **Prompt injection** from results: delimiters + data-not-instructions header; results never auto-execute anything.
8. **Approval-stopped DAG** → msg_type BLOCKED with the approval line from `dag/approval.py`.

## 5. Phasing
- **Phase A (this PR, flags default OFF except where noted):** channel identity (3.1), origin capture (3.2),
  inbox table + writers + DAG bus listener + reader (3.3), metrics (3.6), tests. `NOUS_RESULT_INBOX_ENABLED`
  default **false** in code; Tim flips it in .env after review.
- **Phase B:** wake turn (3.4) behind its flag — same PR only if it stays small and fully tested; else follow-up PR.
- **Phase C:** memory writer (3.5) — follow-up PR unless trivial.

## 6. Acceptance tests (minimum)
1. Unit: channel derivation matrix (telegram w/ chat_id, w/o chat_id, api, none).
2. Integration (SQLite/test backend per repo conventions): subtask spawned in session S1 with channel C; S1 rolled;
   turn in S2 on C → result injected once; second turn → not injected again.
3. Legacy: flag off → existing get_undelivered behaviour and tests unchanged.
4. DAG listener: emit dag.completed with origin_channel → one inbox row; emit twice → still one row.
5. Concurrency: two readers claim same rows → each row injected exactly once.
6. Age/count bounds honoured; backlog not injected.
7. Wake (if built): debounce batches 3 results into one turn; quiet hours defer; rate limit holds; failure leaves row
   undelivered.
8. Full test suite green; ruff/lint clean per CLAUDE.md.

## 7. Rollout
1. Merge with flags off → deploy → migration 081 applies.
2. Flip `NOUS_RESULT_INBOX_ENABLED=true`; watch delivery rate for 3 days (target ≥ 95% of channel-origin results).
3. Flip wake flag; watch for double pings / noise for a week.
4. Flip memory flag.
