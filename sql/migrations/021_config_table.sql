-- 021: Runtime config key-value store (F025 prep)
-- General-purpose runtime configuration table.  Agent-scoped overrides
-- belong in nous_system.agents.config JSONB, not here.

CREATE TABLE IF NOT EXISTS nous_system.config (
    key   VARCHAR PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON nous_system.config
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();
