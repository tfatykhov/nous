-- 112: whether an MCP server's images may reach the model (F112)
--
-- An image in a tool's result was the words "[image omitted]". nous_system.mcp_servers.images is the owner's switch
-- for one server: when true, the PNG and JPEG images of its results go to the model inside the tool's result, each
-- decoded and encoded again by the application first. False for every existing row and for a new one: an image can
-- hold what no text check sees (instructions drawn as pixels, whatever is on a screen), so it is let through for a
-- server only when the owner says so, on the Integrations screen.
--
-- Idempotent: the column is added only if it is not there. No grant: the table's existing grants cover it.

ALTER TABLE nous_system.mcp_servers ADD COLUMN IF NOT EXISTS images BOOLEAN NOT NULL DEFAULT FALSE;
