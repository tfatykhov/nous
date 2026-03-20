-- F021.1: Per-dimension admission scores for dashboard analytics.
-- Stores {utility, confidence, novelty, recency, type_prior} at admission time.
-- NULL for pre-migration facts and bypassed facts.

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS admission_scores JSONB DEFAULT NULL;

COMMENT ON COLUMN heart.facts.admission_scores IS
    'Per-dimension A-MAC scores at admission time. {utility, confidence, novelty, recency, type_prior}. NULL for pre-migration facts and bypassed facts.';
