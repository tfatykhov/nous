-- F064.4 (v1 partial): workflow-as-code skill manifest fields.
--
-- Adds runtime_metadata JSONB on heart.procedures for the new SkillManifest
-- fields: concurrency_cap, timeout_override_seconds, hooks, requires_human_review.
-- These are persisted unconditionally at parse time (no flag gate on the
-- write path) so the silent-drop pattern (skill author declares a field,
-- gets a success response, field silently lost) cannot occur. The
-- NOUS_SKILL_RUNTIME_METADATA_ENABLED flag gates only the deferred
-- F064.4-v2 orchestrator consumer.

ALTER TABLE heart.procedures
    ADD COLUMN IF NOT EXISTS runtime_metadata JSONB;

COMMENT ON COLUMN heart.procedures.runtime_metadata IS
    'F064.4: skill runtime hints (concurrency_cap, timeout_override_seconds, hooks, requires_human_review). NULL for pre-flag procedures. Always persisted at parse time; consumer is gated by NOUS_SKILL_RUNTIME_METADATA_ENABLED (deferred to F064.4-v2).';
