# F108: Agent Work View

**Status:** PR 3 in progress (Phase 1, recording). PR 1 (#870, merged) records tool calls and turns; PR 2 (#871, merged) records the model-call numbers; PR 3 records the model-call text. Every setting defaults off: `NOUS_TOOL_CALL_LOG_ENABLED` and `NOUS_TOOL_REASON_ENABLED` (PR 1), `NOUS_MODEL_CALL_LOG_ENABLED` (PR 2), `NOUS_MODEL_CALL_TEXT_ENABLED` (PR 3). Later PRs add the cutover from `context_log`, the views and the tool notes.

## Problem

Nobody can answer "what did Nous do in this conversation, and why" from what is stored. Only side-effecting tool calls are stored (the execution ledger), and only the tool names of each turn (`f026_claim_verification` events). Nous's reasons for its tool calls are never stored, so a session's work cannot be followed call by call, nor across sessions.

## What PR 1 records

### Tool calls (`nous_system.tool_call_log`, migration 100)

One row per tool call the model asked for, written after the call by the runner (`AgentRunner._note_tool_call`, through `ToolCallRecorder.record`, `nous/observability/tool_calls.py`). Calls that never ran are recorded too: `outcome` is one of `ran`, `error`, `refused` (a tool policy refused it), `gated` (the F026 action gate blocked it), `suppressed` (a keyed send whose key was already held), `bad_input` (malformed input JSON) or `retired` (a retired tool name); `refused_by` holds the refusal code, or `action_gate`. There is no CHECK on `outcome`, so a new outcome is recorded, not dropped.

A row holds the agent, session and parent session, the turn's id and number, the call's 0-based place in its turn (`call_index`, counted across the turn's stream segments), the kind of work (`conversation`, `subtask`, `workflow_step`, `intention`, `heartbeat`, `background_check`, from the turn's `ExecutionContext.kind`), the subtask and DAG ids when there are any, the canonical tool name and the name the model used (an alias), the reason, the input, a result preview, the error flag and error text, the duration, the start time and the execution-ledger row of a side-effecting call.

The input, result preview, reason and error text are redacted with the execution ledger's redaction before they are cut (`detail_text`): at most `NOUS_TOOL_INPUT_MAX_CHARS` (2,000), `NOUS_TOOL_RESULT_PREVIEW_CHARS` (500), `NOUS_TOOL_REASON_MAX_CHARS` (500) and 1,000 characters. Over a cap, the head and the tail are kept around a 16-character marker.

`execute_single_call` (one owner-approved call run with no model in between) writes its tool-call row under a turn id of its own, with no turn number, and no turn row.

Two gaps in the tool-call rows:

- **A dispatch exception that escapes the tool loop writes no tool row.** `ToolDispatcher.dispatch` turns a tool's own exception into an error result, so only two kinds escape: a cancellation (or the stream closed) in the middle of the call, and a failure of the ledger plumbing around it. The execution ledger closes a cancelled call `unknown` at once; after a plumbing failure the row stays `pending` until the ledger sweep marks it; `tool_call_log` has no row for either.
- **A call blocked because its compensation snapshot could not be taken is recorded as `error`** (with `refused_by` null and the `ledger_entry_id` of its row), while its execution-ledger row says `blocked`.

### Turns (`nous_system.agent_turn_log`, migration 100)

One row per turn, `id` = the turn's id, so a turn's tool calls join on `tool_call_log.turn_id`. Written when the turn ends, before `post_turn` (`ToolCallRecorder.record_turn`, an upsert on `id`). It holds what started the turn (the message or the background trigger, at most 500 characters), the memory already in the context (`memory_context`: the ids of the recalled memories under the singular keys `decision`, `fact`, `procedure` and `episode`, at most 50 per kind), the critic's verdict for the turn (the `critic_classified` event's data, or null when the critic did not run), the claim check (the `f026_claim_verification` result, or null), the reply (at most 1,000 characters), the error (at most 1,000; `cancelled` for a cancelled turn), the number of tool calls, and the start and end times. Every text, and every string inside the critic and claim-check JSON, is redacted before it is cut. Turn numbers repeat after compaction, so a trace is ordered by `agent_turn_log.started_at`, then by `(turn_id, call_index)`.

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
- background calls: `compaction`, `critic`, `epistemic_gate`, `query_expansion`, `date_window`, `admission_utility`, `contradiction`, `fact_dedup`, `actionability`, `profile_card`, `episode_summarizer`, `fact_extractor`, `knowledge_extractor`, `enumerative_extractor`, `correction_extractor`, `outcome_detector`, `procedure_learner`, `procedure_coverage`, `strategy_card`, `sleep_reflection`, `sleep_contradiction`, `sleep_merge`, `job_fix`, `a2ui_compose`, `dag_fix`, `pdf_transcription`, `email_classify`;
- the calls outside the two clients: `browser_task`, `embedding`, `rerank`;
- `unlabelled`.

