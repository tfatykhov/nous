-- 110: the stored credentials of the MCP client's servers (F112 PR 2c)
--
-- nous_system.mcp_server_secrets: at most one row per server (server_id is unique), either headers (an API key or
-- another header the server wants) or, from PR 2d, an OAuth record. nonce and ciphertext are AES-256-GCM (the
-- ciphertext includes the tag), sealed by the application with the key in NOUS_MCP_CLIENT_KEY_FILE and associated
-- data naming the agent, the server's name, its URL and the kind, so a row moved to another server or URL cannot be
-- opened. key_id is the first 16 hex characters of the key's SHA-256. header_names is plain, for display (empty
-- for oauth). The row's id is the credential's version: the owner's set or replace deletes the row and inserts a
-- new one, a token refresh updates it in place. The row goes with its server (ON DELETE CASCADE).
--
-- Writer: the owner's /integrations/mcp routes. Reader: the app. Not read by the db_query reader (its list is an
-- allowlist, and scripts/ops/db_roles.sql is not touched). Created by the application role through the migrator.

CREATE TABLE IF NOT EXISTS nous_system.mcp_server_secrets (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT         NOT NULL,
    server_id    UUID         NOT NULL REFERENCES nous_system.mcp_servers (id) ON DELETE CASCADE,
    kind         TEXT         NOT NULL CHECK (kind IN ('headers', 'oauth')),
    key_id       TEXT         NOT NULL,
    nonce        BYTEA        NOT NULL,
    ciphertext   BYTEA        NOT NULL,
    header_names TEXT[]       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (server_id)
);
