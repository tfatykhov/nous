-- 056_censor_correct_side.sql — F078 Correct-Side Censor Enforcement (v3)
-- Backward-compatible by design (BC invariant). This migration is functionally INERT.
-- It renames the action vocabulary and adds two columns. It creates NO new hard tier.
--   block    -> steer   (halts become advisory, wrongly-halted turns now run)
--   warn     -> steer   (already non-blocking, just the rename)
--   absolute -> abort   (absolute was a no-op on input, only rm -rf-class lives here)
-- The few genuinely-prohibitive censors (trading -> refuse, rm -rf -> abort) are
-- PROMOTED later by the gated triage script (scripts/migrate_censors_f078.py),
-- only after operator review of its --dry-run. Nothing here blocks anything new.
-- Idempotent: safe on fresh DB (no rows) and re-runnable via DROP/ADD ... IF EXISTS.
-- NOTE: no semicolon may appear inside these comment lines (the splitter breaks on it).

-- 1. Drop the old action CHECK so values can be remapped.
--    init.sql declares it inline, so Postgres auto-names it `censors_action_check`.
--    Later ORM/migration paths may name it `ck_censors_action`. Drop BOTH so this
--    migration is correct on a fresh DB (auto-named) and on any partially-migrated DB.
ALTER TABLE heart.censors DROP CONSTRAINT IF EXISTS ck_censors_action;
ALTER TABLE heart.censors DROP CONSTRAINT IF EXISTS censors_action_check;

-- 2. Mechanical, UNIVERSALLY-inert remap: every old tier -> steer (advisory).
--    Even 'absolute' (historically rm -rf-class, and a no-op on input today) maps to
--    steer so NO row can auto-become a hard tier. The one real 'rm -rf' censor is
--    promoted to 'abort' by the gated triage / operator UI, never automatically.
UPDATE heart.censors SET action = 'steer' WHERE action IN ('warn', 'block', 'absolute');

-- 3. New default plus new CHECK.
ALTER TABLE heart.censors ALTER COLUMN action SET DEFAULT 'steer';
ALTER TABLE heart.censors ADD CONSTRAINT ck_censors_action
    CHECK (action IN ('steer', 'refuse', 'abort'));

-- 4. Provenance drives the max-tier creation cap (auto<=steer, agent<=refuse, human<=abort).
--    Backfill is conservative here (existing rows default to 'human'). The gated triage
--    script reclassifies auto/agent precisely (an F039/Auto-created reason wins).
ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS provenance VARCHAR(20) NOT NULL DEFAULT 'human';
ALTER TABLE heart.censors DROP CONSTRAINT IF EXISTS ck_censors_provenance;
ALTER TABLE heart.censors ADD CONSTRAINT ck_censors_provenance
    CHECK (provenance IN ('auto', 'agent', 'human'));

-- 5. F068 refuse opt-out. When true, a refuse-tier censor does NOT strip tools.
ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS refuse_keep_tools BOOLEAN NOT NULL DEFAULT false;
