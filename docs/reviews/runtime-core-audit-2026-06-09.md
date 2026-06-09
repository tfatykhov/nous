# Runtime Core Audit — 2026-06-09

**Scope:** `nous/api/runner.py`, `nous/api/anthropic_client.py`, `nous/api/compaction.py`,
`nous/api/smart_compress.py`, `nous/api/tool_cache.py`, `nous/api/cache_optimizer.py`,
`nous/api/models.py`, `nous/main.py`, `nous/telegram_bot.py`, `nous/events.py`.

**Method:** code-only; every claim verified against function bodies. Reachability checked against
`nous/config.py` defaults AND the prod overlay `.env.prod-snapshot`. Prod-relevant facts used below:
API backend = **SDK** (default `sdk`, not overridden in prod), `NOUS_MODEL=claude-opus-4-8`,
`NOUS_CONTEXT_WINDOW=700000`, `NOUS_MAX_TURNS=600`, `NOUS_TOOL_TIMEOUT=2000`,
`NOUS_KEEP_LAST_TOOL_RESULTS=50`, `NOUS_TOOL_METADATA_DEGRADE_AFTER=60`, `NOUS_TOOL_HARD_CLEAR_AFTER=90`,
`NOUS_ACTION_GATING_ENABLED=false`, compaction + pruning + smart-compress + F048 streaming all on,
thinking adaptive, `NOUS_SUBTASK_WORKERS=6` (concurrent background turns on the **same** AgentRunner
instance as chat).

Verdicts: **LIVE** (reachable in prod config), **LATENT** (reachable under non-default but supported
config), **INERT** (flag-gated off everywhere), **DEAD** (no caller).

---

## 1. How it actually works (brief)

**Turn flow (non-streaming).** `AgentRunner.run_turn` (runner.py:312) touches the session monitor
synchronously, takes a per-session `asyncio.Lock` (runner.py:381), runs `cognitive.pre_turn`,
appends the user message to the in-memory `Conversation` (plain-text `Message` objects only),
optionally compacts history (Layer 2) under a per-session compaction lock, then enters `_tool_loop`
(runner.py:1427). The loop builds messages fresh from `conversation.messages` each turn
(`_format_messages`, runner.py:2498 — ALL messages when compaction enabled), calls the API via
`_call_api` → backend client, dispatches every `tool_use` block via `ToolDispatcher.dispatch`,
appends results as a single user message, smart-compresses (`smart_compress`, runner.py:1745) and
caches non-refetchable originals (`tool_cache`), prunes old tool results in-turn
(`prune_tool_results`, runner.py:1828), and repeats until no `tool_use` blocks or `max_turns`.
**Only the final response text is persisted to `conversation.messages`** — intermediate tool
exchange is in-turn-local by design. `post_turn` always runs, then a caught exception is re-raised
after cleanup (runner.py:556).

**Streaming.** `stream_chat` (runner.py:911) mirrors run_turn but reconstructs blocks from SSE
events, yields `StreamEvent`s, holds the session lock across yields, shields `post_turn` in its
`finally`. rest.py pumps it via fresh `create_task(aiter.__anext__())` per chunk, racing a 15s SSE
comment-ping (rest.py:151-221).

**API clients.** `create_client` (anthropic_client.py:1190) picks `SdkAnthropicClient` (prod) or
`HttpxAnthropicClient`. Both set the "You are Claude Code…" preamble as system block 0 with
`cache_control` (runner.py:720, 777) — the OAT 429 requirement is met on both the 3-tier and legacy
paths. Max cache breakpoints = preamble + static + semi_stable + last-user-message = 4 (exactly the
API limit). F048 background turns (`is_background=True`) route through `call_streaming_aggregated`
with truncated-stream detection (httpx: anthropic_client.py:736-744; SDK: `messages.stream()` +
`get_final_message()`), and both transports get TCP keep-alive socket options + env-proxy mounts.

