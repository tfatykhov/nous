-- 027: F034.5 Dynamic Heartbeat Checks
CREATE TABLE IF NOT EXISTS nous_system.dynamic_checks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL DEFAULT 'nous',
    name            TEXT NOT NULL,
    description     TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    tools           TEXT[] DEFAULT '{}',
    cron_expr       TEXT,
    interval_seconds INTEGER DEFAULT 3600,
    timeout_seconds  INTEGER DEFAULT 30,
    enabled         BOOLEAN DEFAULT TRUE,
    urgent          BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    created_by      TEXT DEFAULT 'conversation',
    last_run_at     TIMESTAMPTZ,
    run_count       INTEGER DEFAULT 0,
    error_count     INTEGER DEFAULT 0,
    last_error      TEXT,
    metadata        JSONB DEFAULT '{}',
    UNIQUE(agent_id, name)
);

CREATE INDEX IF NOT EXISTS idx_dynamic_checks_agent_enabled
    ON nous_system.dynamic_checks(agent_id, enabled);
