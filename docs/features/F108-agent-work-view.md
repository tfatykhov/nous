# F108: Agent Work View

**Status:** PR 1 in progress (Phase 1, recording tool calls and turns). Both settings default off: `NOUS_TOOL_CALL_LOG_ENABLED` and `NOUS_TOOL_REASON_ENABLED`. Later PRs add the model-call numbers, the full model-call text, the views and the tool notes.

## Problem

Nobody can answer "what did Nous do in this conversation, and why" from what is stored. Only side-effecting tool calls are stored (the execution ledger), and only the tool names of each turn (`f026_claim_verification` events). Nous's reasons for its tool calls are never stored, so a session's work cannot be followed call by call, nor across sessions.

## What PR 1 records

### Tool calls (`nous_system.tool_call_log`, migration 100)

One row per tool call the model asked for, written after the call by the runner (`AgentRunner._note_tool_call`, through `ToolCallRecorder.record`, `nous/observability/tool_calls.py`). Calls that never ran are recorded too: `outcome` is one of `ran`, `error`, `refused` (a tool policy refused it), `gated` (the F026 action gate blocked it), `suppressed` (a keyed send whose key was already held), `bad_input` (malformed input JSON) or `retired` (a retired tool name); `refused_by` holds the refusal code, or `action_gate`. There is no CHECK on `outcome`, so a new outcome is recorded, not dropped.

A row holds the agent, session and parent session, the turn's id and number, the call's 0-based place in its turn (`call_index`, counted across the turn's stream segments), the kind of work (`conversation`, `subtask`, `workflow_step`, `intention`, `heartbeat`, `background_check`, from the turn's `ExecutionContext.kind`), the subtask and DAG ids when there are any, the canonical tool name and the name the model used (an alias), the reason, the input, a result preview, the error flag and error text, the duration, the start time and the execution-ledger row of a side-effecting call.

The input, result preview, reason and error text are redacted with the execution ledger's redaction before they are cut (`detail_text`): at most `NOUS_TOOL_INPUT_MAX_CHARS` (2,000), `NOUS_TOOL_RESULT_PREVIEW_CHARS` (500), `NOUS_TOOL_REASON_MAX_CHARS` (500) and 1,000 characters. Over a cap, the head and the tail are kept around a 16-character marker.

`execute_single_call` (one owner-approved call run with no model in between) writes its tool-call row under a turn id of its own, with no turn number, and no turn row.

### Turns (`nous_system.agent_turn_log`, migration 100)

One row per turn, `id` = the turn's id, so a turn's tool calls join on `tool_call_log.turn_id`. Written when the turn ends, before `post_turn` (`ToolCallRecorder.record_turn`, an upsert on `id`). It holds what started the turn (the message or the background trigger, at most 500 characters), the memory already in the context (the ids of the recalled facts, decisions, procedures and episodes, at most 50 per kind), the critic's verdict for the turn (the `critic_classified` event's data, or null when the critic did not run), the claim check (the `f026_claim_verification` result, or null), the reply (at most 1,000 characters), the error (at most 1,000; `cancelled` for a cancelled turn), the number of tool calls, and the start and end times. Every text, and every string inside the critic and claim-check JSON, is redacted before it is cut. Turn numbers repeat after compaction, so a trace is ordered by `agent_turn_log.started_at`, then by `(turn_id, call_index)`.

Turn paths that write no turn row (they also skip `post_turn`):