**Lifecycle.** `main.create_components` wires everything in dependency order on the uvicorn loop via
Starlette lifespan; `shutdown_components` (main.py:779) stops heartbeat → subtask pool → scheduler →
decision reviewer → session monitor → bus (drains queue after cancelling the loop task) → HTTP
clients → runner → API client → heart/brain → DB. Heartbeat's dedicated API client is closed inside
`HeartbeatRunner.stop()` (heartbeat/runner.py:133-139).

**Event bus.** Bounded queue (1000), `put_nowait` with drop-on-full, single consumer task, handlers
run concurrently via `gather` with per-handler exception isolation (`_safe_handle`), DB persister
awaited inline before handlers.

**Telegram bot.** Separate container (`python -m nous.telegram_bot`, compose `telegram` service,
distinct `TELEGRAM_BOT_TOKEN` env). Long-polls `getUpdates`, handles updates **sequentially**, and
proxies to `/chat/stream` with `read=None` timeout (server SSE pings keep the socket warm).
`StreamingMessage` throttles edits to 1.2s, splits >4000-char messages, converts markdown → HTML
with a plain-text fallback retry when Telegram rejects parse_mode.

---

## 2. Findings register

### P1 — none found

No live-path data-loss/corruption bug was confirmed at P1 severity. The closest candidates are
RT-1 (telemetry corruption + crash variant) and RT-2 (guard evidence poisoning), both classified P2
for the reasons given inline.

### P2

---

**RT-1 — Streaming usage: `message_delta` input_tokens double-counted; SDK conversion can inject
`None` and TypeError the turn.**
**Severity:** P2 · **Reachability:** LIVE (prod = SDK backend + Telegram streaming)
**Where:** nous/api/anthropic_client.py:1163-1176 (`_convert_sdk_event`, `message_delta` branch);
nous/api/runner.py:1193-1199 (`stream_chat` `done` handler); runner.py:1118-1122 (`message_start`).
**Evidence:** `stream_chat` adds `input_tokens` to `total_usage` at **both** `message_start`
(runner.py:1120) and `done` (runner.py:1196). The installed SDK (anthropic 0.85.0, verified) defines
`MessageDeltaUsage.input_tokens: Optional[int] = None`; `_convert_sdk_event` builds
`{"input_tokens": getattr(event.usage, "input_tokens", 0), ...}` — when the server populates it
(current API sends cumulative usage on `message_delta`), the same input tokens are summed twice →
inflated usage in the Telegram footer and `/chat/stream` `done` event. When the server omits it, the
dict value is `None` (the key exists, so `.get("input_tokens", 0)` returns `None`) and
`total_usage["input_tokens"] += None` raises TypeError, which the broad handler at runner.py:1370
converts into "I encountered an error…" — discarding an otherwise-successful streamed turn.
**Fix:** in `_convert_sdk_event`, coerce `None → 0` (`getattr(...) or 0`); in `stream_chat`'s `done`
branch, stop adding `input_tokens` (it is already captured at `message_start`) or replace instead of
add.

---

**RT-2 — F059 hallucination guard compares the new summary against input that EXCLUDES the prior
summary → systematic false suspects from the 2nd compaction onward.**
**Severity:** P2 · **Reachability:** LIVE (guard on by default; fires whenever compaction runs more
than once per session — rare in prod due to the 420K threshold, but every fire is wrong)
**Where:** nous/api/compaction.py:745-749 vs. 717-719 and 858-864.
**Evidence:** `_summarize` feeds the model `existing_summary + new messages` and instructs
"PRESERVE existing info" (UPDATE_SYSTEM_PROMPT, compaction.py:96). The guard's reference text is
`self._serialize_for_summary(old_messages[serialize_start:])` (compaction.py:746-748) where
`serialize_start=2` deliberately skips the synthetic `[Previous conversation summary]` prefix pair —
i.e. it excludes exactly the text the carried-over entities come from. Every entity correctly
preserved from the previous summary that doesn't reappear in the new message window is reported as a
hallucination suspect. Consequences: (a) noisy `f059_hallucination_guard` events poison the audit
data meant to justify flipping `compaction_hallucination_fallback_enabled`; (b) if that flag is ever
flipped, legitimate summaries get destroyed (fallback truncation drops `conversation.summary`
entirely, compaction.py:783-788).
**Fix:** include `existing_summary` in `input_text` when present
(`input_text = (existing_summary or "") + serialized`).

