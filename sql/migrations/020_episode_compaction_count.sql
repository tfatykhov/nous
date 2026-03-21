-- 020_episode_compaction_count.sql
-- Issue #169: Track compaction count per episode instead of creating new episodes

-- 1. Add compaction_count column
ALTER TABLE heart.episodes ADD COLUMN IF NOT EXISTS compaction_count INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN heart.episodes.compaction_count IS 'Number of conversation compactions during this episode lifetime';

-- 2. Clean up existing compaction pollution edges.
DELETE FROM brain.graph_edges
WHERE source_type = 'episode'
  AND target_type = 'episode'
  AND source_id IN (
    SELECT id FROM heart.episodes WHERE trigger = 'compaction'
  );

-- 3. Deactivate orphaned compaction stub episodes
UPDATE heart.episodes
SET active = false
WHERE trigger = 'compaction'
  AND summary = 'Continuation after conversation compaction'
  AND id NOT IN (SELECT episode_id FROM heart.episode_decisions);
