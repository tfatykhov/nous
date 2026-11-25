-- 124: nous_system.tool_loading_watch, what a watched model call would have left out (F120, tool loading, watch only)
--
-- One row per model call made while NOUS_TOOL_LOADING_ENABLED is on and the owner's setting is Watch only for the
-- call's kind of call. The app's model-call recorder writes it after the call's own nous_system.model_call_log row is
-- committed, in a transaction of its own. call_id is that row's id. No foreign key, as the tool-call and turn logs:
-- the card joins the two on call_id and leaves out a watch row whose call row is missing. kind is the kind of call.
-- record holds names and counts only: how many tools the call sent and how many it would have left out, their
-- estimated tokens and those of the list of names that would replace them, the names kept in full, the call's model,
-- and whether the call read its tools from the prompt cache or wrote them. Rows go with the model-call retention
-- sweep (NOUS_MODEL_CALL_RETENTION_DAYS, the same agent and the same age as model_call_log).
--
-- Writer and reader: the app only. No grant: the db_query reader is not given this table, and scripts/ops/db_roles.sql
-- is not touched. Rollback is DROP TABLE IF EXISTS nous_system.tool_loading_watch and deleting version 124 from
-- schema_migrations, after the code is rolled back. Dropping it removes the watch records only: model_call_log and
-- every other table are untouched, and while code that writes it still runs, each watch write fails on its own (logged
-- once) and the model-call rows are written as before.

CREATE TABLE IF NOT EXISTS nous_system.tool_loading_watch (
    call_id    UUID        PRIMARY KEY,
    agent_id   TEXT        NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    turn_id    UUID,
    kind       TEXT        NOT NULL,
    record     JSONB       NOT NULL
);

-- The card's window and the retention sweep, both per agent by start.
CREATE INDEX IF NOT EXISTS idx_tool_loading_watch_agent_started
    ON nous_system.tool_loading_watch (agent_id, started_at DESC);