---

**RT-3 — `NOUS_TOOL_METADATA_DEGRADE_AFTER` / `NOUS_TOOL_HARD_CLEAR_AFTER` are severed wires:
validated in Settings, never consumed; pruning uses hardcoded per-profile ages.**
**Severity:** P2 · **Reachability:** LIVE (prod sets 60/90 expecting late clearing)
**Where:** nous/config.py:452-460, 1452-1456 (definition + cross-validation only — grep confirms no
other consumer); nous/api/compaction.py:593-595 uses `DECAY_PROFILE_AGES` from
nous/cognitive/schemas.py:40-45 (`preserve (8,999,20)`, `aggressive (2,4,8)`, `standard (3,8,12)`,
`conservative (5,10,15)`).
**Evidence:** `prune_tool_results` derives degrade/clear ages exclusively from the profile table.
With prod's `keep_last_tool_results=50`, every tool result older than the last 50 has
age ≥ 51 ≥ all profile clear ages → **immediately hard-cleared** (content replaced with
"[Tool output cleared…]", compaction.py:599-610), skipping the soft-trim and metadata tiers. The
operator's explicit 60/90 settings — documented as functional in CLAUDE.md — are silently ignored.
In a 600-max-turn agentic session this clears context the operator paid to keep.
**Fix:** either thread `settings.tool_metadata_degrade_after/_hard_clear_after` into
`DECAY_PROFILE_AGES` lookup as the "standard" tuple (scaling the profiles), or delete the settings
and the CLAUDE.md rows so the knob isn't a lie.

---

**RT-4 — Context-logger attribution state is shared mutable instance state across concurrent turns.**
**Severity:** P2 · **Reachability:** LIVE (prod runs 6 subtask workers + chat + DAG turns on the
same `AgentRunner`; `context_log_enabled=true` and `NOUS_CONTEXT_LOG_FULL_PAYLOAD=true` in prod)
**Where:** nous/api/runner.py:163-167 (`_current_session_id/_current_turn_number/_current_frame_id/
_current_call_type/_last_context_entry_id`), written at runner.py:420-423 and 977-981, read inside
`_build_api_payload` (runner.py:828-847), consumed at runner.py:1601-1610 and 1202-1211.
**Evidence:** Two concurrent turns (e.g. a chat turn and a subtask turn — different sessions, so the
per-session lock does not serialize them) interleave on `await` points: turn B overwrites
`_current_session_id` before turn A's `_call_api` logs, so A's context_log row is attributed to B's
session/frame/call_type; `_last_context_entry_id` is a single slot, so usage/stop_reason updates can
be attached to the wrong entry or lost (`= None  # Consumed`). Telemetry-only corruption (the
payload itself is passed explicitly), hence P2 not P1.
**Fix:** pass a per-call context object (session_id, turn, frame, call_type) down into
`_build_api_payload` and return the entry id to the call site, or use a ContextVar like F071 does.

---

**RT-5 — `HttpxAnthropicClient.call`: non-retryable errors (4xx) are retried `_MAX_RETRIES+1` times
with NO backoff (missing `break`).**
**Severity:** P2 · **Reachability:** LATENT (httpx backend only; prod uses SDK)
**Where:** nous/api/anthropic_client.py:506-509.
**Evidence:** after `last_error = RuntimeError(...)` there is no `break`/`raise`; the `for attempt`
loop falls through to the next iteration, immediately re-POSTing the full payload. A 400
(invalid_request / context-too-long), 401, or 403 is sent 6 times back-to-back before the final
raise. Only `httpx.HTTPError` (anthropic_client.py:518-520) breaks.
**Fix:** add `break` after assigning `last_error` in the non-retryable branch.

---

