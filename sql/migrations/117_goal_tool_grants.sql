-- 117: the tools the owner allowed one goal to use without asking (F115 per-goal tools)
--
-- brain.goal_tool_grants: one row per grant. A grant is changed only to revoke it (revoked_by and revoked_at set
-- together), and a revoked row stays, so the table is also the record of every grant. At most one grant of a tool per
-- goal is active (revoked_at IS NULL). granted_by and revoked_by are the proof the owner's route accepted (owner or
-- proxy). For a tool of an external MCP server (a name that starts with mcp__) the row keeps the identity the owner
-- saw when allowing it: the server's name, its host, its row id (no foreign key, so the server can still be deleted),
-- the SHA-256 of its full URL (never the URL) and the server's own name for the tool. Those five are all NULL for any
-- other tool.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched: the db_query reader is not
-- granted this table. Whoever holds the application's database login can write it, as it can write every table the
-- app owns. Created by the application role through the migrator.

CREATE TABLE IF NOT EXISTS brain.goal_tool_grants (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id    TEXT        NOT NULL,
    goal_id     UUID        NOT NULL REFERENCES brain.goals(id),
    tool        TEXT        NOT NULL CHECK (char_length(tool) BETWEEN 1 AND 200 AND tool ~ '^[A-Za-z0-9_-]+$'),
    server_name TEXT,
    server_host TEXT        CHECK (server_host IS NULL OR char_length(server_host) BETWEEN 1 AND 300),
    server_id   UUID,
    url_sha256  TEXT        CHECK (url_sha256 IS NULL OR url_sha256 ~ '^[0-9a-f]{64}$'),
    remote_tool TEXT        CHECK (remote_tool IS NULL OR char_length(remote_tool) BETWEEN 1 AND 200),
    granted_by  TEXT        NOT NULL CHECK (char_length(granted_by) BETWEEN 1 AND 40),
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_by  TEXT        CHECK (revoked_by IS NULL OR char_length(revoked_by) BETWEEN 1 AND 40),
    revoked_at  TIMESTAMPTZ,
    CONSTRAINT chk_goal_tool_grants_revoked CHECK ((revoked_by IS NULL) = (revoked_at IS NULL)),
    CONSTRAINT chk_goal_tool_grants_mcp CHECK (
        (left(tool, 5) = 'mcp__') = (server_name IS NOT NULL AND server_host IS NOT NULL AND server_id IS NOT NULL
                                     AND url_sha256 IS NOT NULL AND remote_tool IS NOT NULL)
        AND (left(tool, 5) = 'mcp__' OR (server_name IS NULL AND server_host IS NULL AND server_id IS NULL
                                         AND url_sha256 IS NULL AND remote_tool IS NULL)))
);

-- At most one active grant of a tool per goal.
CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_tool_grants_active
    ON brain.goal_tool_grants (goal_id, tool) WHERE revoked_at IS NULL;

-- A goal's grants, newest first.
CREATE INDEX IF NOT EXISTS idx_goal_tool_grants_goal ON brain.goal_tool_grants (goal_id, granted_at DESC);
