-- Migration 050: F067 — Episode Chunks (raw transcript chunks for retrieval)
--
-- Adds heart.episode_chunks: stores chunked raw transcript text alongside
-- the existing lossy fact extraction. Each chunk is independently embedded
-- and searchable, preserving verbatim tokens (names, numbers, exact quotes)
-- that the fact extractor drops during summarization.
--
-- Validated on LongMemEval per-question isolation methodology (+13pp QA
-- accuracy). NOT validated on shared-corpus retrieval — see
-- memory/project_lme_methodology_dependency for the constraint.
--
-- Feature-flagged via NOUS_EPISODE_CHUNKS_ENABLED (default false). When
-- the flag is off, this table stays empty and recall paths skip it.

CREATE TABLE IF NOT EXISTS heart.episode_chunks (
    id          UUID                     PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id    VARCHAR(100)             NOT NULL,
    episode_id  UUID                     NOT NULL
        REFERENCES heart.episodes(id) ON DELETE CASCADE,
    chunk_index INTEGER                  NOT NULL,
    content     TEXT                     NOT NULL,
    embedding   vector(1536)             NULL,
    -- Tsvector for keyword search; same recipe as heart.facts.search_tsv.
    search_tsv  tsvector                 GENERATED ALWAYS AS
                                          (to_tsvector('english'::regconfig, content))
                                         STORED,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Indexes:
--   1. agent_id filter (every recall scopes by agent_id)
--   2. episode_id lookup (cascading + group-by-episode display)
--   3. HNSW vector index (matches heart.facts / heart.episodes pattern)
--   4. GIN on tsvector for keyword fallback / hybrid search
--   5. UNIQUE on (episode_id, chunk_index) to make re-ingest idempotent
CREATE INDEX IF NOT EXISTS idx_episode_chunks_agent
    ON heart.episode_chunks(agent_id);
CREATE INDEX IF NOT EXISTS idx_episode_chunks_episode
    ON heart.episode_chunks(episode_id);
CREATE INDEX IF NOT EXISTS idx_episode_chunks_embedding
    ON heart.episode_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_episode_chunks_tsv
    ON heart.episode_chunks USING gin(search_tsv);
CREATE UNIQUE INDEX IF NOT EXISTS uq_episode_chunks_episode_index
    ON heart.episode_chunks(episode_id, chunk_index);

COMMENT ON TABLE heart.episode_chunks IS
    'F067: raw transcript chunks for retrieval. Co-stored with lossy fact extraction. Cascade-deleted with parent episode.';
COMMENT ON COLUMN heart.episode_chunks.chunk_index IS
    'Sequential index within the parent episode transcript (0-based).';
COMMENT ON COLUMN heart.episode_chunks.content IS
    'Verbatim transcript text, ~chunk_size chars per chunk_size/overlap settings.';