**RT-6 — `HttpxAnthropicClient.stream`: mid-stream `TimeoutException` retries the whole request
AFTER events were already yielded → duplicated deltas to the consumer.**
**Severity:** P2 · **Reachability:** LATENT (httpx backend only)
**Where:** nous/api/anthropic_client.py:597-602 (retry `continue` inside the attempt loop wraps the
`aiter_lines()` consumption at 585-595).
**Evidence:** a `ReadTimeout` raised while iterating SSE lines (after N events have already been
yielded to `stream_chat` / `call_streaming_aggregated`) is caught and the request is re-sent from
scratch; the new stream re-yields the response from the beginning. In `stream_chat` the flat
`text_parts` list (runner.py:1101, 1160) is appended without reset → duplicated text shown to the
user and stored in history. In `call_streaming_aggregated`, `text_parts[idx]` is reset on
`text_block_start` so duplication is partial, but `message_start` usage is `update()`-merged twice.
Also: a `json.JSONDecodeError` from a truncated SSE line (anthropic_client.py:588) is not caught by
any handler in `stream()` and propagates raw.
**Fix:** track "first event yielded" and disable retry after it (raise/yield error instead);
tolerate malformed SSE lines.

---

**RT-7 — `create_components` raises NameError at the return statement when
`NOUS_EVENT_BUS_ENABLED=false`.**
**Severity:** P2 · **Reachability:** LATENT (default true, prod true; any operator disabling the bus
gets a startup crash)
**Where:** nous/main.py:386 (`sleep_handler`), main.py:428-436 (`decision_reviewer`) — both defined
only inside `if bus is not None:` (main.py:244) but referenced unconditionally in the return dict at
main.py:768 and 770.
**Evidence:** with the bus disabled, neither local exists; `return {... "decision_reviewer":
decision_reviewer, "sleep_handler": sleep_handler ...}` raises `NameError` and the lifespan never
yields. (`rubric_evolver` is correctly pre-initialized at main.py:241; these two are not.)
**Fix:** initialize `sleep_handler = None` and `decision_reviewer = None` before the bus guard.

---

**RT-8 — Telegram `StreamingMessage` overflow: once a streamed reply crosses 4000 chars, EVERY
subsequent edit spawns a new Telegram message duplicating the head.**
**Severity:** P2 · **Reachability:** LIVE
**Where:** nous/telegram_bot.py:429-446 (`_send_or_edit` overflow branch) + 332-344 (`update`
rebuilds the FULL display text from `_base_text` every delta).
**Evidence:** after the first overflow, `message_id` points at the overflow message and
`self.text = overflow`, but the next `append_text` calls
`update(self._base_text + text)` → `_build_display_text()` reconstructs the **entire** text
(`_base_text` was never truncated). `len > 4000` is true again, so the code edits the current
(overflow) message back to the truncated **head** and sends the tail as yet another new message.
Each throttled edit cycle (~1.2s) emits one more duplicate message until the stream ends; the
visible chat fills with repeated 4000-char heads.
**Fix:** track a per-message base offset (only render `_base_text[sent_offset:]` into the active
message) instead of re-rendering the full text.

---

**RT-9 — Telegram bot processes updates strictly sequentially with an unbounded stream read — one
hung/long turn blocks ALL commands including `/new`.**
**Severity:** P2 · **Reachability:** LIVE
**Where:** nous/telegram_bot.py:492-505 (`for update in updates: await self._handle_update(update)`),
724-732 (`read=None` stream timeout in `_chat_streaming`).
**Evidence:** `_handle_update` awaits the full streaming turn inline; there is no per-chat task
spawn and no overall deadline on the SSE read (`read=None`, justified by server pings — but if the
server keeps the socket open while wedged, e.g. a stuck tool with prod `NOUS_TOOL_TIMEOUT=2000` ≈ 33
min, the poll loop is blocked for the duration). During that window `/new` — the user's only
recovery lever — is queued behind the hung turn, as is every other message.
**Fix:** dispatch `_handle_update` as a task (per-chat serialization if desired) and/or add a
generous absolute deadline around the stream.

---

### P3

**RT-10 — Tool timeout (`NOUS_TOOL_TIMEOUT`) is only enforced on the streaming path.**
LIVE · runner.py:1727-1730 awaits `self._dispatcher.dispatch(...)` directly (no `wait_for`);
`_dispatch_with_keepalive` (runner.py:2444-2461, streaming only) is the sole place the timeout is
applied. `ToolDispatcher.dispatch` (tools.py:61-95) has no timeout either. Non-streaming `/chat` and
heartbeat triage rely entirely on per-tool internal timeouts; a tool without one hangs the turn
indefinitely (subtasks are saved by the outer `wait_for`).

