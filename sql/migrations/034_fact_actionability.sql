-- F047: Actionability classification at learn time.
-- Adds two nullable columns and a partial index on heart.facts.
-- NULL = not yet classified (legacy rows; backfill handler will populate).
-- Partial index optimizes the "find actionable facts" query used by heartbeat.

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS actionable BOOLEAN DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS actionable_confidence REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_facts_actionable_agent
    ON heart.facts(agent_id, actionable)
    WHERE actionable = TRUE;

COMMENT ON COLUMN heart.facts.actionable IS
    'F047: True=pending action, False=observation/resolved, NULL=unclassified';
COMMENT ON COLUMN heart.facts.actionable_confidence IS
    'F047: Classifier confidence 0.0-1.0';
