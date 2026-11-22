-- 119: heart.pending_resolutions, why a stale pending item stopped surfacing (F116 PR 1)
--
-- One row per fact or episode that a heartbeat layer, the learn_fact tool or the owner closed, so the self-initiated
-- check does not raise it as "Pending action" or "Unresolved commitment" again. subject_id is a heart.facts or a
-- heart.episodes id (subject_kind says which), with no foreign key because it points at one of two tables and memory
-- is soft-deleted. evidence holds the referents and their states, prior_actionable and note_fact_id. review_by is when
-- the close expires. A suppression note always has one (60 days by default), a close on a terminal referent may not.
-- Reopening sets reopened_at. One resolution per item and agent. Additive: heart.facts is not changed.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Rollback while no layer
-- writes it is DROP TABLE IF EXISTS heart.pending_resolutions and deleting version 119 from schema_migrations.

CREATE TABLE IF NOT EXISTS heart.pending_resolutions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT        NOT NULL,
    subject_kind TEXT        NOT NULL CHECK (subject_kind IN ('fact', 'episode')),
    subject_id   UUID        NOT NULL,
    reason       TEXT        NOT NULL CHECK (reason IN ('referents_terminal', 'superseded', 'suppressed_by_note',
                                                        'age_cap', 'owner_closed')),
    evidence     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_by   TEXT        NOT NULL CHECK (created_by IN ('heartbeat', 'tool', 'owner')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_by    TIMESTAMPTZ,
    reopened_at  TIMESTAMPTZ,
    CONSTRAINT chk_pending_resolutions_note_expires CHECK (reason <> 'suppressed_by_note' OR review_by IS NOT NULL),
    CONSTRAINT uq_pending_resolutions_subject UNIQUE (agent_id, subject_kind, subject_id)
);
