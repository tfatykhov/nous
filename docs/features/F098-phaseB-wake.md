# F098 Phase B — Wake Turn: Options

Status: **Option C chosen** (Tim, 2026-10-04) — built in PR "F098 Phase B", stacked on #694
Author: Nous, 2026-10-04
Verified against: PR #694 head `eaaacfc` (branch `feature/F098-result-inbox`) + prod DB, 30-day window
Parent spec: `docs/features/F098-result-inbox-and-wake.md` §3.4

## 0. Prerequisite — Phase A is not merge-ready yet (all four fixed on #694)

Phase B builds on Phase A, so these items come first. They are all small.

1. **CI red:** `tests/test_database.py::test_all_tables_exist` fails because its list of expected tables doesn't include `heart.result_inbox` and `heart.channel_sessions`. It is the only failure (1 failed / 9373 passed). The fix is two lines.
2. **Codex P1, `result_inbox.py:169`:** the inbox identity is `(source_kind, source_id)`. After `retry_node`, `delivery_generation` is incremented, but the second terminal outcome hits `ON CONFLICT DO NOTHING`. So a retried DAG's eventual *success* is never delivered. Fix: add `delivery_generation` to the unique key (default 0 for subtasks).
3. **Codex P1, `result_inbox.py:312`:** a failed one-shot subtask insert is swallowed. With the flag on, the result is then lost. Fix: add a terminal-subtask reconciler sweep that re-inserts terminal subtasks from the last 72 h that have a routing key and no inbox row. **The Phase C memory writer reuses this sweeper.**
4. **Codex P2, `result_inbox.py:415`:** the overflow note lists every omitted id. Fix: list only the count, plus at most 5 ids.

## 1. What actually needs waking (prod, last 30 days)

Completed subtasks, non-DAG-node:
- **Conversation-origin** (a parent session exists): **12**, about 0.4 per day. *This is the population the wake turn is for.*
- **Scheduled with `notify=true`, no conversation:** **164**. These are Gap Scout, pre-market, AI briefing, the decision-sweep triage, the Italy weather check and similar. Most already email or ping, and today they get the raw 500-char `_notify_telegram` ping.
- **Background, `notify=false`:** **937**. These are the LinkedIn monitor, the Codex watchdog, the PR finisher, Garmin sync, micro-app button taps and similar. They must never wake.
- **DAG-node subtasks:** **523**. They report through their DAG.

DAGs: 134 in 30 days (111 completed, 18 failed, 5 cancelled). Before #694 none recorded `origin_channel`, so the conversation-origin share is unknown. Judging by scheduler names, most are scheduled.

**Conclusion:** the wake path should cover conversation-origin results only. That is about 1 per day once DAGs are included. Scheduled results already have a delivery channel, and waking an LLM for them would add noise and cost without adding signal. So volume, rate limits and LLM cost are not the hard part. **Session continuity and racing a live turn are.**

## 2. Options

### Option A — Nudge only (no LLM turn)
When a conversation-origin row lands, the server sends a deterministic Telegram message through the Bot API, for example: `📬 2 results ready — "F098 build", "tinyHippo review". Reply to discuss.` The row stays undelivered, and Tim's next message injects it through the Phase A reader.
- **Pros:** about 120 LOC. No LLM cost. No session or tool-policy questions. It can't race a live turn because it never runs a turn. Prompt injection is impossible.
- **Cons:** the message isn't in Nous's voice and doesn't give an answer. Tim still has to type something to get the content, which is the friction behind "I had to ask for results". It's only half of G3.
- **Risk:** very low.

### Option B — Server-side wake turn (as written in spec §3.4)
`WakeDispatcher` runs in the server. It is triggered by `result_inbox.created` plus a 60 s sweep. It debounces per channel, applies quiet hours and a rate limit, then calls `run_turn` in `channel_sessions.session_id` (or a new session) and sends the reply through the Bot API with `format_telegram_html`.
- **Pros:** self-contained in the server. Works for any channel type later (companion, API).
- **Cons:**
  - **Session split.** The bot's chat→session map is in-process (`telegram_bot.py:491`). A wake turn in a new session is invisible to the bot, so Tim's reply "tell me more" lands in a *different* session with no wake-turn history. The fix is for the bot to adopt `channel_sessions` on each message, which means extra bot changes and an extra REST call per message.
  - **Live-turn race** is only approximate (`SessionMonitor.touch` reflects last activity, not "in flight").
  - **Two code paths** for sending and formatting Telegram replies.
  - Both existing pushes need suppressing (`_notify_telegram`, and F087 `_leg_telegram`, which must become *not required* rather than "ok", or F087 retry semantics change).
- **Size:** about 450–550 LOC plus tests. This is the size that pushed it out of the Phase A PR.
- **Risk:** medium. Duplicate pings and split-session confusion are the likely failure modes.

### Option C — Bot-driven wake (pull) ★ recommended — ✅ CHOSEN
The bot already owns the session map and the reply/streaming/formatting path, so let it start the wake.
- **Server side:**
  - `GET /inbox/wake?channel=telegram:<id>` returns `{wake: bool, count, titles[]}`. It does *not* claim rows. Quiet hours, the per-channel rate limit, the debounce window ("the newest row is older than 20 s") and the origin filter all live here, in one testable function.
  - `/chat/stream` accepts `wake: true`. That turn runs as a new `ContextKind "result_wake"`. The Phase A reader in `pre_turn` claims and injects the rows, so exactly-once delivery is still enforced by the existing `UPDATE … RETURNING`.