- `run_turn` and `stream_chat`: `pre_turn` raises or is cancelled (before the turn's id exists).
- `run_turn`: the attachment ingest or the execution-ledger setup raises or is cancelled, or recording the attachment fact (after the turn's try) raises or is cancelled.
- `stream_chat`: the attachment ingest, the execution-ledger setup, the system-prompt build, the offered-tool list or the compaction raises or is cancelled (before the turn's try).

A `stream_chat` turn whose consumer closes the generator (rather than cancelling it) writes its row with no error.

### The reason field (`NOUS_TOOL_REASON_ENABLED`)

Every tool offered to the model gets a required string `reason`: "One sentence: why this tool, for this step." It is added to the offered copies of the schemas only (`add_reason_field` in `AgentRunner._offered_tools`), after the F061 extra tools, so the dispatcher's schemas and its per-frame cache never change. A tool whose own schema declares `reason` (`create_censor`) keeps its own and its handler still gets it. For every other tool the runner takes the reason out of the call before the tool runs (`split_reason`); the dispatcher also drops a stray `reason` in `_repair`, so it never reaches a handler, the repaired arguments or the idempotency key. A call without a reason still runs; its row has a null reason, so the rate of missing reasons can be watched. The setting works with or without the log. Turning it on or off changes every tool schema once, so the prompt cache misses once after the flip.

## How the rows are written

- **One bounded writer.** `main.py` builds one `BoundedWriter` (`nous/observability/recording.py`) when `NOUS_TOOL_CALL_LOG_ENABLED` is on, and hands it to the `ToolCallRecorder`. The model-call recorder of a later PR takes the same writer. A write is a factory scheduled fire-and-forget: at most 4 writes hold a pool connection at once, each is cut off after 30 s, and past 500 pending writes a new one is dropped. `record` and `record_turn` are synchronous and never raise, so recording never fails or slows a turn.
- **Counted, never silent.** Every outcome is counted (`written`, `dropped_timeout`, `dropped_error`, `dropped_backpressure`) and every failure is logged at WARNING. `GET /dashboard/observability` returns the recorder's counters under `recording.tool_calls` (null while the log is off).
- **Shutdown.** `shutdown_components` waits up to 5 s for the pending writes immediately before `database.disconnect()`, after every producer of turns has stopped.
- **Off means nothing.** With the log off, no writer, recorder or sweep is built, and the runner records nothing.

## Retention

Rows are kept `NOUS_TOOL_CALL_RETENTION_DAYS` (30) days. A daily loop (`_tool_call_retention_loop` in `main.py`, started with the log) deletes this agent's `tool_call_log` and `agent_turn_log` rows that started earlier, in one transaction (`sweep_tool_calls`, `nous/observability/work_view_retention.py`). It is agent-scoped, so one agent never deletes the rows of another that keeps them longer, and both tables index `(agent_id, started_at)` for it. A failed sweep logs a WARNING and is tried again a day later. As for the context-log sweep, the first pass runs a day after start, so a process restarted more often than once a day does not sweep. A retention of 0 or less starts no sweep.

## Who reads the rows

The dashboards, and Nous itself: the `db_query` reader (`nous_reader`, `scripts/ops/db_roles.sql` section 5d) may read `tool_call_log`, so Nous can look up its own tool calls and their reasons. It never reads `agent_turn_log`, which holds message text. This is the one exception to "dashboards read the observability tables, the agent never does".

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

Over 30 days that is 22,500 to 120,000 tool-call rows (about 30 MB typical, 520 MB if every row hit every cap) and 15,000 to 63,000 turn rows (about 100 MB typical), plus their indexes: about 100 to 300 MB in all at steady state. For comparison, `context_log` holds about 1.6 KB a row (44,226 rows, 72 MB, since 2026-10-04).

The daily sweep deletes one day's rows, about 1,250 to 6,100. Deletes of that size need only autovacuum: the dead rows are reclaimed and their space reused by the next days' inserts, so the tables stay near their 30-day size. No partitioning is needed.

## Turning it on

`docker-compose.yml` passes `NOUS_*` variables one by one and is a code-owner path, so this PR does not change it. To turn recording on, the owner adds pass-through lines for the settings to the `nous` service (`- NOUS_TOOL_CALL_LOG_ENABLED=${NOUS_TOOL_CALL_LOG_ENABLED:-false}`, `- NOUS_TOOL_REASON_ENABLED=${NOUS_TOOL_REASON_ENABLED:-false}`, and any cap or retention that should differ from its default) and sets the values in the host `.env`; a value only in `.env` never reaches the container. Migration 100 is applied by the application at start, before `scripts/ops/db_roles.sql` is run again to grant the reader `tool_call_log` (see [database roles](../reference/database-roles.md)).

## Reference

- Settings: [environment variables](../reference/environment-variables.md) (`NOUS_TOOL_CALL_LOG_ENABLED`, `NOUS_TOOL_REASON_ENABLED`, `NOUS_TOOL_CALL_RETENTION_DAYS`, `NOUS_TOOL_INPUT_MAX_CHARS`, `NOUS_TOOL_RESULT_PREVIEW_CHARS`, `NOUS_TOOL_REASON_MAX_CHARS`).
- Endpoint: [REST API](../reference/rest-api.md) (`GET /dashboard/observability`, `recording`).
- Reader: [database roles](../reference/database-roles.md) and [agent tools](../reference/agent-tools.md) (`db_query`).
- Code: `nous/observability/recording.py`, `nous/observability/tool_calls.py`, `nous/observability/work_view_retention.py`, `nous/api/runner.py` (`_note_tool_call`, `_note_turn`), `nous/main.py` (wiring, retention loop, shutdown drain).
