-- 077: add kind column to heart.procedures for strategy cards (Reasoning Maps L1)
-- kind='strategy' marks distilled strategy cards; NULL = normal procedure
ALTER TABLE heart.procedures ADD COLUMN IF NOT EXISTS kind VARCHAR(100) NULL;

CREATE INDEX IF NOT EXISTS idx_procedures_kind
    ON heart.procedures (agent_id, kind)
    WHERE kind IS NOT NULL;
