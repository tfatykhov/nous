-- F064.5 (v1 partial): scheduled task Episode reuse.
--
-- Adds continuation columns to heart.schedules. v1 ships Episode reuse
-- only (each fire still starts a fresh LLM context — `runner.end_conversation`
-- pops the in-memory Conversation, so true thread continuity requires
-- explicit state serialization, deferred to F064.5-v2).
--
-- - continuation_turns: cap on consecutive fires that share an Episode.
--   0 (default) = disabled, every fire is a fresh session.
-- - continuation_session_id: stable session_id reused across fires. NULL
--   between continuation cycles and on first fire.
-- - continuation_prompt: reserved for F064.5-v2 (LLM thread continuity).
--   Not consumed by v1.
-- - continuation_count: tracks dispatches (not successes) within a
--   continuation cycle. Resets to 0 when the cycle hits continuation_turns.

ALTER TABLE heart.schedules
    ADD COLUMN IF NOT EXISTS continuation_turns INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS continuation_session_id TEXT,
    ADD COLUMN IF NOT EXISTS continuation_prompt TEXT,
    ADD COLUMN IF NOT EXISTS continuation_count INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN heart.schedules.continuation_turns IS
    'F064.5 v1: max consecutive fires that share the same Episode. 0 = disabled (default). Capped at NOUS_SCHEDULE_MAX_CONTINUATION_TURNS in code.';
COMMENT ON COLUMN heart.schedules.continuation_session_id IS
    'F064.5 v1: stable session_id reused across continuation fires. NULL between cycles. Schedule fires with this session_id → runner uses the existing Episode (Episode.session_id contract).';
COMMENT ON COLUMN heart.schedules.continuation_prompt IS
    'F064.5 v1: RESERVED for v2 (LLM thread continuity). Not read in v1 — every fire still sends the full task prompt because end_conversation pops the in-memory Conversation between fires.';
COMMENT ON COLUMN heart.schedules.continuation_count IS
    'F064.5 v1: dispatches within the current continuation cycle. Resets to 0 when cycle hits continuation_turns or continuation_session_id is null. Counts dispatches (not successes) — a failed fire still consumes a slot.';
