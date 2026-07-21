-- F086 ICL exemplar mode - retrieval indexes for source-filtered cosine fetch
-- Partial HNSW keeps the exemplar walk off the global embedding index.
-- Codex r7: the predicate includes active = true so a rollback-deactivated
-- exemplar leaves the ANN candidate horizon -- ANN is approximate, and
-- inactive near-duplicates would otherwise fill the horizon and starve the
-- read leg of ACTIVE examples after a rollback + re-backfill. DROP + CREATE
-- so a pre-merge dev DB that already applied the old (active-less) predicate
-- self-heals on a manual re-run; the migrator tracks by filename, so DBs that
-- already recorded 066 will NOT rerun it automatically (see the feature doc's
-- Backfill Runbook for the manual re-create note).
DROP INDEX IF EXISTS heart.idx_facts_exemplar_embedding;

CREATE INDEX IF NOT EXISTS idx_facts_exemplar_embedding
    ON heart.facts USING hnsw (embedding vector_cosine_ops)
    WHERE source = 'exemplar_extractor' AND active = true;

CREATE INDEX IF NOT EXISTS idx_facts_exemplar_agent
    ON heart.facts (agent_id)
    WHERE source = 'exemplar_extractor' AND active = true;
