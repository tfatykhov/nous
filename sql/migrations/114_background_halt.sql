-- 114: the background halt switch (Goals PR 0c)
--
-- nous_system.background_halt: one row per agent, whether background work is halted, who halted it and when.
-- nous_system.background_halt_log: one append-only row per halt, resume, refused resume and sweep, written in the
-- same transaction as the state change (the event bus drops events when its queue is full, so it is not an audit
-- record). reason, actor and via are short text from the caller, cut by the application (200, 100 and 40
-- characters). Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Idempotent.

CREATE TABLE IF NOT EXISTS nous_system.background_halt (
    agent_id        TEXT        PRIMARY KEY,
    halted          BOOLEAN     NOT NULL DEFAULT FALSE,
    halted_at       TIMESTAMPTZ,
    halted_by       TEXT,
    halted_via      TEXT,
    halted_verified BOOLEAN     NOT NULL DEFAULT FALSE,
    reason          TEXT,
    resumed_at      TIMESTAMPTZ,
    resumed_by      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (reason IS NULL OR char_length(reason) <= 200)
);

CREATE TABLE IF NOT EXISTS nous_system.background_halt_log (
    id        BIGSERIAL   PRIMARY KEY,
    agent_id  TEXT        NOT NULL,
    at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action    TEXT        NOT NULL CHECK (action IN ('halt', 'resume', 'resume_refused', 'sweep')),
    actor     TEXT,
    via       TEXT,
    verified  BOOLEAN     NOT NULL DEFAULT FALSE,
    reason    TEXT,
    detail    JSONB
);

CREATE INDEX IF NOT EXISTS background_halt_log_agent_at ON nous_system.background_halt_log (agent_id, at DESC);
