-- 100: the tool-call log and the turn log (F108 agent work view, Phase 1 recording)
--
-- tool_call_log: one row per tool call the model asked for, refused and gated calls included, written after the
-- call by the runner's recorder (NOUS_TOOL_CALL_LOG_ENABLED, off by default). call_index is the call's 0-based
-- place in its turn, counted across the turn's stream segments and never reset. tool_name is the canonical name,
-- tool_name_called what the model sent. input_text, result_preview and reason are redacted and capped by the writer.
-- outcome is one of ran, error, refused, gated, suppressed, retired or bad_input (no CHECK, so a new outcome is
-- recorded, not dropped). refused_by is the refusal code or action_gate. ledger_entry_id names the execution_ledger
-- row of a side-effecting call. turn_id names the agent_turn_log row with no foreign key, because the turn row is
-- written when the turn ends, after its calls.
--
-- agent_turn_log: one row per turn, id = the turn's turn_id. Turn numbers repeat after compaction, so a trace is
-- ordered by agent_turn_log.started_at, then by (turn_id, call_index), never by (session_id, turn_number).
-- trigger_text and reply_text are redacted and capped by the writer. It holds message text, so the db_query
-- reader does not read it.
--
-- No column is named like a credential (scripts/ops/db_roles.sql section 5d refuses to grant one).
-- Writer: the app. Readers: the app, and the db_query reader for tool_call_log only (scripts/ops/db_roles.sql).
-- Created by the application role through the migrator: the tables and their indexes, nothing else.

CREATE TABLE IF NOT EXISTS nous_system.tool_call_log (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id           TEXT         NOT NULL,
    session_id         TEXT,
    parent_session_id  TEXT,
    turn_id            UUID,
    turn_number        INT,
    call_index         INT          NOT NULL,
    work_kind          TEXT         NOT NULL,
    context_kind       TEXT,
    subtask_id         UUID,
    dag_id             UUID,
    dag_node_id        UUID,
    tool_name          TEXT         NOT NULL,
    tool_name_called   TEXT         NOT NULL,
    reason             TEXT,
    input_text         TEXT         NOT NULL DEFAULT '',
    result_preview     TEXT         NOT NULL DEFAULT '',
    outcome            TEXT         NOT NULL,
    is_error           BOOLEAN      NOT NULL DEFAULT FALSE,
    error_text         TEXT,
    refused_by         TEXT,
    duration_ms        INT,
    started_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ledger_entry_id    UUID
);

-- A turn's calls in order.
CREATE INDEX IF NOT EXISTS idx_tool_call_log_turn_call
    ON nous_system.tool_call_log (turn_id, call_index);

-- Recent calls, and the retention sweep.
CREATE INDEX IF NOT EXISTS idx_tool_call_log_agent_started
    ON nous_system.tool_call_log (agent_id, started_at DESC);

-- Recent calls of one tool.
CREATE INDEX IF NOT EXISTS idx_tool_call_log_agent_tool_started
    ON nous_system.tool_call_log (agent_id, tool_name, started_at DESC);

-- Recent calls of one session.
CREATE INDEX IF NOT EXISTS idx_tool_call_log_agent_session_started
    ON nous_system.tool_call_log (agent_id, session_id, started_at DESC);

CREATE TABLE IF NOT EXISTS nous_system.agent_turn_log (
    id                 UUID         PRIMARY KEY,
    agent_id           TEXT         NOT NULL,
    session_id         TEXT,
    parent_session_id  TEXT,
    turn_number        INT,
    work_kind          TEXT         NOT NULL,
    context_kind       TEXT,
    frame_id           TEXT,
    trigger_text       TEXT,
    memory_context     JSONB,
    critic             JSONB,
    claim_check        JSONB,
    reply_text         TEXT,
    error              TEXT,
    tool_call_count    INT,
    started_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at           TIMESTAMPTZ
);

-- A session's turns.
CREATE INDEX IF NOT EXISTS idx_agent_turn_log_agent_session_turn
    ON nous_system.agent_turn_log (agent_id, session_id, turn_number);

-- Recent turns, and the retention sweep.
CREATE INDEX IF NOT EXISTS idx_agent_turn_log_agent_started
    ON nous_system.agent_turn_log (agent_id, started_at DESC);
