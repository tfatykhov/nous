-- Migration 081: Result inbox + channel identity (F098 Phase A)
--
-- Background results (subtasks, DAGs) were keyed to the session that spawned
-- them. Telegram sessions expire after 30 min idle, so a result that finished
-- after the rollover was never injected. Results are now keyed to the
-- CHANNEL the conversation lives on (e.g. 'telegram:<chat_id>'), through one
-- inbox read at pre-turn and claimed exactly once.
--
-- No backfill: historical undelivered subtasks get no inbox row (F098 §4.6),
-- so turning NOUS_RESULT_INBOX_ENABLED on cannot flood context.

CREATE TABLE IF NOT EXISTS heart.result_inbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    channel TEXT,
    session_id TEXT,
    source_kind VARCHAR(20) NOT NULL,
    source_id UUID NOT NULL,
    -- A DAG's execution_dags.delivery_generation (0 for subtasks): retry_node
    -- bumps it, and the retried run's outcome is a new result, not a duplicate.
    source_generation INTEGER NOT NULL DEFAULT 0,
    msg_type VARCHAR(20) NOT NULL,
    correlation_id TEXT,
    reply_to TEXT,
    title VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    delivered_session_id TEXT,
    wake_attempted_at TIMESTAMPTZ,
    CONSTRAINT uq_result_inbox_source UNIQUE (source_kind, source_id, source_generation),
    CONSTRAINT chk_result_inbox_source_kind CHECK (source_kind IN ('subtask', 'dag')),
    CONSTRAINT chk_result_inbox_msg_type CHECK (msg_type IN ('INFORM', 'FAILURE', 'BLOCKED'))
);

CREATE INDEX IF NOT EXISTS idx_result_inbox_channel_undelivered
    ON heart.result_inbox (agent_id, channel) WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_result_inbox_session_undelivered
    ON heart.result_inbox (agent_id, session_id) WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_result_inbox_created
    ON heart.result_inbox (agent_id, created_at DESC);

-- channel -> the latest session on it. Survives restarts (the Telegram
-- bot's chat->session map is in-process only); the Phase B wake turn reads it.
CREATE TABLE IF NOT EXISTS heart.channel_sessions (
    agent_id VARCHAR(100) NOT NULL,
    channel TEXT NOT NULL,
    session_id TEXT NOT NULL,
    last_active TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, channel)
);

-- When the inbox was first switched on for an agent, written once by the
-- first process that starts with the flag on. The reconciler repairs only
-- results that finished after it, so enabling the flag never backfills.
CREATE TABLE IF NOT EXISTS heart.result_inbox_state (
    agent_id VARCHAR(100) PRIMARY KEY,
    enabled_at TIMESTAMPTZ NOT NULL
);

-- Origin capture: where a subtask / DAG was started from.
ALTER TABLE heart.subtasks
    ADD COLUMN IF NOT EXISTS parent_channel TEXT;

ALTER TABLE nous_system.execution_dags
    ADD COLUMN IF NOT EXISTS origin_channel TEXT;

ALTER TABLE nous_system.execution_dags
    ADD COLUMN IF NOT EXISTS origin_session_id TEXT;
