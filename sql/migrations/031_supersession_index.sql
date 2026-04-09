-- F027: Partial index for stale scan query
CREATE INDEX IF NOT EXISTS idx_facts_stale_candidates
    ON heart.facts (agent_id, confidence, last_recalled_at)
    WHERE active = true AND superseded_by IS NOT NULL;
