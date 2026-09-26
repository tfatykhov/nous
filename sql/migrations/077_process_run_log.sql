-- 077: process_run_log — per-phase runtime heartbeat for fault detection
--
-- One row per periodic memory-process execution (e.g. 'sleep/stale_scan').
-- Written by ProcessRecorder; consumed by ProcessFaultCheck heartbeat check.
-- Fail-open: if a write fails, the owning process continues normally.

CREATE TABLE IF NOT EXISTS nous_system.process_run_log (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    process_name TEXT       NOT NULL,   -- e.g. 'sleep/stale_scan'
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL DEFAULT 'started',  -- started/finished/error/skipped
    items_examined INTEGER,   -- NULL when the phase doesn't track this
    items_changed  INTEGER,   -- NULL when the phase doesn't track this
    error_message  TEXT,
    metadata       JSONB,
    CONSTRAINT ck_process_run_log_status
        CHECK (status IN ('started', 'finished', 'error', 'skipped'))
);

-- Primary lookup: "what's the recent history for this agent+phase?"
CREATE INDEX IF NOT EXISTS process_run_log_agent_process_idx
    ON nous_system.process_run_log (agent_id, process_name, started_at DESC);

-- Prune helper: "all rows older than N days for this agent"
CREATE INDEX IF NOT EXISTS process_run_log_agent_started_idx
    ON nous_system.process_run_log (agent_id, started_at);
