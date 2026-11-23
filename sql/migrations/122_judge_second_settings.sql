-- 122: nous_system.judge_second_settings, the owner's settings of the second outside judge (F118 PR 3)
--
-- One row per agent and judge name (jev only). Each setting is NULL for the server's value: enabled (whether the
-- second judge runs), model (a versioned id) and daily_calls (the most requests a UTC day, never above the server's
-- NOUS_OUTSIDE_JUDGE_DAILY_CALLS, which the application applies). The API key is stored sealed by the application
-- (AES-256-GCM with the key in NOUS_MCP_CLIENT_KEY_FILE): key_id names the key that sealed it, nonce and ciphertext
-- are the sealed value, key_set_at is when it was saved. The four are all NULL or all set. No plaintext, length or
-- part of the key is stored. updated_by is the proof the owner guard accepted.
--
-- Writer and reader: the app only, through its own routes. No grant, and scripts/ops/db_roles.sql is not touched.
-- Rollback is DROP TABLE IF EXISTS nous_system.judge_second_settings and deleting version 122 from schema_migrations.

CREATE TABLE IF NOT EXISTS nous_system.judge_second_settings (
    agent_id    TEXT        NOT NULL,
    judge_name  TEXT        NOT NULL CHECK (judge_name = 'jev'),
    enabled     BOOLEAN,
    model       TEXT        CHECK (model IS NULL OR model ~ '^jev-[0-9]+\.[0-9]+\.[0-9]+$'),
    daily_calls INTEGER     CHECK (daily_calls IS NULL OR (daily_calls >= 0 AND daily_calls <= 5000)),
    key_id      TEXT,
    nonce       BYTEA,
    ciphertext  BYTEA,
    key_set_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT        NOT NULL CHECK (updated_by IN ('owner', 'proxy')),
    PRIMARY KEY (agent_id, judge_name),
    CONSTRAINT chk_judge_second_settings_key CHECK (
        (key_id IS NULL AND nonce IS NULL AND ciphertext IS NULL AND key_set_at IS NULL) OR
        (key_id IS NOT NULL AND nonce IS NOT NULL AND ciphertext IS NOT NULL AND key_set_at IS NOT NULL))
);
