-- F023: Memory Admission Control (A-MAC)
-- Adds admission scoring columns to heart.facts.
-- Safe for existing installations: IF NOT EXISTS + DEFAULT values.
-- Existing facts get admission_score=NULL, recall_count=0, last_recalled_at=NULL.

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS admission_score FLOAT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS recall_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ DEFAULT NULL;
