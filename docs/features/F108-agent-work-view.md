# F108: Agent Work View

**Status:** Phase 1 and Phase 2 deployed (all four recording flags on in prod); the full tool result is deployed; Phase 3 tool notes are on in prod since 2026-11-09 (`NOUS_TOOL_NOTES_ENABLED`, which needs `NOUS_TOOL_CALL_LOG_ENABLED`; default off). Phase 1 is the recording. PR 1 (#870) records tool calls and turns. PR 2 (#871) records the model-call numbers. PR 3 (#872) records the model-call text. PR 4 (#873), the last PR of Phase 1, makes the model-call table the one recorder: the context endpoints read it, and `context_log` gets no more rows. Every setting defaults off: `NOUS_TOOL_CALL_LOG_ENABLED` and `NOUS_TOOL_REASON_ENABLED` (PR 1), `NOUS_MODEL_CALL_LOG_ENABLED` (PR 2; the cutover in PR 4), `NOUS_MODEL_CALL_TEXT_ENABLED` (PR 3). Phase 2 is the views: the dashboard's Work and Model calls screens, their read-only endpoints, and the navigation in six groups (see [Phase 2: the views](#phase-2-the-views)). Phase 3 (tool notes) is described in [Phase 3: tool notes](#phase-3-tool-notes-nous_tool_notes_enabled-migration-103).

## Problem

Nobody can answer "what did Nous do in this conversation, and why" from what is stored. Only side-effecting tool calls are stored (the execution ledger), and only the tool names of each turn (`f026_claim_verification` events). Nous's reasons for its tool calls are never stored, so a session's work cannot be followed call by call, nor across sessions.

## What PR 1 records

### Tool calls (`nous_system.tool_call_log`, migration 100)

One row per tool call the model asked for, written after the call by the runner (`AgentRunner._note_tool_call`, through `ToolCallRecorder.record`, `nous/observability/tool_calls.py`). Calls that never ran are recorded too: `outcome` is one of `ran`, `error`, `refused` (a tool policy refused it), `gated` (the F026 action gate blocked it), `suppressed` (a keyed send whose key was already held), `bad_input` (malformed input JSON) or `retired` (a retired tool name); `refused_by` holds the refusal code, or `action_gate`. There is no CHECK on `outcome`, so a new outcome is recorded, not dropped.

A row holds the agent, session and parent session, the turn's id and number, the call's 0-based place in its turn (`call_index`, counted across the turn's stream segments), the kind of work (`conversation`, `subtask`, `workflow_step`, `intention`, `heartbeat`, `background_check`, from the turn's `ExecutionContext.kind`), the subtask and DAG ids when there are any, the canonical tool name and the name the model used (an alias), the reason, the input, a result preview, the error flag and error text, the duration, the start time and the execution-ledger row of a side-effecting call.

The input, result preview, reason and error text are redacted with the execution ledger's redaction before they are cut (`detail_text`); the input's strings are redacted before the input is serialised (`redact_structure`), since a string that holds JSON is escaped in the JSON text, where the patterns cannot see its secrets: at most `NOUS_TOOL_INPUT_MAX_CHARS` (2,000), `NOUS_TOOL_RESULT_PREVIEW_CHARS` (500), `NOUS_TOOL_REASON_MAX_CHARS` (500) and 1,000 characters. Over a cap, the head and the tail are kept around a 16-character marker.

`execute_single_call` (one owner-approved call run with no model in between) writes its tool-call row under a turn id of its own, with no turn number, and no turn row.

Two gaps in the tool-call rows:

- **A dispatch exception that escapes the tool loop writes no tool row.** `ToolDispatcher.dispatch` turns a tool's own exception into an error result, so only two kinds escape: a cancellation (or the stream closed) in the middle of the call, and a failure of the ledger plumbing around it. The execution ledger closes a cancelled call `unknown` at once; after a plumbing failure the row stays `pending` until the ledger sweep marks it; `tool_call_log` has no row for either.
- **A call blocked because its compensation snapshot could not be taken is recorded as `error`** (with `refused_by` null and the `ledger_entry_id` of its row), while its execution-ledger row says `blocked`.

### Turns (`nous_system.agent_turn_log`, migration 100)

One row per turn, `id` = the turn's id, so a turn's tool calls join on `tool_call_log.turn_id`. Written when the turn ends, before `post_turn` (`ToolCallRecorder.record_turn`, an upsert on `id`). It holds what started the turn (the message or the background trigger, at most 500 characters), the memory already in the context (`memory_context`: the ids of the recalled memories under the singular keys `decision`, `fact`, `procedure` and `episode`, at most 50 per kind), the critic's verdict for the turn (the `critic_classified` event's data, or null when the critic did not run), the claim check (the `f026_claim_verification` result, or null), the reply (at most 1,000 characters), the error (at most 1,000; `cancelled` for a cancelled turn), the number of tool calls, and the start and end times. Migration 106 (F110) adds three scalars: `frame_reason`, the frame's `match_method` (`pattern` or `default` from the heuristic, `critic` when the critic replaced the frame in advised mode), `critic_frame`, the frame the critic recommended as a label (a frame id it was offered, `unknown` for anything else, never the critic's own text; null when the critic did not run or its model gave no frame: a passthrough, no API client, a timeout, an error, or an answer that does not parse or names no frame), and `had_error`, true when the turn ended with an error (`cancelled` included). Every text, and every string inside the critic and claim-check JSON, is redacted before it is cut. Turn numbers repeat after compaction, so a trace is ordered by `agent_turn_log.started_at`, then by `(turn_id, call_index)`.

Turn paths that write no turn row (they also skip `post_turn`):