**RT-11 — Session-lock entry pop allows brief double-turn concurrency after close.**
LIVE (edge) · runner.py:655 pops `_session_locks[sid]` after releasing; a coroutine still queued on
the OLD lock object proceeds while a newer arrival gets a NEW lock via `_get_session_lock`
(runner.py:241-249) → two turns for the same (resurrected) session can run concurrently. Also,
LRU eviction (runner.py:2348-2352) pops compaction locks/ledgers/corrections but NOT
`_session_locks` — slow leak for evicted-but-never-closed sessions.

**RT-12 — LRU eviction orphans an in-flight session without `end_session`.**
LIVE (requires >100 concurrent sessions) · runner.py:2348-2352 silently drops the `Conversation` of
the least-recently-used session; an in-flight turn keeps appending to the orphaned object and its
episode is never closed at eviction time (F060 sleep recovery is the backstop). No log line is
emitted for the eviction.

**RT-13 — EventBus: in-flight event lost on stop; queue-full drops are the only delivery guarantee.**
LIVE (shutdown window) · events.py:199-206 cancels the consumer task first; an event mid-`_dispatch`
is abandoned (handlers partially run, event not re-queued) before the drain loop at 208-213 runs.
`emit` (events.py:175-184) drops on full queue by design. Acceptable, but worth knowing: events are
at-most-once.

**RT-14 — Fire-and-forget `asyncio.create_task` without strong references (GC risk).**
LIVE · runner.py:206/222 (`_log_f026_decision`, `_log_compaction_guard`), runner.py:2229 (`_ping`),
main.py:239 (actionability backfill). The event loop holds only weak refs to tasks; a pending task
with no external reference can be collected mid-flight (documented CPython footgun). Low observed
probability, silent when it happens (telemetry/backfill loss only).

**RT-15 — `CacheBreakDetector` is global across sessions and call types.**
LIVE · cache_optimizer.py:44-110 holds a single `_previous` state; runner.py:184-186 creates one per
runner. Interleaved sessions (chat ↔ subtask) have different semi-stable tiers, so in
single-breakpoint mode (`cache_single_breakpoint=true`, runner.py:737-749) the semi-stable
`cache_control` is suppressed for nearly every interleaved call, and cache-break telemetry reports
phantom breaks. `reset()` on ANY session end (runner.py:648-649) clears global state. Cost/telemetry
impact only.

**RT-16 — Heartbeat fork never gets `_session_monitor` (and intentionally no context logger), so
triage turns skip the synchronous activity touch.**
LIVE · runner.py:283-304 (`fork` copies api/dispatcher/ledgers only); main.py:526-533 wires
`_session_monitor` onto the main runner only. The mid-turn-close race that `touch()` exists to fix
(runner.py:369-373) remains open for multi-turn heartbeat triage sessions; classification still
arrives via the `turn_completed` bus event (`is_background` in event data, layer.py:1187,
session_monitor.py:102-111), so #462 sleep-gating itself is intact.

**RT-17 — In-turn cache key never surfaced to the model.**
LIVE · runner.py:1755 assigns `hash_key = await cache_compressed_result(...)` and never uses it; the
SmartCompressed marker (smart_compress.py:208-210) contains no retrieval key. The model can only
discover `cache_retrieve` keys from next-turn context hints (cognitive/context.py:411-412) — within
the turn that produced the compressed result, retrieval is impossible.

**RT-18 — Telegram: `append_tool_indicator`/`append_thinking` bypass the 1.2s edit throttle; no
Telegram 429 `retry_after` handling anywhere.**
LIVE · telegram_bot.py:346-350 and 323-330 call `_send_or_edit()` unconditionally (only `update()`
checks `_min_interval`, telegram_bot.py:340-343). A burst of tool starts produces unthrottled
`editMessageText` calls; on 429 `_tg` (telegram_bot.py:813-839) misdiagnoses the failure as a
parse_mode problem, retries once stripped, then gives up (returning a `list` where callers expect a
`dict`).

