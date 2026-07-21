-- F086 ICL exemplar mode - retrieval indexes for source-filtered cosine fetch
-- Partial HNSW keeps the exemplar walk off the global embedding index
CREATE INDEX IF NOT EXISTS idx_facts_exemplar_embedding
    ON heart.facts USING hnsw (embedding vector_cosine_ops)
    WHERE source = 'exemplar_extractor';

CREATE INDEX IF NOT EXISTS idx_facts_exemplar_agent
    ON heart.facts (agent_id)
    WHERE source = 'exemplar_extractor' AND active = true;
