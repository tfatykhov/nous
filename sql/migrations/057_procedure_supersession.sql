-- Procedure dedup Phase 0: supersession bookkeeping on heart.procedures.
-- Audit: docs/reviews/procedure-subsystem-audit-2026-06-06.md (§6 B1).
--
-- Closes the restart "resurrection loop": when a duplicate procedure is archived
-- (active=false), bootstrap_local_skills re-imports the skill,local SKILL.md and
-- reactivate_skills un-archives it on next boot, because both keyed off the active
-- flag alone. These columns let those paths key off superseded_by instead
-- (superseded_by IS NOT NULL = "deliberately consolidated -> redirect to canonical,
-- do NOT recreate/reactivate"), distinct from active=false-for-missing-requires.
--
-- Additive (nullable columns only) -> safe to deploy at any time. The
-- (agent_id, lower(name)) WHERE active uniqueness index ships in migration 058 and
-- must NOT deploy until the consolidation has removed the active duplicates.
--
-- NOTE: statements are kept splitter-safe for the auto-migrator (nous/storage/
-- migrator.py) — no DO $$ blocks (the splitter does not understand dollar quoting)
-- and no BEGIN/COMMIT (run_migrations wraps every file in one transaction).
-- superseded_by is a plain UUID, not an FK: procedures are never hard-deleted.

ALTER TABLE heart.procedures ADD COLUMN IF NOT EXISTS superseded_by UUID DEFAULT NULL;

ALTER TABLE heart.procedures ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_procedures_superseded_name
    ON heart.procedures (agent_id, name)
    WHERE superseded_by IS NOT NULL;

COMMENT ON COLUMN heart.procedures.superseded_by IS
    'Dedup Phase 0: id of the canonical procedure that absorbed this one during consolidation. NOT NULL = consolidated duplicate; bootstrap/reactivate must redirect to the canonical instead of recreating/reactivating this row.';

COMMENT ON COLUMN heart.procedures.archived_at IS
    'Dedup Phase 0: timestamp this procedure was archived by consolidation. Paired with superseded_by. NULL for live rows and for rows deactivated by other paths (e.g. missing requires).';