**RT-19 — Telegram session TTL race + URL-transported payloads.**
LIVE (edge) · telegram_bot.py:34 hardcodes `SESSION_TTL_SECONDS=1800` equal to prod
`NOUS_SESSION_TIMEOUT` — at the boundary the bot reuses a session id the server just closed
(conversation state deleted → silent context reset mid-conversation). `_tg` sends all calls as
**GET** with the message text in query params (telegram_bot.py:819-821) — ~4000-char texts ride in
the URL; works today but is fragile (proxy/URL limits) and leaks message text into any intermediary
logs. `_LINK_RE` href substitution (telegram_bot.py:154) doesn't escape `"` in URLs → broken HTML →
plain-text fallback fires.

**RT-20 — `stream_chat` swallows `CancelledError` and then yields `done`.**
LIVE (edge) · runner.py:1376-1380 converts client-disconnect cancellation into `error="cancelled"`,
then control reaches `yield StreamEvent(type="done", ...)` (runner.py:1421) — yielding again after
cancellation only works because rest.py suppresses everything in its finally (rest.py:206-212);
under a different consumer this is a `RuntimeError` waiting to happen. Partial streamed text is also
not appended to history on cancel (user saw words; history has nothing).

**RT-21 — Token-estimate `else`-branch and compaction health log inaccuracies.**
LIVE (cosmetic) · runner.py:481-485 / 1049-1053 estimate `len(content)//4` only when the compactor
is disabled — but in run_turn the "Context health" log after a compaction (runner.py:487-492) prints
pre-compaction token counts (messages rebuilt only inside `_tool_loop`). `TokenEstimator.calibrate`
(compaction.py:418-424) divides API `input_tokens` (which include system prompt + tools) by message
chars only — ratio systematically overestimates (acknowledged in docstring; safety margin absorbs).

**RT-22 — `MODEL_CONTEXT_WINDOWS` has no entry for prod's model.**
LIVE (masked) · cognitive/schemas.py:15-23 lacks `claude-opus-4-8` (and any 4-8 family) → fallback
200K. Prod masks this with `NOUS_CONTEXT_WINDOW=700000`; if that override is ever removed, the
compaction threshold drops to 120K and budget scaling shrinks silently. Worth adding the entry.

**RT-23 — SmartCompress + tool-cache run ONLY on the non-streaming loop.**
LIVE · `stream_chat`'s dispatch path (runner.py:1300-1338) appends raw `result_text` — no
`smart_compress`, no `cache_compressed_result`. Telegram turns (the primary chat surface in prod)
never compress web_search/web_fetch results at ingestion; only the pruning tiers apply. Divergence
between the two loops is undocumented.

### INFO

- **I-1** runner.py:1493/2362 `_stream_with_keepalive` annotates `system_prompt: str` but receives
  the F036 dict; works only because it's passed through opaquely.
- **I-2** `_build_api_payload` adds `cache_control` to the last user message every call
  (runner.py:684-705) — moving breakpoint across tool-loop iterations is correct usage; total
  breakpoints sit exactly at the API max of 4.
- **I-3** events.py `_dispatch` awaits the DB persister inline before handlers — a slow DB write
  delays all handler fan-out for that event (single consumer task). Acceptable at current volume.
- **I-4** main.py:670-672 heartbeat API client is created inside the `try:` — a non-ImportError
  failure between creation and `HeartbeatRunner.start()` leaks the client (startup aborts anyway).
