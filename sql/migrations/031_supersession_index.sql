-- F027: Composite index for stale scan query (Step 5)
-- Covers the WHERE predicate in _phase_stale_scan():
--   active = true AND superseded_by IS NOT NULL AND confidence < 0.5
-- The partial-index WHERE clause pre-filters ~99% of rows (most facts are not
-- superseded), making the stale scan cheap even on large agent fact tables.
CREATE INDEX IF NOT EXISTS idx_facts_stale_candidates
    ON heart.facts (agent_id, confidence, last_recalled_at)
    WHERE active = true AND superseded_by IS NOT NULL;
