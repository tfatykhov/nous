-- 111: an MCP server's address may be http as well as https (F112)
--
-- 109 held nous_system.mcp_servers.url to https. The MCP client now takes an http address for a host the owner
-- named in NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS (a server on the owner's own network that serves no TLS). The table
-- cannot know that list, so the check here is the scheme alone, http or https, and the application holds the rest:
-- the routes refuse an http address for a host that is not listed, and the client refuses to open a stored one whose
-- host has left the list. Every other scheme is still refused here.
--
-- Idempotent: the check is dropped if present and added again, so a second run ends with the same check. No row can
-- fail the new check (every existing row passed the stricter one). No grant.

ALTER TABLE nous_system.mcp_servers DROP CONSTRAINT IF EXISTS mcp_servers_url_check;

ALTER TABLE nous_system.mcp_servers
    ADD CONSTRAINT mcp_servers_url_check CHECK (left(url, 8) = 'https://' OR left(url, 7) = 'http://');