### Wiring

`main.py` builds the `ModelCallRecorder` before the first model call and hands it to every hook: both Anthropic clients (`create_client(settings, recorder=...)`: the main client and the heartbeat's), the embedder (`EmbeddingProvider.set_recorder`), the browser task loop (`nous.browser.loop.set_recorder`, which `run_browser_task` hands to each task's caller) and the reranker (`nous.heart.reranker.set_recorder`). The browser loop's and the reranker's hooks are module-level, so they are per process: the last `set_recorder` wins. Shutdown clears them, and the main client's and the embedder's, before the writer drains, so no recorder outlives its writer.

### Measured

Measured in prod after deploy, with the flags turned on: rows per hour, the `unlabelled` count and `dropped_backpressure`. The numbers follow in a later PR.

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
- is redacted with the ledger's redaction (`redact_output`);
- is cut at `NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS` (200,000) characters and then ends with `[truncated N chars]`. Over the cap only the head and a scan pad are redacted, and the pad is dropped, so a secret the cut splits is not kept in part.

A block is stored once per agent, under the sha256 of that body (`model_text_block`), so a block that repeats (the system prompt, the tool list, the earlier messages of a session) is stored once. Each call lists its blocks in order (`model_call_block_ref`: the part, `system`, `messages`, `tools` or `response`, then `ord`), and its refs are deleted with its `model_call_log` row. The text is kept 7 days (see Retention).

### How the text is written

- **After the numbers.** The text write is submitted once the call's `model_call_log` row is committed (its refs need that row), as a write of its own through the same `BoundedWriter`, with its own pending cap of 50: a text write past it is dropped and counted, never counts against the other writes' 500, and never evicts a numbers write. A text failure never touches the numbers row.
- **Nothing at the call's exit but copies of containers.** The scope's exit copies the lists, and each message down to its content blocks, never a string: the runner appends to the message list, and compaction rewrites an aged tool result in place right after the call. Serialising, redacting and hashing all run in the write.
- **One transaction per call.** The write upserts the call's distinct blocks in hash order, then inserts its refs. A block already stored is touched (`created_at = now()`), so `created_at` means "last used".
- **Redaction is the cost.** It takes about 0.35 ms per KB. A block over 20,000 characters is redacted in a thread, but CPython's regex engine holds the GIL, so that thread shares the CPU with the event loop rather than freeing it. What keeps the steady state cheap is an LRU of the recently prepared blocks (at most 1,024 blocks and 4,000,000 characters): a block sent again is not redacted again, so a call redacts only its new blocks. Measured in the Task 14 review on a 374 KB context: 1 to 5 ms of loop time per call with a warm LRU, about 140 ms cold (process start) in stalls of at most one block each.

### Wiring

`main.py` builds the `TextStore` on the shared writer and hands it to the `ModelCallRecorder` only when both `NOUS_MODEL_CALL_LOG_ENABLED` and `NOUS_MODEL_CALL_TEXT_ENABLED` are on. With the text on and the numbers off, it builds no store, logs one WARNING at startup and records no text.

### Measured

Measured in prod after deploy, with the text on, from the block and ref tables. Over a window (an hour, then a day):

- **Bytes per call, with dedup:** the growth of `pg_total_relation_size` of `model_text_block` and `model_call_block_ref` (heap, TOAST and indexes) over the window, divided by the calls whose refs were written in it (`count(DISTINCT call_id)` of the refs created in the window). Only a block not stored before adds to the size, so the figure includes the dedup.
- **GB per week:** the bytes per call times the calls per week (about 14,400 main-loop calls, plus the background calls the numbers table counts). The sweep keeps 7 days of it.

The numbers follow from prod in a later PR.

### Known limits