- **I-5** Compose `telegram` service reads `TELEGRAM_BOT_TOKEN` (docker-compose.yml:97), a different
  variable from the app's `NOUS_TELEGRAM_BOT_TOKEN`; `.env.prod-snapshot` contains only the latter —
  if the host `.env` matches the snapshot, the bot container exits at startup ("TELEGRAM_BOT_TOKEN
  not set", telegram_bot.py:853-856). Unverifiable from the repo; flagging for operator check.
- **I-6** `fork()` shares the `_ledgers` dict across runners without locking — safe under GIL
  dict-op atomicity for current usage, but `ExecutionLedger` itself is not designed for cross-task
  mutation.
- **I-7** prod `NOUS_MAX_TURNS=600` + no tool-call cap on chat turns means a runaway chat tool loop
  is bounded only by tokens/cost; combined with RT-3's early hard-clearing, very long loops silently
  degrade their own context.

---

## 3. Dead-code inventory

| Item | Location | Note |
|---|---|---|
| `format_event_bus_status`, `format_trace_summary`, `format_context_summary` | telegram_bot.py:205, 230, 248 | No `/status`, `/trace`, `/context` command handlers exist in `_handle_update` (telegram_bot.py:507-557). Planned F035 integration (docs/superpowers/plans/2026-04-04-f035-observability.md:874) never wired. DEAD. |
| `has_cache_entries` | tool_cache.py:141-147 | No production caller (only docs/tests reference it). DEAD. |
| `StreamEvent.tool_input` field | anthropic_client.py:52 | Never populated by either parser; tool inputs travel via `tool_input_delta` fragments. DEAD field. |
| `_get_last_bot_message_id` | telegram_bot.py:809-811 | Always returns None → the `setMessageReaction` call at telegram_bot.py:657-665 always fires with `message_id=None` and fails silently. Broken vestigial feature (non-streaming `/debug` path only). |
| `settings.tool_metadata_degrade_after` / `tool_hard_clear_after` | config.py:452-460 | Validated, documented, never consumed (see RT-3). |
| `hash_key` local | runner.py:1755 | Assigned, never used (see RT-17). |
| `_parse_sse_event` / `ApiResponse`/`Conversation`/`Message` re-exports | runner.py:23, 29 | Backward-compat re-exports for tests; intentional. |
| ActionGate machinery (`_call_gate_model`, gate branches) | runner.py:228-239, 1663-1693, 1267-1298 | INERT in prod (`NOUS_ACTION_GATING_ENABLED=false`); live code path elsewhere. |
| F071 exclusion wiring on the streaming path | runner.py:1068-1095 | INERT (flag default false, unset in prod) and documented as non-functional under SSE even when on. |

---

## 4. Improvement opportunities

1. **Unify the two tool loops.** `_tool_loop` and `stream_chat` duplicate ~200 lines (gating,
   ledger, episode-id injection, pruning) with already-diverged behavior (smart-compress, tool
   timeout, F064.1 pings, F071). Extract a shared per-tool-call pipeline; the streaming loop should
   differ only in transport.
2. **Per-call attribution object for the context logger** (fixes RT-4 structurally) — thread an
   immutable `CallMeta` through `_call_api`/`_build_api_payload` instead of instance fields.
3. **Usage accounting normalization layer.** Define one place that merges
   `message_start`/`message_delta` usage with explicit None-coercion and replace-not-add semantics
   (fixes RT-1 for both backends and future API shape drift).
4. **Bring the httpx client to parity or delete it.** RT-5/RT-6 plus the SDK's native retry handling
   suggest the httpx backend is under-maintained; if it exists only as a fallback, gate it with a
   startup warning and a test matrix, or remove it.
5. **Telegram bot hardening:** per-chat task dispatch with an absolute turn deadline (RT-9), offset-
   based message continuation (RT-8), honor `retry_after` from Telegram 429 payloads (RT-18), POST
   with JSON body instead of GET query params (RT-19).
6. **Make pruning settings real** (RT-3): scale `DECAY_PROFILE_AGES` from the two settings, keeping
   profile ratios.
7. **Event-bus graceful stop:** set `_running=False`, `await queue.join()`-style drain (or sentinel)
   BEFORE cancelling, so the in-flight event finishes (RT-13).
8. **Persist conversation state on every turn** (not only post-compaction) if restart amnesia for
   active sessions matters — `_restore_conversation` already supports it; today it only ever sees
   compacted sessions.
9. **Add `claude-opus-4-8` (and 4-7/4-8 family) to `MODEL_CONTEXT_WINDOWS`** so prod doesn't depend
   on the manual `NOUS_CONTEXT_WINDOW` override (RT-22).
