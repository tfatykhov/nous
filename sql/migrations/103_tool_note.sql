-- 103: tool notes (F108 agent work view, Phase 3)
--
-- tool_note: one row per note about a repeated tool mistake, and also one row per pattern the review declined or
-- reported to the owner as a guard (inactive, empty note_text, retired = {reason: declined or guard}), so that a
-- pattern is not offered to the review again for 14 days. A pattern id is kind:tool:sha1 of the key, the same as the
-- detection code builds, and is not stored: the row keeps tool_name, pattern_kind and pattern_key.
-- Caps count the rows with active and a non-empty note_text: 3 per tool, 40 in all, 300 characters each (the CHECK
-- holds the length, the store holds the counts in the transaction that writes). A retired note is soft deleted
-- (active false, retired_at set), never removed. retired holds reason (no_improvement, unused, rewritten, declined,
-- guard), at and, for a judged note, after. judged holds the one judgement of a kept note. baseline_* is the pattern's
-- rate over the 14 days before created_at.
--
-- note_text is the model's own words after redaction. No column is named like a credential.
-- Writer: the app. Readers: the app only. There is no reader grant and scripts/ops/db_roles.sql is not touched.
-- Created by the application role through the migrator: the table and its indexes, nothing else.

CREATE TABLE IF NOT EXISTS nous_system.tool_note (
    id             UUID              PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id       TEXT              NOT NULL,
    tool_name      TEXT              NOT NULL,
    pattern_kind   TEXT              NOT NULL,
    pattern_key    TEXT              NOT NULL DEFAULT '',
    note_text      TEXT              NOT NULL DEFAULT '' CHECK (char_length(note_text) <= 300),
    baseline_rate  DOUBLE PRECISION  NOT NULL,
    baseline_calls INT               NOT NULL,
    baseline_hits  INT               NOT NULL,
    active         BOOLEAN           NOT NULL DEFAULT TRUE,
    retired        JSONB,
    judged         JSONB,
    created_at     TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    retired_at     TIMESTAMPTZ
);

-- At most one active note per pattern.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_note_active_pattern
    ON nous_system.tool_note (agent_id, tool_name, pattern_kind, pattern_key)
    WHERE active;

-- The newest rows of an agent: the re-review check and the active list.
CREATE INDEX IF NOT EXISTS idx_tool_note_agent_created
    ON nous_system.tool_note (agent_id, created_at DESC);
