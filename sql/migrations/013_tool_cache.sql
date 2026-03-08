-- 014.2: Tool result cache for ReversibleCache (F020)
CREATE TABLE IF NOT EXISTS heart.tool_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    hash_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input JSONB,
    original_content TEXT NOT NULL,
    compressed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    item_count INT,
    UNIQUE(session_id, hash_key)
);

CREATE INDEX IF NOT EXISTS idx_tool_cache_session ON heart.tool_cache(session_id);