- **What is stored is the content sent, not its exact bytes.** `cache_control` is left out of every block, a user message sent as one text block is stored as its string, and JSON keys are sorted, so that a block hashes the same from call to call (the runner moves the cache marker and sends the same message in both shapes). Where the cache breakpoints were is not stored: the numbers row (cache reads and writes, the cache break) and the runner's fixed breakpoint placement answer that. If a view ever needs it, a per-ref flag would keep the dedup.
- **A rebuilt stream** follows `_aggregate`'s rules in code of its own. Its thinking blocks have no `signature`. A stream retried in the middle of its body, after some events were yielded, mixes both attempts' content, as the runner sees it.
- **Counters.** The store counts its own writes (`TextStore.stats`), which no endpoint shows yet. A text write lost because its call's numbers write was dropped (backpressure, timeout or error) is counted only in the numbers counters (`recording.model_calls`): the text could not have been stored without its row.
- **Shared slots.** Text writes share the writer's 4 concurrent writes, so under heavy text load a numbers write can wait behind the text writes queued before it (at most 50; about 2 s in the worst cold case). It is never dropped because of text.
- **Every call sends every distinct block's body to Postgres,** because the upsert touches each block: about the size of the call's context per call, over the local socket. A follow-up could touch the stored blocks first and send only the missing bodies.
- **The redaction thread does not free the loop** (above); a process pool or a cheaper redactor would.
- **A change deeper than a content block** made to a message after the call would reach the stored text. Nothing makes one today.

## How the rows are written

- **One bounded writer.** `main.py` builds one `BoundedWriter` (`nous/observability/recording.py`) when `NOUS_TOOL_CALL_LOG_ENABLED` or `NOUS_MODEL_CALL_LOG_ENABLED` is on, never two, and hands it to the `ToolCallRecorder` and the `ModelCallRecorder`. A write is a factory scheduled fire-and-forget: at most 4 writes hold a pool connection at once, each is cut off after 30 s, and past 500 pending writes a new one is dropped (the model-call text has a pending cap of its own, 50). `record`, `record_turn`, a scope's exit and `record_bucket` are synchronous and never raise, so recording never fails or slows a turn or a model call.
- **Counted, never silent.** Every outcome is counted (`written`, `dropped_timeout`, `dropped_error`, `dropped_backpressure`) and every failure is logged at WARNING. `GET /dashboard/observability` returns each recorder's counters, `recording.tool_calls` and `recording.model_calls` (each null while its log is off). The model-call recorder also counts `unlabelled` calls and `dropped_late` reports (an HTTP attempt or a stream event reported after its call's row was written).
- **Shutdown.** `shutdown_components` clears the model-call hooks, then waits up to 5 s for the pending writes immediately before `database.disconnect()`, after every producer of turns has stopped.
- **Off means nothing.** With both logs off, no writer, recorder or sweep is built, the clients and hooks hold no recorder, and nothing is recorded. Each log is off on its own: with only the model-call log on, the runner records no tool calls, and the other way round. With the text off, no text store or text sweep is built and a scope keeps nothing for it. The runner computes a call's context estimate once, and only when the old `/context/log*` ring or a recording client takes it.
- **The estimate changed for every reader.** Even with every flag off, the old `/context/log*` ring, the dashboard utilisation and the Telegram `/context` estimates rise after PR 2: the preamble, tool results and the sections formerly folded into their neighbour now count. This is telemetry only, not a regression.

## Retention

Tool-call and turn rows are kept `NOUS_TOOL_CALL_RETENTION_DAYS` (30) days. A loop (`_work_view_retention_loop` in `main.py`, started with either log) deletes this agent's `tool_call_log` and `agent_turn_log` rows that started earlier, in one transaction (`sweep_tool_calls`, `nous/observability/work_view_retention.py`). It is agent-scoped, so one agent never deletes the rows of another that keeps them longer, and both tables index `(agent_id, started_at)` for it. The first pass runs at startup and the next ones once a day, as for the retrieval-log sweep, so a process restarted more often than once a day still sweeps. A failed sweep logs a WARNING and is tried again a day later. A retention of 0 or less means no sweep of that log.

Model-call rows (calls and minute buckets) are kept `NOUS_MODEL_CALL_RETENTION_DAYS` (30) days. While the model-call log is on, the same loop deletes this agent's older `model_call_log` rows (`sweep_model_calls`, agent-scoped, on `(agent_id, started_at)`); their attempts go with them (`ON DELETE CASCADE`).

