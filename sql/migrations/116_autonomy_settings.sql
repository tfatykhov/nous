-- 116: the owner's autonomy settings (F115)
--
-- nous_system.autonomy_settings: one row per limit the owner changed on the dashboard (key is a key of
-- nous/autonomy/registry.py, value its JSON value). With no row a limit is the host's. A stored value is checked
-- against the registry's range and the host's ceiling when it is loaded, and skipped when it fails.
--
-- nous_system.autonomy_settings_log: one row per change of a limit or of a tool's setting, written in the same
-- transaction as the change: the key (a limit's key, or tool_guard:<scope>:<tool>), the value before and after (NULL
-- is the host's value or the default), who made it (the proof the owner's route accepted) and when.
--
-- nous_system.tool_guard: one row per tool and scope the owner set. scope background is every turn nobody
-- converses in. The owner's route writes scope background with the settings allowed and denied only.
-- destructive_guard is stored with its default and read by nothing yet.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched: the db_query reader is
-- not granted these tables. Created by the application role through the migrator.

CREATE TABLE IF NOT EXISTS nous_system.autonomy_settings (
    agent_id   TEXT        NOT NULL,
    key        TEXT        NOT NULL CHECK (char_length(key) BETWEEN 1 AND 100),
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT        NOT NULL CHECK (char_length(updated_by) BETWEEN 1 AND 40),
    PRIMARY KEY (agent_id, key)
);

CREATE TABLE IF NOT EXISTS nous_system.autonomy_settings_log (
    id         BIGSERIAL   PRIMARY KEY,
    agent_id   TEXT        NOT NULL,
    key        TEXT        NOT NULL CHECK (char_length(key) BETWEEN 1 AND 300),
    old_value  JSONB,
    new_value  JSONB,
    actor      TEXT        NOT NULL CHECK (char_length(actor) BETWEEN 1 AND 40),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The screen lists an agent's newest changes.
CREATE INDEX IF NOT EXISTS idx_autonomy_settings_log_agent_id
    ON nous_system.autonomy_settings_log (agent_id, id DESC);

CREATE TABLE IF NOT EXISTS nous_system.tool_guard (
    agent_id          TEXT        NOT NULL,
    scope             TEXT        NOT NULL CHECK (scope IN ('background', 'goal_session')),
    tool              TEXT        NOT NULL CHECK (char_length(tool) BETWEEN 1 AND 200),
    setting           TEXT        NOT NULL CHECK (setting IN ('allowed', 'ask', 'denied')),
    destructive_guard TEXT        NOT NULL DEFAULT 'common' CHECK (destructive_guard IN ('off', 'common', 'changes')),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by        TEXT        NOT NULL CHECK (char_length(updated_by) BETWEEN 1 AND 40),
    PRIMARY KEY (agent_id, scope, tool)
);
