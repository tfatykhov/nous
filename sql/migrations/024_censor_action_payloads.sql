-- F031: Censor middleware with action payloads
-- Adds trigger_action (JSONB), action_instruction (TEXT), unblock_pattern (TEXT) to censors

ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS trigger_action JSONB;
ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS action_instruction TEXT;
ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS unblock_pattern TEXT;

-- Record migration
INSERT INTO nous_system.schema_migrations (version, description)
VALUES (24, 'F031: censor action payloads')
ON CONFLICT (version) DO NOTHING;
