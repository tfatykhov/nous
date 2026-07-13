-- 064: Write-path adjudication (R1 enumerative extraction + R2 store-time supersession).
-- Adds normalized conflict-slot keys, ordinal authority signal, and the
-- parametric-override marker to heart.facts. All columns nullable; existing
-- rows untouched. Partial index drives the R2.1 candidate lookup.
ALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS subject_key VARCHAR(200);
ALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS attribute_key VARCHAR(100);
ALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS source_ordinal BIGINT;
ALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS overrides_prior BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_facts_conflict_slot
    ON heart.facts (agent_id, subject_key, attribute_key)
    WHERE subject_key IS NOT NULL AND active = true;