- `run_turn` and `stream_chat`: `pre_turn` raises or is cancelled (before the turn's id exists).
- `run_turn`: the attachment ingest or the execution-ledger setup raises or is cancelled, or recording the attachment fact (after the turn's try) raises or is cancelled.
- `stream_chat`: the attachment ingest, the execution-ledger setup, the system-prompt build, the offered-tool list or the compaction raises or is cancelled (before the turn's try).

A `stream_chat` turn whose consumer closes the generator (`aclose()`) rather than cancelling it writes its row with the error `cancelled`, as a cancelled turn does; `post_turn` is given what it was given before.

### The reason field (`NOUS_TOOL_REASON_ENABLED`)

Every tool offered to the model gets a required string `reason`: "One sentence: why this tool, for this step." It is added to the offered copies of the schemas only (`add_reason_field` in `AgentRunner._offered_tools`), after the F061 extra tools, so the dispatcher's schemas and its per-frame cache never change. A tool whose own schema declares `reason` (`create_censor`) keeps its own and its handler still gets it. For every other tool the runner takes the reason out of the call before the tool runs (`split_reason`); the dispatcher also drops a stray `reason` in `_repair`, so it never reaches a handler, the repaired arguments or the idempotency key. A call without a reason still runs; its row has a null reason, so the rate of missing reasons can be watched. The setting works with or without the log. Turning it on or off changes every tool schema once, so the prompt cache misses once after the flip.

## What PR 2 records (`NOUS_MODEL_CALL_LOG_ENABLED`)

### Model calls (`nous_system.model_call_log` and `nous_system.model_call_attempt`, migration 101)

One row per model call, written when the call ends (`ModelCallRecorder.scope`, `nous/observability/model_calls.py`): every call through either Anthropic client (`call_kind` `call`, `stream` or `aggregated`, from `call`, `stream` and `call_streaming_aggregated`) and every step of a browser task, which posts to the Messages API itself (`browser_step`). A row holds:

- **why:** the `purpose` and where it came from (`label_source`): `explicit` when the caller passed a `CallContext`, `context` when it came from the `model_call_context` fallback set around the call, `none` when neither named one, which is recorded as `unlabelled` and counted;
- **where:** the session and parent session, the turn's id and number, the frame, the subtask, the DAG and the trace id, as far as the caller knows them (the runner passes all of them; a background caller usually names the purpose and at most the session);
- **what:** the model, the status (`ok`, `error` or `cancelled`), the last HTTP status, the error text (redacted, about 1,000 characters) and the stop reason;
- **tokens:** uncached input, cache reads, cache writes, `input_total` (computed by the database) and output;
- **time:** the duration and the number of HTTP attempts, plus `group_id` and `is_fallback` for a call re-sent on a fallback path;
- **context:** for the runner's calls, the context estimate (`section_tokens_est`, the total and the window, utilisation, the sections present, the tools and messages, the memory counts loaded, and the cache break).

Each HTTP try of a call is one `model_call_attempt` row (deleted with its call): its number, start, duration, HTTP status, and error type and text where the client has them. No column holds message text.

### Embedding and rerank minute buckets

Embeddings requests (`EmbeddingProvider`) and cross-encoder reranks (`nous/heart/reranker.py`) are counted per minute, not per call: one `model_call_log` row per minute, purpose, model and status (`call_kind` `embedding` or `rerank`), with `call_count` calls and their durations summed. An embeddings request is one call however many retries it took, and a cache hit is none. An embedding bucket's `input_uncached` (so `input_total`) sums the requests' `usage.total_tokens`; a rerank bucket has no tokens, only its time and count. Sum tokens per purpose or `call_kind`, never across them.

### Purposes

The vocabulary is `PURPOSES` in `nous/observability/model_calls.py`; a name outside it is recorded as `unlabelled` and counted. Today it holds:

- the runner's own calls (`MAIN_LOOP_PURPOSES`): `chat`, `subtask`, `heartbeat`, `background_check`, `continuation`, `dag_summary`, `mcp`, `action_gate`, `reflection`;
- background calls: `compaction`, `critic`, `epistemic_gate`, `query_expansion`, `date_window`, `admission_utility`, `contradiction`, `fact_dedup`, `actionability`, `profile_card`, `episode_summarizer`, `fact_extractor`, `knowledge_extractor`, `enumerative_extractor`, `correction_extractor`, `outcome_detector`, `procedure_learner`, `procedure_coverage`, `strategy_card`, `sleep_reflection`, `sleep_contradiction`, `sleep_merge`, `job_fix`, `a2ui_compose`, `dag_fix`, `pdf_transcription`, `email_classify`, `tool_notes`;
- the calls outside the two clients: `browser_task`, `embedding`, `rerank`;
- `unlabelled`.

### Wiring

`main.py` builds the `ModelCallRecorder` before the first model call and hands it to every hook: both Anthropic clients (`create_client(settings, recorder=...)`: the main client and the heartbeat's), the embedder (`EmbeddingProvider.set_recorder`), the browser task loop (`nous.browser.loop.set_recorder`, which `run_browser_task` hands to each task's caller) and the reranker (`nous.heart.reranker.set_recorder`). The browser loop's and the reranker's hooks are module-level, so they are per process: the last `set_recorder` wins. Shutdown clears them, and the main client's and the embedder's, before the writer drains, so no recorder outlives its writer.

### Measured

Measured in prod over the day from 2026-11-08 23:30 to 2026-11-09 23:30 UTC, with every flag on:

- **Rows:** 4,136 rows for 7,642 calls (the embedding and rerank minute buckets hold 2,588 and 1,337 calls in 342 and 77 rows): 2,627 `aggregated`, 973 `call` and 117 `stream`. That is 165 rows an hour on average, 42 to 343.
- **Main loop:** 2,747 calls, about 19,200 a week against the 14,400 counted before.
- **Labels:** every row names its purpose: `label_source` is `explicit` on all 4,285 rows since recording began, and `unlabelled` is 0.
- **Size:** a row takes about 1 KB; with `model_call_attempt` and the indexes the tables held 7.2 MB after the first 25 hours (about 200 MB at the 30-day retention).
- **Drops:** the recorder counters (`dropped_backpressure` and the rest) reset at each restart, and that day had several deploys. Read once after the last one they showed no drop of any kind, over minutes of uptime, so they are not a daily figure.

### Known gaps

- **Rerank:** no candidate count is recorded; a rerank bucket carries its time and `call_count` only.
- **Browser steps:** a `browser_step` row has no session, turn or trace id; nothing sets the call context around tool dispatch.
- **SDK transport errors:** an SDK attempt that got no HTTP response leaves no attempt row; it appears only in the call's final error.
- **Cut-off streams:** a stream that ends without a terminal event is recorded `error`. A `stream` row's error text is `stream ended without a terminal event`; the httpx client's `aggregated` call raises instead, and its row carries that error: `RuntimeError: Anthropic streaming ended without terminal event (no 'message_delta' or 'message_stop'); response truncated`.
- **Background calls inside a turn:** the critic, the epistemic gate, the date window, query expansion, admission and the like carry a purpose but no turn id; only the monitor and attachment sites pass a session.
- **Out of scope:** model calls made by `scripts/` and `nous_eval/` are not recorded.

## What PR 3 records (`NOUS_MODEL_CALL_TEXT_ENABLED`)

### Model-call text (`nous_system.model_text_block` and `nous_system.model_call_block_ref`, migration 102)

The full text of each recorded model call: what it sent and what it got back (`TextStore`, `nous/observability/model_call_text.py`). A call's system blocks (a text block as its text), its messages (JSON), its tool list (JSON) and its response's content (JSON) are each one block. A stream's response is rebuilt from its content events. Nothing else of the payload is stored: no header, no auth, no other key.

Each block's body:

- has every base64 attachment over 1 KB (an image or document `source.data`, a data URL) replaced by `[base64 N bytes]`;
- is redacted with the ledger's redaction (`redact_output`). In a JSON block every string is redacted first (`redact_structure`), and the serialised text after: in the JSON text a string that holds JSON (a curl body, a tool result) has its quotes escaped and the patterns cannot see its secrets, while a credential-named key of the block itself is masked only in the serialised text. A string over the cap is cut in its middle there (its head and tail kept around a marker);
- is cut at `NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS` (200,000) characters and then ends with `[truncated N chars]`. Over the cap only the head and a scan pad are redacted, and the pad is dropped, so a secret the cut splits is not kept in part.

A block is stored once per agent, under the sha256 of that body (`model_text_block`), so a block that repeats (the system prompt, the tool list, the earlier messages of a session) is stored once. Each call lists its blocks in order (`model_call_block_ref`: the part, `system`, `messages`, `tools` or `response`, then `ord`), and its refs are deleted with its `model_call_log` row. The text is kept 7 days (see Retention).

### How the text is written

- **After the numbers.** The text write is submitted once the call's `model_call_log` row is committed (its refs need that row), as a write of its own through the same `BoundedWriter`, with its own pending cap of 50: a text write past it is dropped and counted, never counts against the other writes' 500, and never evicts a numbers write. A text failure never touches the numbers row.
- **Nothing at the call's exit but copies of containers.** The scope's exit copies the lists, and each message down to its content blocks, never a string: the runner appends to the message list, and compaction rewrites an aged tool result in place right after the call. Serialising, redacting and hashing all run in the write.
- **One transaction per call.** The write upserts the call's distinct blocks in hash order, then inserts its refs. A block already stored is touched (`created_at = now()`), so `created_at` means "last used".
- **Redaction is the cost.** It takes about 0.35 ms per KB. A block over 20,000 characters is redacted in a thread, but CPython's regex engine holds the GIL, so that thread shares the CPU with the event loop rather than freeing it. What keeps the steady state cheap is an LRU of the recently prepared blocks (at most 1,024 blocks and 4,000,000 characters): a block sent again is not redacted again, so a call redacts only its new blocks. A miss parses a JSON block back and redacts it twice (its strings, then its text), about twice the work of a single pass. Measured before PR 3 on a 374 KB context (40 messages, 44 blocks): 1 to 5 ms of loop time per call with a warm LRU, about 140 ms cold (process start) in stalls of at most one block each.

### Wiring

`main.py` builds the `TextStore` on the shared writer and hands it to the `ModelCallRecorder` only when both `NOUS_MODEL_CALL_LOG_ENABLED` and `NOUS_MODEL_CALL_TEXT_ENABLED` are on. With the text on and the numbers off, it builds no store, logs one WARNING at startup and records no text.

### Measured

Measured in prod after deploy, with the text on, from the block and ref tables. Over a window (an hour, then a day):

- **Bytes per call, with dedup:** the growth of `pg_total_relation_size` of `model_text_block` and `model_call_block_ref` (heap, TOAST and indexes) over the window, divided by the calls whose refs were written in it (`count(DISTINCT call_id)` of the refs created in the window). Only a block not stored before adds to the size, so the figure includes the dedup.
- **GB per week:** the bytes per call times the calls per week (about 14,400 main-loop calls, plus the background calls the numbers table counts). The sweep keeps 7 days of it.

Measured over the day from 2026-11-08 23:30 to 2026-11-09 23:30 UTC: 3,716 calls stored their text through 48,129 block references, and only 9,037 blocks were new (28.9 million characters, 19.1 MB stored after compression), so about 81% of the references reused a block already stored. The rows alone come to about 7 KB a call (19.1 MB of blocks and 6.9 MB of references). With the indexes, the two tables held 36.4 MB for the 3,801 calls stored in the first 25 hours, about 9.6 KB a call. At about 26,000 calls a week that is about 250 MB for the 7 days the sweep keeps: an upper bound, since a block reused from an earlier day is not stored again.

### Known limits

- **What is stored is the content sent, not its exact bytes.** `cache_control` is left out of every block, a user message sent as one text block is stored as its string, and JSON keys are sorted, so that a block hashes the same from call to call (the runner moves the cache marker and sends the same message in both shapes). Where the cache breakpoints were is not stored: the numbers row (cache reads and writes, the cache break) and the runner's fixed breakpoint placement answer that. If a view ever needs it, a per-ref flag would keep the dedup.
- **A rebuilt stream** follows `_aggregate`'s rules in code of its own. Its thinking blocks have no `signature`. A stream retried in the middle of its body, after some events were yielded, mixes both attempts' content, as the runner sees it.
- **Counters.** The store counts its own writes (`TextStore.stats`), shown under `recording.model_call_text`. A text write lost because its call's numbers write was dropped (backpressure, timeout or error) is counted only in the numbers counters (`recording.model_calls`): the text could not have been stored without its row.
- **Shared slots.** Text writes share the writer's 4 concurrent writes, so under heavy text load a numbers write can wait behind the text writes queued before it (at most 50; about 2 s in the worst cold case). It is never dropped because of text.
- **Every call sends every distinct block's body to Postgres,** because the upsert touches each block: about the size of the call's context per call, over the local socket. A follow-up could touch the stored blocks first and send only the missing bodies.
- **The redaction thread does not free the loop** (above); a process pool or a cheaper redactor would.
- **A change deeper than a content block** made to a message after the call would reach the stored text. Nothing makes one today.

## What PR 4 changes: one recorder, the context endpoints read it

PR 4 is the last PR of Phase 1. It adds no setting: `NOUS_MODEL_CALL_LOG_ENABLED` is the cutover.

### With `NOUS_MODEL_CALL_LOG_ENABLED` on

- **One recorder.** `model_call_log` is the one record of the main loop's model calls. The F035.4 `ContextLogger` (`NOUS_CONTEXT_LOG_ENABLED`) keeps its in-memory ring but writes no `context_log` row and no response update (`_build_context_logger` in `main.py` gives it no writer). The `context_log` retention sweep keeps running, so the old rows age out in their 30 days.
- **The endpoints read the table.** `/context/log`, `/context/log/{id}`, `/context/log/{id}/payload`, `/context/log/{id}/sections`, `/context/diff`, `/dashboard/cache` and `/dashboard/observability`'s `context_log` read `model_call_log` through a `TableContextView` (`nous/observability/context_view.py`). They show the runner's own calls only (`MAIN_LOOP_PURPOSES`), never a background helper's. The lists asked for without a session id leave out the heartbeat's and the background checks' calls (`QUIET_PURPOSES`), as they left out the ring's `heartbeat` calls. The views survive a restart, list failed calls, and show the duration, trace id, gate and reflection calls that the ring lacked.
- **Same shapes, a few keys more.** Every key keeps its name and meaning. `id` is the row's UUID, `call_type` carries the purpose, and `input_tokens_actual` stays the uncached input. Each entry adds `input_total_tokens`, `status`, `purpose`, `attempts` and `turn_id`. `/dashboard/cache` counts failed calls in `total_calls` and adds `error_calls` and `input_total`. No field the dashboard reads changes type, so the dashboard is unchanged (see the [REST API](../reference/rest-api.md)).
- **Payload and section text.** A call's payload and section text come from the stored text when `NOUS_MODEL_CALL_TEXT_ENABLED` stored it: redacted, the payload with its `response`, and the section text parsed from the stored system blocks as the runner parsed the prompt. Otherwise they come from the ring, as before: the full payload when `NOUS_CONTEXT_LOG_FULL_PAYLOAD` captured it, and the section text. The runner gives a recorded call's ring entry the row's id (`CallContext.call_id`), so the ring is found by it. Without either, the payload is 404 `Not captured`, as before.

### With it off

Every endpoint reads the ring through a `RingContextView`, exactly as before PR 4: same keys, same values, `context_log` rows written as before. Nothing reads the new tables, and a ring entry's id is its own 16 characters.

### The recording bugs

The design's 15 recording bugs are fixed by the new recorder, not patched in the old one. `tests/test_recording_bug_regressions.py` pins each one, `test_bug1_*` to `test_bug15_*`. Each drives a real runner, the critic or a background helper over a real Anthropic client whose HTTP goes to a stubbed API. The rows are read back from Postgres and, where the bug was about what the dashboard showed, through the endpoints.

### Rollout order

Each setting needs its compose pass-through line and its `.env` line (see Turning it on). Turn them on one at a time, in this order:

1. `NOUS_TOOL_CALL_LOG_ENABLED`: tool-call and turn rows.
2. `NOUS_TOOL_REASON_ENABLED`: the `reason` field on every offered tool.
3. `NOUS_MODEL_CALL_LOG_ENABLED`: the model-call rows, and the cutover. The context endpoints read the table from here on, and `context_log` gets no new rows.
4. `NOUS_MODEL_CALL_TEXT_ENABLED`: the full text, which also serves the context endpoints' payload and section text.

### Rollback

Drop the setting's `.env` line and restart. For `NOUS_MODEL_CALL_LOG_ENABLED`, that brings back the ring path and the `context_log` writer, with no data step. The `context_log` sweep never stopped. The `model_call_log` rows are kept, unswept until the setting is on again. The text needs the numbers, so it stops with them.

### Known limits

- **The ring fallback is the ring's.** It holds at most 10 payloads per session and 50 in all, in memory: with the text off, a call from before a restart has no payload or section text, as before.
- **Ties.** Calls are listed newest first by start time, then by id, so two calls that start within one tick of the clock are listed in either order.
- **Stored section text is redacted** and a block over the cap is cut with a marker, so a section's text can be shorter than its estimate. `/context/log/{id}/sections` loads the call's whole stored text to read its system blocks.
- **The dashboard is not changed in Phase 1.** Its Observability view showed the uncached input (`input_tokens_actual`) beside the estimate. Phase 2 moved the per-call numbers to the Model calls view, which shows the uncached input, the cache reads and writes, and their total.

## How the rows are written

- **One bounded writer.** `main.py` builds one `BoundedWriter` (`nous/observability/recording.py`) when `NOUS_TOOL_CALL_LOG_ENABLED` or `NOUS_MODEL_CALL_LOG_ENABLED` is on, never two, and hands it to the `ToolCallRecorder` and the `ModelCallRecorder`. A write is a factory scheduled fire-and-forget: at most 4 writes hold a pool connection at once, each is cut off after 30 s, and past 500 pending writes a new one is dropped (the model-call text has a pending cap of its own, 50). `record`, `record_turn`, a scope's exit and `record_bucket` are synchronous and never raise, so recording never fails or slows a turn or a model call.
- **Counted, never silent.** Every outcome is counted (`written`, `dropped_timeout`, `dropped_error`, `dropped_backpressure`) and every failure is logged at WARNING. `GET /dashboard/observability` returns each recorder's counters, `recording.tool_calls` and `recording.model_calls` (each null while its log is off), and the text store's, `recording.model_call_text` (null while no store is built: the text or the numbers off). The model-call recorder also counts `unlabelled` calls and `dropped_late` reports (an HTTP attempt or a stream event reported after its call's row was written).
- **Shutdown.** `shutdown_components` clears the model-call hooks, then waits up to 5 s for the pending writes immediately before `database.disconnect()`, after every producer of turns has stopped.
- **Off means nothing.** With both logs off, no writer or recorder is built, the clients and hooks hold no recorder, and nothing is recorded; no sweep runs either, unless this agent still has model-call text (see Retention). Each log is off on its own: with only the model-call log on, the runner records no tool calls, and the other way round. With the text off, no text store is built and a scope keeps nothing for it, but text stored before is still swept. The runner computes a call's context estimate once, and only when the old `/context/log*` ring or a recording client takes it.
- **The estimate changed for every reader.** Even with every flag off, the old `/context/log*` ring, the dashboard utilisation and the Telegram `/context` estimates rise after PR 2: the preamble, tool results and the sections formerly folded into their neighbour now count. This is telemetry only, not a regression.

## Retention

Tool-call and turn rows are kept `NOUS_TOOL_CALL_RETENTION_DAYS` (30) days. A loop (`_work_view_retention_loop` in `main.py`, started with either log, or while model-call text is left) deletes this agent's `tool_call_log` and `agent_turn_log` rows that started earlier, in one transaction (`sweep_tool_calls`, `nous/observability/work_view_retention.py`). It is agent-scoped, so one agent never deletes the rows of another that keeps them longer, and both tables index `(agent_id, started_at)` for it. The first pass runs at startup and the next ones once a day, as for the retrieval-log sweep, so a process restarted more often than once a day still sweeps. A failed sweep logs a WARNING and is tried again a day later. A retention of 0 or less means no sweep of that log.

Model-call rows (calls and minute buckets) are kept `NOUS_MODEL_CALL_RETENTION_DAYS` (30) days. While the model-call log is on, the same loop deletes this agent's older `model_call_log` rows (`sweep_model_calls`, agent-scoped, on `(agent_id, started_at)`); their attempts go with them (`ON DELETE CASCADE`).

Model-call text is kept `NOUS_MODEL_CALL_TEXT_RETENTION_DAYS` (7) days, whatever `NOUS_MODEL_CALL_TEXT_ENABLED` says, so text stored before the text was turned off is never kept past its retention. Each pass of the same loop deletes this agent's refs older than that, then its blocks that no call references and no store has used for an hour, in one transaction (`sweep_model_call_text`, agent-scoped). The loop runs while either log is swept or the text is being recorded (both text settings on, even on an empty database and with the numbers kept 0 days); when nothing can write new text, startup runs one EXISTS query (`has_model_call_text`) and starts the loop only if this agent still has text (a failed check starts it too). A store touches every block it sends, so a block in use is never old. The sweep locks the blocks it deletes in hash order, the order a store upserts them in, so a sweep and a store never deadlock, and a block a store touches while the sweep waits for it is kept: its new ref never meets a deleted block. The numbers rows are untouched and keep their own 30 days; a call's refs go with its row, so the text is never kept longer than its numbers.

Each sweep runs in a try of its own, so one failing sweep never skips another.

The full result of a call (`tool_call_log.result_text`, migration 104) goes by the model-call text retention, `NOUS_MODEL_CALL_TEXT_RETENTION_DAYS` (7), not by the 30 days of the row: the same loop, in the tool-call branch, sets it to NULL on this agent's rows older than that (`sweep_tool_call_text`, an UPDATE), and the row and its 500-character preview stay. It is stored only when the preview was cut, as the tool's output before SmartCompress, redacted and cut at 20,000 characters (`RESULT_TEXT_MAX_CHARS`), and never for an error call. The reader never reads it (`db_roles.sql` excludes the column), and the trace marks a call that has one with `result_full`; `GET /dashboard/work/calls/{call_id}/result` returns it for one call. A limit: text written while the tool-call log was on and left behind when it was turned off is cleared only when the log is on again and the loop runs, or by the 30-day row delete; the other work-view rows behave the same.

## Who reads the rows

The dashboards, and Nous itself: the `db_query` reader (`nous_reader`, `scripts/ops/db_roles.sql` section 5d) may read `tool_call_log`, `model_call_log` and `model_call_attempt`, so Nous can look up its own tool calls, their reasons and its own model calls, and (F110) the numbers, ids and labels of `agent_turn_log`: per turn the frame, why it was chosen (`frame_reason`), the frame the critic recommended (`critic_frame`), whether the turn failed (`had_error`), the turn number, the work kind and the tool-call count. It never reads a turn's text (`trigger_text`, `reply_text`, `error`, and the `critic` and `claim_check` records), which `db_roles.sql` excludes column by column, nor the model-call text (`model_text_block` and `model_call_block_ref`, the full prompts and responses), which it does not list. The grants name columns, so after migration 106 `db_roles.sql` must run again (as `nous_admin`, then `verify_db_roles.sql`) before the reader sees `frame_reason`, `critic_frame` and `had_error`. CLAUDE.md states the rule: dashboards read the observability tables in full, the agent only what section 5d lists. The tool-notes review (Phase 3) reads `tool_call_log` in the app, and `tool_note` is read and written by the app only: `db_roles.sql` lists it nowhere.

The dashboard reads both text tables (`model_text_block` and `model_call_block_ref`) through the app role, never the reader: the model-call detail lists a call's blocks without their bodies (`GET /dashboard/model-calls/{call_id}`), and one block's body is read alone, on request (`GET /dashboard/model-calls/{call_id}/blocks/{part}/{ord}`).

## Phase 2: the views

Phase 2 reads the Phase 1 rows on the dashboard (`/dashboard/v2/`). It adds no table, no setting and no write route.

### The screens

- **Work** (`#/work`) has three tabs. **Sessions** lists conversations and tasks, latest activity first, so a chat that goes on in an older session (Telegram keeps one) moves to the top and shows its latest message under the first; background work is hidden unless asked for. It filters by kind, tool, text (the title or any turn's message) and five lenses: a tool failed, the same call repeated, 10 or more calls in a turn, Nous asked you a question, and changed something. A link's `days` sets the window (1 to 30, default 7). **A session's trace** (`#/work/s/<session_id>`, or `.../<turn_id>` for one turn) shows each turn: what started it, the memory in its context, the critic's verdict, the claim check, its main-loop model calls, and each tool call with its reason first. Every call the ledger recorded shows its ledger status; a held send (status `unknown` with `held` true) also gets the undo help (copy the guarded SQL). **Patterns** (`#/work/patterns`) shows the tools by kind of work with their common reasons, the tools that fail most (each links to its failing sessions), and the wasted steps (repeated calls, and retries that failed again). **Changes** (`#/work/changes`) is the execution ledger, shown under Work's title.
- **Model calls** (`#/model-calls`) has two tabs. **Calls** lists every model call with its purpose, tokens, cache reads, duration and any problem, filtered by purpose, model, session, turn and status (`problem` = failed, sent more than once, sent on a fallback path, or no purpose recorded). **A call** (`#/model-calls/c/<call_id>`) shows its attempts, its context sections by size, a link to the turn that made it, and its stored text by block, each with how many calls sent the same block; a block's body is loaded only when it is opened. **Cache** (`#/model-calls/cache`) is the old Cache screen; a timeline row links to its call when the model-call log is on.
- **Memory health** (`#/health`) has three tabs: **Graph** (the graph health charts, with the old Density screen's orphan rate by type, edge distribution and backfill progress), **Writes** (memory write health, then admission) and **Growth** (memory growth, fact categories and the fact growth rate, which Overview sums up in one line).
- The old Activity screen's sections moved to the screens they belong to: the Event Timeline to Observability, the Upcoming Schedules to Subtasks, the Sleep Activity to Consolidation, and the Most Active Censors to Guards. Their numbers keep Activity's fixed windows (7 days; the top censors' counts are all-time), which a caption says, since Guards and Subtasks have a window selector of their own. Observability's per-call sections are gone; their numbers are on Model calls.

Every text is shown as stored (redacted when written), as plain text: never as HTML, never as rendered markdown.

### Endpoints

All read-only `GET`s, agent-scoped, documented in the [REST API](../reference/rest-api.md): `/dashboard/work/sessions`, `/dashboard/work/sessions/{session_id}`, `/dashboard/work/patterns`, `/dashboard/model-calls`, `/dashboard/model-calls/{call_id}` and `/dashboard/model-calls/{call_id}/blocks/{part}/{ord}`. Lists page with an opaque keyset cursor. A trace orders turns by their start, then id, never by turn number, since turn numbers repeat after compaction.

### With a setting off

Each endpoint group reads its tables only while its Phase 1 setting is on. Off, it answers 200 with `enabled` false, every list empty and every count zero, and opens no database session. The screens then say so and name the setting: Work, "Tool-call recording is off. Set NOUS_TOOL_CALL_LOG_ENABLED to record what Nous does."; Model calls, "Model-call recording is off. Set NOUS_MODEL_CALL_LOG_ENABLED."; a call's text, "Prompt text is not recorded. Set NOUS_MODEL_CALL_TEXT_ENABLED." A call older than the text's retention says its text is no longer kept.

### Navigation and redirects

The navigation has 18 items in six groups: Overview; **Memory**: Graph, Browse, Health, Retrieval, Consolidation; **Execution**: Work, Model calls, Workflows, Subtasks, Heartbeat; **Decisions**: Decisions, Scoring; **Autonomy**: Autonomy, Intentions, Guards; **System**: Observability, Identity. Workflows, Scoring and Guards keep their route ids (`dag`, `rubric`, `harness`). The badge for sends in doubt is on Work (the sends are on its Changes tab); the badge for questions waiting stays on Workflows.

Six routes are retired. An old link or bookmark lands on the screen's new home: the router replaces the hash (no new history entry), so the address bar shows the new one. The old screens had no sub paths or queries, so an old hash's sub path and query are dropped.

| Old | New |
|---|---|
| `#/activity` | `#/observability` |
| `#/admission` | `#/health/writes` |
| `#/memory-write-health` | `#/health/writes` |
| `#/density` | `#/health/graph` |
| `#/cache` | `#/model-calls/cache` |
| `#/execution` | `#/work/changes` |

### What Phase 2 leaves out

- **Patterns** shows tools by kind of work, their common reasons, the tools that fail most and the wasted steps. It does not group reasons by goal, show what Nous did after a failure, or show which memory a call was given: each needs reasons clustered or labelled first, so they move to Phase 3 (tool notes).
- **"Nous asked you a question"** finds only `resolve_intention` calls that decided `ask`; a workflow waiting on an answer is not counted.
- **A trace's model calls** are the main loop's calls of that turn. A background helper inside a turn (`critic`, `epistemic_gate`, `query_expansion`, `date_window`, `admission_utility`) records a session but no turn, so the trace also links the session's whole set of model calls.
- **A session with calls but no turn row** (for example one that starts with an owner-approved call) is not listed; its trace still opens by id. Every unlabelled Claude Code conversation shares the session id `mcp-session`, so it is one long row, paged in its trace.

## Phase 3: tool notes (`NOUS_TOOL_NOTES_ENABLED`, migration 103)

Phase 3 turns the tool mistakes that repeat in `tool_call_log` into short notes the agent can read. It ships off. It adds one table (`nous_system.tool_note`, migration 103), one setting, one sleep step, one procedure and one quiet owner card, and no agent tool.

### Patterns

`detect_patterns` (`nous/observability/tool_patterns.py`) finds repeated mistakes in code, not with a model. There are three kinds, each per tool, and each has a rate: its hits divided by that tool's calls over the same window.

| Kind | A hit is |
|---|---|
| `error_repeat` | an error row whose normalised error text equals the pattern's |
| `call_repeat` | a call with a previous identical call (same turn, tool and input) |
| `retry_failed` | an error after an identical error (same turn, tool and input) |

`call_repeat` and `retry_failed` use the same definition as the Work view's Patterns tab: the constants `REPEAT_KEY`, `PREV_ERROR_EXPR`, `REPEATED_SQL` and `RETRY_FAILED_SQL` are shared, and a test pins the two to the same counts. Turnless calls have no turn to repeat in. `normalise_error` lowercases the text and replaces ids, times, absolute paths, hex runs, numbers and quoted strings of 3 or more characters, collapses whitespace and cuts to 160 characters; an error with no text becomes `(no error text)` and is never offered.

A pattern needs at least `MIN_HITS` (3) hits in the last `WINDOW_DAYS` (14) days. A tool with `ERROR_ROWS_CAP` (5,000) or more error rows in the window is skipped for `error_repeat` (counted as `capped`), since a bounded count never becomes a rate. A pattern's id is `kind:tool:` plus a short hash of its key. Its example (at most 200 characters) is redacted.

A pattern is not offered to the review again while it has an active note or any `tool_note` row (a note, a declined row or a guard row) written or retired in the last `REREVIEW_DAYS` (14) days.

### The review

Once per sleep, after the job fixes, `ToolNotesReview` (`nous/handlers/tool_notes.py`) first judges the existing notes (see Keep or retire), then offers at most `MAX_CANDIDATES` (8) patterns, most hits first, to one model call: purpose `tool_notes`, model `NOUS_BACKGROUND_MODEL`, 1,500 output tokens. The prompt holds tool-call records (the tool, the kind, hits and calls, the redacted example, and the tool's existing notes), redacted before it is formatted, plus how each offered tool is called: its parameters, then its description, as the agent sees them, one line per tool, at most `TOOL_SPEC_MAX_CHARS` (1,200) characters (each parameter's description at most 240), from the dispatcher's definitions. The model is told a note must agree with them, so an error such as "missing procedure_id, provided: name" is read against the parameter it names (the first prod review, before this, told the agent to look up a procedure's id although `procedure_id` takes the name). It never mentions scores, outcome labels or the rubric: an example that holds such a word is withheld, the word is masked in a tool's description, and a pattern of a tool whose name holds one is left out (counted as rejected). The model is told to write nothing for a pattern it cannot explain from the records.

The answer is checked strictly in code:

- It must be an object with a `notes` list. Anything else counts as `llm_failed`, closes nothing, and the same patterns are offered again next sleep.
- Each entry names an offered pattern once, with `write` or `skip`. The text is redacted, at most `NOTE_MAX_CHARS` (300) characters, and free of score words, of guard wording (the `GUARD_FRAGMENTS` as whole words, except "may not" and "may only", which are everyday words in a benign note) and of advice on getting around a refusal (`WORKAROUND_WORDS`). An entry that fails is dropped and counted as rejected.
- A tool holds at most `NOTES_PER_TOOL` (3) notes and the agent `NOTES_TOTAL` (40). At a cap the entry must name the note it replaces (`replaces`; another tool's note only while the total cap binds and the entry's tool has room): the review rewrites or drops, never appends. The store checks the caps again in the transaction that writes.
- An offered pattern that gets no stored note is closed with a `declined` row, so it is not offered again for 14 days.

A stored note keeps the baseline of its pattern: the rate, hits and calls at the time it was written.

### Guard patterns and the owner card

`guard_cause` labels a call by a runner refusal code (`GUARD_CODES`: the ledger's refusal codes except `duplicate` and `offered_set`, which are model mistakes), by an unlabelled `refused` or `gated` outcome, or by guard wording in an error (`GUARD_FRAGMENTS`). Transient network failures such as "connection refused" are blanked first and are not guards; the ledger's "could not record this send" refusal is one. A pattern is a guard pattern when at least half of its hit rows carry a guard label. It never reaches the model and never becomes a note.

Instead the owner gets one card on the companion (`tool_notes_guard`): the guard patterns of the window, unreported ones first and then reported ones to fill the slots, at most `MAX_PER_CARD` (10), each with its tool, the guard, its hits and calls and the redacted error. It has no button and no advice, is pushed with `notify` and `telegram_ping` off, replaces itself in place and expires in 14 days. It is pushed only when a guard pattern has no `guard` row of the last 14 days, so each pattern is reported at most once per 14 days; after a successful push the newly listed patterns get a `guard` row. A failed push writes nothing and logs one WARNING (tool names, never record text), so the next sleep tries again.

### The procedure

The notes reach the agent through one procedure named "Tool notes" (domain `tools`, tag `tool_notes`, a fixed description), listed in the procedure catalog like any other active procedure. Its body is rendered from the active notes by `get_procedure` (one `tool: note` line per note) and never stored. The procedure is active only while both `NOUS_TOOL_NOTES_ENABLED` and `NOUS_TOOL_CALL_LOG_ENABLED` are on and at least one note exists; with no note it is inactive, which costs no context at all. It is brought in line after every enabled sleep and at startup (so turning the flag off retires it at the next start), and `get_procedure` hides it while it is inactive or either flag is off. No agent tool is added.

### Keep or retire

Each sleep, before the review, every active note that has not been judged is judged once. When its tool has at least `JUDGE_AFTER_CALLS` (20) calls since the note was written and the note is at least `JUDGE_MIN_DAYS` (7) days old, the rate of the note's own pattern since then is compared with its baseline: strictly lower keeps the note (recorded as judged), otherwise it is retired as `no_improvement`. A note not judged after more than `STALE_DAYS` (30) days is retired as `unused`, including one whose `error_repeat` count is bounded by the 5,000-row cap and so can never be decided. Retired notes are soft deleted (`active` false) and keep the reason (a `no_improvement` note also keeps the calls, hits and rate it was judged on; an `unused` one has no numbers). The thresholds differ from the job lessons' 5 runs because a tool is called many times a day, in bursts: a rate over a handful of calls says nothing, while a job lesson is judged on whole runs of one job.

### Sleep counts

The step's counts are one nested dict, `sleep_stats["tool_notes"]`: `detected`, `capped`, `guard`, `offered`, `written`, `rewritten`, `declined`, `rejected`, `at_cap`, `llm_failed`, `retired`, `kept`. It adds no top-level integer, so the consolidation audit is unchanged.

### Rollout

The step needs sleep and the tool-call log; with the log off, `main.py` logs one WARNING and the step finds nothing. The flag is off by default. The owner turned it on in prod on 2026-11-09, a day after recording began, with `NOUS_TOOL_CALL_LOG_ENABLED` on. The first review found 2 patterns and wrote 2 notes; one misread an argument error, was corrected by hand, and led to the review seeing each tool's parameters (above). To turn it on: add `- NOUS_TOOL_NOTES_ENABLED=${NOUS_TOOL_NOTES_ENABLED:-false}` to the `nous` service in `docker-compose.yml` (a code-owner path, so the PR does not change it) and the `.env` line. Migration 103 is applied by the application at start and needs no `db_roles.sql` run: the reader reads neither the notes nor the guard rows. Rollback is removing the `.env` line; the next startup retires the procedure, and the stored notes stay as rows.

## Non-goals

- The MCP server's own tools (`nous_recall`, `nous_status`, `nous_teach` in `nous/api/mcp.py`) are called directly by external agents. Those calls are not Nous's own work and are not recorded, and the MCP server's tool list does not get `reason`. `nous_chat` and `nous_decide` start a turn of Nous's own (context kind `mcp`), which is recorded like any other turn.
- Replacing the execution ledger. It keeps its safety and undo role; these rows are for watching.

## Expected size

Measured inputs (production, 2026-11-07): since 2026-10-27, 161 conversation turns, 1,235 task turns and 4,109 background turns used tools (nearly all background turns were one bash check each), and the main loop makes about 14,400 model calls a week (10,800 background checks, 2,950 subtask calls, 500 chat calls).

| | Per day | Basis |
|---|---|---|
| Turn rows | 500 to 2,100 (about 1,300) | at least the 5,505 tool-using turns over 11 days (about 500 a day; the design counted about 6,000 a week); at most one turn per main-loop model call (about 2,060 a day). The background checks that use no tool are one model call and one turn each, about 800 a day |
| Tool-call rows | 750 to 4,000 (about 1,000) | background turns one call each (about 375); task turns average 3.8 model calls, so at least 2.8 calls each (about 315); conversation turns average 4.8 model calls, so at least 3.8 calls each (about 55). The upper end allows two calls per main-loop model call |

Bytes per row are bounded by the caps. A tool-call row is about 250 bytes of fixed columns plus at most 2,016 characters of input, 516 of result preview, 500 of reason and 1,016 of error text (errors only): at most about 4.3 KB, typically 1 KB (a bash check's input is a few hundred characters). A turn row is about 200 bytes plus at most 516 characters of trigger, 1,016 of reply and 1,016 of error, the memory ids (at most 4 x 50 ids, about 8 KB at the cap, typically under 1 KB) and the critic and claim-check JSON (each string at most 1,000 characters): typically 2 to 3 KB. PostgreSQL compresses a row over about 2 KB, so the largest rows take less than this.

Over 30 days that is 22,500 to 120,000 tool-call rows (about 30 MB typical, 520 MB if every row hit every cap) and 15,000 to 63,000 turn rows (about 100 MB typical), plus their indexes: typically about 100 to 300 MB in all at steady state. The capped worst case, every row at every cap at the top of both ranges, is about 1.3 GB: 120,000 tool-call rows at 4.3 KB (520 MB) and 63,000 turn rows at about 12 KB (750 MB; the claim-check JSON has no cap on its number of entries, so a turn with many claims can exceed even this), before compression. For comparison, `context_log` holds about 1.6 KB a row (44,226 rows, 72 MB, since 2026-10-04).

The daily sweep deletes one day's rows, about 1,250 to 6,100. Deletes of that size need only autovacuum: the dead rows are reclaimed and their space reused by the next days' inserts, so the tables stay near their 30-day size. No partitioning is needed.

Measured in prod over the day from 2026-11-08 23:30 to 2026-11-09 23:30 UTC:

- **Turn rows: 487**, at the low end of the range. 484 used a tool: 372 background checks, 66 subtasks, 25 workflow steps, 17 conversation turns, 7 heartbeats and 2 intentions.
- **Tool-call rows: 2,632**, above the typical 1,000. A background check made 4.0 calls, not the one assumed; a workflow step made 22.7, a subtask 6.5 and a conversation turn 7.4. 34 calls were errors and 337 kept a full result.
- **Row size:** a tool-call row takes about 1.2 KB and a turn row about 2.5 KB (at most 3.8 KB).
- **Tables:** after the first 25 hours `tool_call_log` held 4.8 MB and `agent_turn_log` 2.2 MB with their indexes. At this rate that is about 210 MB at 30 days, inside the typical range above.

The model-call text (PR 3) is not in these figures: its size, with the dedup, is measured in prod after deploy (see What PR 3 records, Measured).

## Turning it on

`docker-compose.yml` passes `NOUS_*` variables one by one and is a code-owner path, so this PR does not change it. To turn recording on, the owner adds pass-through lines for the settings to the `nous` service (`- NOUS_TOOL_CALL_LOG_ENABLED=${NOUS_TOOL_CALL_LOG_ENABLED:-false}`, `- NOUS_TOOL_REASON_ENABLED=${NOUS_TOOL_REASON_ENABLED:-false}`, and any cap or retention that should differ from its default) and sets the values in the host `.env`; a value only in `.env` never reaches the container. Migration 100 is applied by the application at start, before `scripts/ops/db_roles.sql` is run again to grant the reader `tool_call_log` (see [database roles](../reference/database-roles.md)). The model-call numbers need `- NOUS_MODEL_CALL_LOG_ENABLED=${NOUS_MODEL_CALL_LOG_ENABLED:-false}` (and `NOUS_MODEL_CALL_RETENTION_DAYS` if it should differ from 30) the same way; migration 101 is applied at start, before `db_roles.sql` is run again to grant the reader `model_call_log` and `model_call_attempt`. The text needs `- NOUS_MODEL_CALL_TEXT_ENABLED=${NOUS_MODEL_CALL_TEXT_ENABLED:-false}` as well as the numbers line (and `NOUS_MODEL_CALL_TEXT_RETENTION_DAYS` or `NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS` if they should differ from 7 and 200,000); migration 102 is applied at start and needs no `db_roles.sql` run, since the reader reads neither text table.

## Reference

- Settings: [environment variables](../reference/environment-variables.md) (`NOUS_TOOL_CALL_LOG_ENABLED`, `NOUS_TOOL_REASON_ENABLED`, `NOUS_TOOL_CALL_RETENTION_DAYS`, `NOUS_TOOL_INPUT_MAX_CHARS`, `NOUS_TOOL_RESULT_PREVIEW_CHARS`, `NOUS_TOOL_REASON_MAX_CHARS`, `NOUS_MODEL_CALL_LOG_ENABLED`, `NOUS_MODEL_CALL_RETENTION_DAYS`, `NOUS_MODEL_CALL_TEXT_ENABLED`, `NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS`, `NOUS_MODEL_CALL_TEXT_RETENTION_DAYS`; the cutover's effect on `NOUS_CONTEXT_LOG_ENABLED`).
- Endpoints: [REST API](../reference/rest-api.md) (`GET /dashboard/observability`, `recording` and `context_log`; `/dashboard/cache`; `/context/log*`; `/context/diff`; Phase 2: `/dashboard/work/*` and `/dashboard/model-calls*`).
- Dashboard (Phase 2): `nous/api/work_view_dashboard.py` (the endpoints); `dashboard-app/src/views/Work.svelte`, `views/work/`, `views/ModelCalls.svelte`, `views/modelcalls/`, `views/Health.svelte` and `views/health/` (the screens); `dashboard-app/src/lib/router.ts` (the routes and `REDIRECTS`) and `lib/ui/Nav.svelte` (the groups).
- Reader: [database roles](../reference/database-roles.md) and [agent tools](../reference/agent-tools.md) (`db_query`).
- Code: `nous/observability/recording.py`, `nous/observability/tool_calls.py`, `nous/observability/model_calls.py`, `nous/observability/model_call_text.py`, `nous/observability/context_view.py` (the views the context endpoints read), `nous/observability/work_view_retention.py`, `nous/api/runner.py` (`_note_tool_call`, `_note_turn`, the call contexts), `nous/api/anthropic_client.py` (`set_recorder`, the scopes and attempts), `nous/browser/loop.py`, `nous/brain/embeddings.py` and `nous/heart/reranker.py` (`set_recorder`), `nous/main.py` (wiring, `_build_context_logger`, retention loop, shutdown).
