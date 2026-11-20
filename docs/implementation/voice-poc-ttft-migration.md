# Voice POC PR 4: the TTFT migration (111) for the owner to add

The code in this PR writes `model_call_log.ttft_ms`, `agent_turn_log.pre_turn_ms` and `agent_turn_log.pre_turn_phases`.
The migration file is not in this PR: the job that wrote it cannot create files under `sql/migrations/` (the FORGE hook
refuses them in a headless run). Add it as `sql/migrations/111_ttft.sql` before merge (110 is `110_mcp_client_secrets.sql`).
`tests/test_ttft_instrumentation.py::test_the_migration_matches_this_document` compares the file with the block below
once it exists.

Until the columns exist, the model-call and turn rows fail to write (the recorders drop them and log a warning; no call
or turn is affected).

## Owner steps

1. Create `sql/migrations/111_ttft.sql` from the block below.
2. In `tests/test_f099_phase2d_parity.py` change the `startswith("110_")` pin to `"111_"` (and its comment).
3. In `tests/test_f099_phase2e_parity.py` add `migrations[-1].name == "111_ttft.sql"` and shift the other indexes by one.
4. Apply it to the database by the normal migration path after merge (the job did not touch the production database).

## DDL

```sql
-- 111: time to first token and the phases of pre_turn (voice POC, TTFT instrumentation)
--
-- model_call_log.ttft_ms: milliseconds from the start of a streamed call to its first text_delta or tool_start event,
-- set by the recorder (ModelCallScope.mark_first_token) only for a stream call that ended ok. NULL for a
-- non-streamed call (call, aggregated, embedding, rerank, browser_step), for a stream that failed or was cut off, and for every
-- row written before this migration.
--
-- agent_turn_log.pre_turn_ms: milliseconds CognitiveLayer.pre_turn took, from monotonic clocks. pre_turn_phases is the
-- same time by phase, a JSON object: frame_select_ms, critic_ms, context_build_ms, other_ms (what no named phase
-- covers) and total_ms (= pre_turn_ms). NULL for a turn written before this migration. Numbers only: no text.
--
-- No column is named like a credential (scripts/ops/db_roles.sql section 5d refuses to grant one).
-- Writer: the app. Readers: the app. The db_query reader sees none of these columns until its grant in
-- scripts/ops/db_roles.sql is widened (not done here).
-- Created by the application role through the migrator: the columns, nothing else.

ALTER TABLE nous_system.model_call_log ADD COLUMN IF NOT EXISTS ttft_ms INT;
ALTER TABLE nous_system.agent_turn_log ADD COLUMN IF NOT EXISTS pre_turn_ms INT;
ALTER TABLE nous_system.agent_turn_log ADD COLUMN IF NOT EXISTS pre_turn_phases JSONB;
```
