-- 026: F035 Observability — event tracing + context_log + behavior_snapshots

-- F035.2: Causal chain tracing columns on events table
ALTER TABLE nous_system.events ADD COLUMN IF NOT EXISTS event_id VARCHAR(12);
ALTER TABLE nous_system.events ADD COLUMN IF NOT EXISTS trace_id VARCHAR(12);
ALTER TABLE nous_system.events ADD COLUMN IF NOT EXISTS caused_by VARCHAR(12);

CREATE INDEX IF NOT EXISTS idx_events_trace_id ON nous_system.events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_event_id ON nous_system.events(event_id);
CREATE INDEX IF NOT EXISTS idx_events_caused_by ON nous_system.events(caused_by);

-- F035.4: Context log table
CREATE TABLE IF NOT EXISTS nous_system.context_log (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    call_type   TEXT NOT NULL,
    model       TEXT NOT NULL,
    frame_id    TEXT,
    trace_id    VARCHAR(12),

    token_breakdown     JSONB NOT NULL DEFAULT '{}',
    total_tokens_est    INTEGER NOT NULL DEFAULT 0,
    context_window_size INTEGER NOT NULL DEFAULT 0,
    utilization_pct     REAL NOT NULL DEFAULT 0.0,

    sections_present    TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    tools_count         INTEGER NOT NULL DEFAULT 0,
    tool_names          TEXT[],
    messages_count      INTEGER NOT NULL DEFAULT 0,
    message_roles       JSONB,

    loaded_facts        INTEGER NOT NULL DEFAULT 0,
    loaded_decisions    INTEGER NOT NULL DEFAULT 0,
    loaded_procedures   INTEGER NOT NULL DEFAULT 0,
    loaded_episodes     INTEGER NOT NULL DEFAULT 0,
    recent_conversations INTEGER NOT NULL DEFAULT 0,

    input_tokens_actual INTEGER,
    output_tokens       INTEGER,
    cache_creation      INTEGER,
    cache_read          INTEGER,
    duration_ms         REAL,
    stop_reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_context_log_session ON nous_system.context_log(session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_context_log_time ON nous_system.context_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_context_log_agent ON nous_system.context_log(agent_id);

-- F035.3: Behavior snapshots table
CREATE TABLE IF NOT EXISTS nous_system.behavior_snapshots (
    id          SERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    metrics     JSONB NOT NULL,
    anomalies   JSONB DEFAULT '[]',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_behavior_snapshots_ts ON nous_system.behavior_snapshots(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_behavior_snapshots_agent ON nous_system.behavior_snapshots(agent_id);
