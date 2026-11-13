-- 109: the MCP client's servers and their tools (F112 PR 1)
--
-- nous_system.mcp_servers: one row per external MCP server the agent may use. name becomes the middle part of a
-- tool's name (mcp__<name>__<tool>), so it is short, lowercase and holds no double underscore. url is https only and
-- carries no credential (the client refuses a user, a password and a query string). transport is streamable_http
-- only in PR 1. background is the owner's switch "available to background work": with it off, only a chat with the
-- owner and an approved action may call the server's tools. call_timeout_seconds bounds one tool call.
--
-- nous_system.mcp_server_tools: one row per tool a server listed, as the owner approved it. tool is the server's own
-- name for it (sent back to the server as it is), description and input_schema are what the server listed.
-- permission: allow (registered at startup), deny (not registered), ask (not registered in PR 1: the approval path
-- comes later).
--
-- No secret column: PR 1 has no authentication. Writer: the seed command (python -m nous.mcp_client.seed), run by
-- the owner on the host. Reader: the app at startup. No reader grant, and scripts/ops/db_roles.sql is not touched.
-- Created by the application role through the migrator.

CREATE TABLE IF NOT EXISTS nous_system.mcp_servers (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id             TEXT         NOT NULL,
    name                 TEXT         NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,23}$' AND position('__' IN name) = 0),
    url                  TEXT         NOT NULL CHECK (left(url, 8) = 'https://'),
    transport            TEXT         NOT NULL DEFAULT 'streamable_http' CHECK (transport IN ('streamable_http')),
    enabled              BOOLEAN      NOT NULL DEFAULT TRUE,
    background           BOOLEAN      NOT NULL DEFAULT FALSE,
    call_timeout_seconds INT          NOT NULL DEFAULT 30 CHECK (call_timeout_seconds BETWEEN 1 AND 600),
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, name)
);

CREATE TABLE IF NOT EXISTS nous_system.mcp_server_tools (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT         NOT NULL,
    server_id    UUID         NOT NULL REFERENCES nous_system.mcp_servers (id),
    tool         TEXT         NOT NULL,
    description  TEXT         NOT NULL DEFAULT '',
    input_schema JSONB        NOT NULL,
    permission   TEXT         NOT NULL DEFAULT 'deny' CHECK (permission IN ('allow', 'deny', 'ask')),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, server_id, tool)
);
