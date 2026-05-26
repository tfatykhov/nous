-- Migration 052: F069 Phase 1 — document-aware ingestion
-- Adds two columns to heart.episode_chunks that let the chunk surface
-- distinguish dialogue transcripts (existing F067 path) from document
-- bodies ingested via the new ingest_document tool.
--
-- source_kind: 'dialogue' (default, existing F067 rows) or 'document'
--              (rows produced by ingest_document with the structure-aware
--              chunker at 1500-char target / 200-char overlap). Reserve
--              'code' for a future code-chunker variant.
--
-- source_ref:  for 'document' rows, the URL or workspace path the agent
--              passed when calling ingest_document. Nullable for dialogue
--              rows (transcript chunks have no external referent).
--
-- Backfill: every existing row gets DEFAULT 'dialogue' implicitly via
-- the column DEFAULT. No explicit UPDATE needed — the table only contains
-- F067 transcript chunks at the time this migration runs.
--
-- Index: (agent_id, source_kind) supports the future per-kind retrieval
-- weighting (F069 Phase 3) without scanning every chunk row.

BEGIN;

ALTER TABLE heart.episode_chunks
    ADD COLUMN IF NOT EXISTS source_kind VARCHAR(32) NOT NULL DEFAULT 'dialogue'
        CHECK (source_kind IN ('dialogue', 'document', 'code'));

ALTER TABLE heart.episode_chunks
    ADD COLUMN IF NOT EXISTS source_ref TEXT;

CREATE INDEX IF NOT EXISTS idx_episode_chunks_source_kind
    ON heart.episode_chunks(agent_id, source_kind);

COMMIT;
