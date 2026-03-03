-- 007: Agent Identity table + is_initiated flag on agents
-- Part of spec 008 (Agent Identity & Tiered Context Model)

-- Agent identity sections (character, values, protocols, preferences, boundaries)
CREATE TABLE IF NOT EXISTS nous_system.agent_identity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL REFERENCES nous_system.agents(id),
    section VARCHAR(50) NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(50),
    previous_version_id UUID REFERENCES nous_system.agent_identity(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_agent_section_current
    ON nous_system.agent_identity(agent_id, section) WHERE is_current = TRUE;

-- Initiation state flag on agents table
ALTER TABLE nous_system.agents ADD COLUMN IF NOT EXISTS is_initiated BOOLEAN DEFAULT FALSE;