Model-call text is kept `NOUS_MODEL_CALL_TEXT_RETENTION_DAYS` (7) days. While both the model-call log and the text are on, the same loop deletes this agent's refs older than that, then its blocks that no call references and no store has used for an hour, in one transaction (`sweep_model_call_text`, agent-scoped). A store touches every block it sends, so a block in use is never old. The sweep locks the blocks it deletes in hash order, the order a store upserts them in, so a sweep and a store never deadlock, and a block a store touches while the sweep waits for it is kept: its new ref never meets a deleted block. The numbers rows are untouched and keep their own 30 days; a call's refs go with its row, so the text is never kept longer than its numbers.

Each sweep runs in a try of its own, so one failing sweep never skips another.

## Who reads the rows

The dashboards, and Nous itself: the `db_query` reader (`nous_reader`, `scripts/ops/db_roles.sql` section 5d) may read `tool_call_log`, `model_call_log` and `model_call_attempt`, so Nous can look up its own tool calls, their reasons and its own model calls. It never reads `agent_turn_log`, which holds message text, nor the model-call text (`model_text_block` and `model_call_block_ref`, the full prompts and responses): `db_roles.sql` lists none of them. This is the one exception to "dashboards read the observability tables, the agent never does".

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

The model-call text (PR 3) is not in these figures: its size, with the dedup, is measured in prod after deploy (see What PR 3 records, Measured).

## Turning it on

`docker-compose.yml` passes `NOUS_*` variables one by one and is a code-owner path, so this PR does not change it. To turn recording on, the owner adds pass-through lines for the settings to the `nous` service (`- NOUS_TOOL_CALL_LOG_ENABLED=${NOUS_TOOL_CALL_LOG_ENABLED:-false}`, `- NOUS_TOOL_REASON_ENABLED=${NOUS_TOOL_REASON_ENABLED:-false}`, and any cap or retention that should differ from its default) and sets the values in the host `.env`; a value only in `.env` never reaches the container. Migration 100 is applied by the application at start, before `scripts/ops/db_roles.sql` is run again to grant the reader `tool_call_log` (see [database roles](../reference/database-roles.md)). The model-call numbers need `- NOUS_MODEL_CALL_LOG_ENABLED=${NOUS_MODEL_CALL_LOG_ENABLED:-false}` (and `NOUS_MODEL_CALL_RETENTION_DAYS` if it should differ from 30) the same way; migration 101 is applied at start, before `db_roles.sql` is run again to grant the reader `model_call_log` and `model_call_attempt`. The text needs `- NOUS_MODEL_CALL_TEXT_ENABLED=${NOUS_MODEL_CALL_TEXT_ENABLED:-false}` as well as the numbers line (and `NOUS_MODEL_CALL_TEXT_RETENTION_DAYS` or `NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS` if they should differ from 7 and 200,000); migration 102 is applied at start and needs no `db_roles.sql` run, since the reader reads neither text table.

## Reference

- Settings: [environment variables](../reference/environment-variables.md) (`NOUS_TOOL_CALL_LOG_ENABLED`, `NOUS_TOOL_REASON_ENABLED`, `NOUS_TOOL_CALL_RETENTION_DAYS`, `NOUS_TOOL_INPUT_MAX_CHARS`, `NOUS_TOOL_RESULT_PREVIEW_CHARS`, `NOUS_TOOL_REASON_MAX_CHARS`, `NOUS_MODEL_CALL_LOG_ENABLED`, `NOUS_MODEL_CALL_RETENTION_DAYS`, `NOUS_MODEL_CALL_TEXT_ENABLED`, `NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS`, `NOUS_MODEL_CALL_TEXT_RETENTION_DAYS`).
- Endpoint: [REST API](../reference/rest-api.md) (`GET /dashboard/observability`, `recording`).
- Reader: [database roles](../reference/database-roles.md) and [agent tools](../reference/agent-tools.md) (`db_query`).
- Code: `nous/observability/recording.py`, `nous/observability/tool_calls.py`, `nous/observability/model_calls.py`, `nous/observability/model_call_text.py`, `nous/observability/work_view_retention.py`, `nous/api/runner.py` (`_note_tool_call`, `_note_turn`, the call contexts), `nous/api/anthropic_client.py` (`set_recorder`, the scopes and attempts), `nous/browser/loop.py`, `nous/brain/embeddings.py` and `nous/heart/reranker.py` (`set_recorder`), `nous/main.py` (wiring, retention loop, shutdown).
