-- 094: why a fact was deactivated, recorded on the fact row.
-- Until now only nous_system.consolidation_actions said why a fact went
-- inactive, and that table is pruned after 30 days. deactivated_at and
-- deactivation_reason are stamped when a fact goes from active to inactive
-- and cleared when it is reactivated. Both stay NULL on active facts and on
-- facts deactivated before this migration.
-- stale: the sleep stale scan. superseded: a newer fact replaced it.
-- merged: a sleep merge folded it into a combined fact. removed: a sleep
-- REMOVE verdict or a programmatic deactivate. user: an owner edit or
-- delete through the REST API. rollback: a backfill rollback script.
-- Plain statements, no DO block and no semicolon in a comment.
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS deactivation_reason TEXT;

ALTER TABLE heart.facts
    DROP CONSTRAINT IF EXISTS chk_facts_deactivation_reason;
ALTER TABLE heart.facts
    ADD CONSTRAINT chk_facts_deactivation_reason
    CHECK (deactivation_reason IS NULL
           OR deactivation_reason IN ('stale', 'superseded', 'merged', 'removed', 'user', 'rollback'));