- **Bot side:** one `asyncio` task polls `/inbox/wake` every 30 s for each known chat. If `wake` is true and the chat isn't busy (a per-chat flag set around `_chat`/`_chat_streaming`), it calls the normal streaming path. It uses its *own* `session_id` for that chat, so the session is continuous by construction, and passes `wake=true` with the fixed note "Background results arrived; report them briefly."
- **Pros:**
  - The session split goes away, because the wake turn is in the same session Tim replies in.
  - The live-turn check is exact.
  - The reply uses the same renderer and streaming UX as a normal turn.
  - No new Bot-API send code on the server.
  - Rows are claimed by the same reader as normal turns, so there is no double injection.
- **Cons:**
  - Polling: about 2,880 cheap GETs a day for a single chat, using an indexed partial-index lookup. Acceptable, and it could become long-poll later.
  - Wake only works while the bot process is up. Fine, because the channel *is* the bot.
  - The wake note is in the transcript as a user-role message. Mitigation: the bot doesn't echo it, and it is tagged `[system:wake]` so episode summarisation can drop it.
- **Size:** about 280–350 LOC (server about 150, bot about 100, policy row and config about 30) plus tests.
- **Risk:** low to medium.

### Option D — Hybrid tiering (C for conversation-origin, A for scheduled)
Option C, plus Option A's deterministic nudge *replacing* the raw 500-char `_notify_telegram` ping for the 164 scheduled `notify=true` results per month.
- **Pros:** one consistent "📬" format for every background result, and scheduled results become claimable on reply.
- **Cons:** it reverses Phase A deviation #1 (it needs a default-chat routing key for scheduled rows, i.e. `NOUS_RESULT_INBOX_DAG_SCHEDULED`-style for subtasks). It also touches the scheduled-report UX that Tim currently relies on. That is a separate change for a separate reason.
- **Size:** C plus about 100 LOC.
- **Recommendation:** don't do it in Phase B. Revisit after a week of C.

## 3. Decisions that hold for any option (B/C/D)

- **Tool policy for the wake turn:** new kind `result_wake` with `ContextPolicy(_LOCAL, spawn=False)`. A wake turn *reports*. It doesn't send email, spawn or trade, because the injected content is untrusted subtask output (prompt-injection blast radius). Note that the policy is in WARN mode by Tim's standing rule (2026-10-04), so this is logged rather than blocked for now. The `<result_message>` data-not-instructions guard is the actual barrier until enforcement is turned on.
- **Origin filter:**
  - Wake only for rows with a conversation origin: subtask `parent_channel` set, or DAG `origin_channel` set.
  - Never for rows routed only through the scheduled-DAG default.
  - Never for `notify=false` background tasks.
- **Quiet hours:** reuse `HeartbeatRunner._in_quiet_hours` (`heartbeat/runner.py:1283`), extracted to a module function (23:00–08:00 local). Results that arrive during quiet hours wait for the first poll after 08:00 or for Tim's next message, whichever comes first.
- **Rate limit:** `NOUS_RESULT_WAKE_MAX_PER_HOUR=6` per channel. Debounce is 20 s.
- **Suppression:**
  - When wake is on, `_notify_telegram` is skipped for rows that will wake.
  - F087 `_leg_telegram` is skipped for DAGs with `origin_channel`. The leg is marked `required=False` with detail `"superseded_by_wake"`, so it is not silently treated as ok.
- **Failure:** set `wake_attempted_at` before the turn. If the turn fails, the rows were already claimed, which matches Phase A deviation #3. Option C mitigation: the reader only claims inside the wake turn, and if the stream errors before the first token, the bot calls `POST /inbox/release` for the claimed ids. That is an optional nicety. Leave it out of v1 unless it's cheap.
- **Flag:** `NOUS_RESULT_WAKE_ENABLED=false`. It requires `NOUS_RESULT_INBOX_ENABLED=true`, and startup logs a warning when wake is on but the inbox is off.

## 4. Recommendation

**Option C.** It is the only option that delivers G3 (a report in Nous's own voice, without Tim having to ask) and avoids the session-split bug. It is smaller than B. And it puts the decision logic (quiet hours, rate, debounce, filter) in one server function that's easy to test.

Order of work: fix the Phase A blockers in §0 → merge #694 → flip the inbox flag and watch delivery rate for 3 days → build C as PR "F098 Phase B" → flip wake and watch a week for double pings. Phase C (memory) is independent and can be built in parallel with B (see the Phase C spec).

Acceptance tests for C:
1. The `/inbox/wake` matrix: quiet hours, rate limit, debounce, origin filter, empty.
2. A busy chat is never woken.
3. Three results within 20 s produce one wake turn, and all three are injected once.
4. The wake turn runs as `result_wake` and is denied a side-effect tool (WARN log asserted).
5. With wake on, there is no `_notify_telegram` ping and the F087 Telegram leg is not required.
6. A wake turn followed by a reply from Tim stays in the same `session_id`.
7. Flag off: no polling task starts and behaviour is identical to Phase A.
