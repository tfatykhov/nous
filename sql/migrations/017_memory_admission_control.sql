-- F023: Memory Admission Control (A-MAC)
-- Adds admission scoring columns to heart.facts.
-- Safe for existing installations: IF NOT EXISTS + DEFAULT values.
-- Existing facts get admission_score=NULL, recall_count=0, last_recalled_at=NULL.

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS admission_score FLOAT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS recall_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN heart.facts.admission_score IS
    'A-MAC composite score at time of admission. NULL for pre-F023 facts.';
COMMENT ON COLUMN heart.facts.recall_count IS
    'Number of times this fact was recalled and used in a response.';
COMMENT ON COLUMN heart.facts.last_recalled_at IS
    'Last time this fact was recalled and used.';
