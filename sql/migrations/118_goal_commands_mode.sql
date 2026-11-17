-- 118: a goal whose tasks may run any command until an end time (brain.goals.mode = 'commands')
--
-- mode_hours is the number of hours the owner chose when creating the goal, 1 to 2160. mode = 'commands' with
-- mode_until NULL is a draft's choice: the end time is set when the goal is approved, never later than its deadline.
-- 'contained' has neither. A later mode is another value of chk_goals_mode_value. Every existing row is 'contained'
-- with both NULL. Each constraint is dropped before it is added, so the file applies twice.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Created by the
-- application role through the migrator.

ALTER TABLE brain.goals ADD COLUMN IF NOT EXISTS mode_hours INTEGER
    CHECK (mode_hours IS NULL OR mode_hours BETWEEN 1 AND 2160);
ALTER TABLE brain.goals DROP CONSTRAINT IF EXISTS goals_mode_check;
ALTER TABLE brain.goals DROP CONSTRAINT IF EXISTS chk_goals_mode_value;
ALTER TABLE brain.goals ADD CONSTRAINT chk_goals_mode_value CHECK (mode IN ('contained', 'commands'));
ALTER TABLE brain.goals DROP CONSTRAINT IF EXISTS chk_goals_mode;
ALTER TABLE brain.goals ADD CONSTRAINT chk_goals_mode CHECK (
    (mode = 'contained' AND mode_hours IS NULL AND mode_until IS NULL)
    OR (mode = 'commands' AND mode_hours IS NOT NULL)
);
ALTER TABLE brain.goals DROP CONSTRAINT IF EXISTS chk_goals_mode_end;
ALTER TABLE brain.goals ADD CONSTRAINT chk_goals_mode_end CHECK (
    mode_until IS NULL OR deadline_at IS NULL OR mode_until <= deadline_at
);
