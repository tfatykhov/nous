-- Decision resolution: agent-facing outcome mutation (resolve_decision tool).
-- Consult + decision: live Nous decision 06d62894; FORGE a22f4ccc.
--
-- Widens brain.decisions.outcome to admit two non-prediction states so the
-- agent's session-time sweeps can drain the pending queue without polluting
-- calibration:
--   noise       -- sweep/heartbeat-tick artifact, never a real prediction
--   superseded  -- a later decision replaced this one (see superseded_by)
-- Both are EXCLUDED from the Brier/ECE denominator (nous/brain/calibration.py)
-- alongside 'pending' -- they are not failed predictions.
--
-- Adds superseded_by as a self-FK so a supersession records the canonical
-- decision that replaced this one (lineage, not connectivity).
--
-- The old inline CHECK from sql/init.sql is auto-named decisions_outcome_check;
-- the ORM (nous/storage/models.py) names it ck_decisions_outcome. Drop BOTH
-- names so the migration is idempotent regardless of how the table was created.
-- The new CHECK is a strict superset of the old, so every existing row passes.
--
-- NOTE: statements are kept splitter-safe for the auto-migrator (nous/storage/
-- migrator.py) -- no DO $$ blocks (the splitter does not understand dollar
-- quoting) and no BEGIN/COMMIT (run_migrations wraps every file in one tx).

ALTER TABLE brain.decisions DROP CONSTRAINT IF EXISTS decisions_outcome_check;

ALTER TABLE brain.decisions DROP CONSTRAINT IF EXISTS ck_decisions_outcome;

ALTER TABLE brain.decisions ADD CONSTRAINT ck_decisions_outcome
    CHECK (outcome IN ('pending', 'success', 'partial', 'failure', 'noise', 'superseded'));

ALTER TABLE brain.decisions ADD COLUMN IF NOT EXISTS superseded_by UUID DEFAULT NULL
    REFERENCES brain.decisions(id);

COMMENT ON COLUMN brain.decisions.superseded_by IS
    'id of the decision that replaced this one when outcome=superseded. Lineage marker (not a graph edge); NULL for every other outcome.';
