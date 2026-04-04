-- F025 P3-C: Add transcript column to episodes for full-text persistence.
-- Nullable TEXT — only populated for episodes closed after this migration.

ALTER TABLE heart.episodes
    ADD COLUMN IF NOT EXISTS transcript TEXT;

COMMENT ON COLUMN heart.episodes.transcript IS 'F025: Raw conversation transcript, populated on episode close';
