-- F022 follow-up (2026-05-01): add session_id to heart.episodes so
-- get_active_episode_id() can fall back to a DB lookup when the
-- in-memory _active_episodes map is empty (e.g. after container restart).
--
-- The 2026-04-30 prod audit showed:
--   * 23% of agent-tool-injected facts still missed source_episode_id —
--     attributed to in-memory map wiped on restart.
--   * 100% of handler-internal facts (reflection, sleep_reflection,
--     inline_correction) missed source_episode_id — these paths bypass
--     runner injection entirely; they need explicit episode_id wiring.
--
-- This migration plus the codebase changes in the matching PR close
-- both gaps: session_id-tagged episodes provide a DB-backed answer to
-- "what's the active episode for this session?" so runner injection
-- survives restart, and handlers thread the episode_id through the
-- FactInput construction.

ALTER TABLE heart.episodes ADD COLUMN IF NOT EXISTS session_id VARCHAR(100);

CREATE INDEX IF NOT EXISTS idx_episodes_session_id
    ON heart.episodes(agent_id, session_id, started_at DESC)
    WHERE session_id IS NOT NULL;

COMMENT ON COLUMN heart.episodes.session_id IS
    'Conversation session that produced this episode. Lets get_active_episode_id fall back to a DB lookup after process restart.';
