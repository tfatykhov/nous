-- 091: F101 schedule concurrency and catch-up policies
--
-- What a schedule does when it comes due while its previous run is still active, and when the
-- process was down across one or more of its slots (docs/features/F101-schedule-concurrency-catchup.md).
-- Read only when NOUS_SCHEDULE_POLICIES_ENABLED is on. With the flag off the scheduler ignores them.
--
-- concurrency_policy: coalesce_if_active (default) and skip_if_active spawn nothing while a run is
--   active, and log and emit an event naming that run. always_enqueue always spawns.
-- catch_up_policy: skip_missed (default) fires once for any number of missed slots.
--   enqueue_missed_with_cap fires once per missed slot, up to catch_up_cap (NULL: NOUS_SCHEDULE_CATCH_UP_CAP).
-- Existing rows get the defaults.
--
-- execution_dags.schedule_id: the schedule whose run created the DAG (a scheduled turn's dag_create),
--   so a DAG that outlives the subtask that launched it still counts as the schedule's active run.
--   NULL for every other DAG and for DAGs created before this migration.

ALTER TABLE heart.schedules
    ADD COLUMN IF NOT EXISTS concurrency_policy TEXT NOT NULL DEFAULT 'coalesce_if_active',
    ADD COLUMN IF NOT EXISTS catch_up_policy TEXT NOT NULL DEFAULT 'skip_missed',
    ADD COLUMN IF NOT EXISTS catch_up_cap INTEGER;

ALTER TABLE heart.schedules DROP CONSTRAINT IF EXISTS chk_schedule_concurrency_policy;
ALTER TABLE heart.schedules ADD CONSTRAINT chk_schedule_concurrency_policy
    CHECK (concurrency_policy IN ('coalesce_if_active', 'skip_if_active', 'always_enqueue'));

ALTER TABLE heart.schedules DROP CONSTRAINT IF EXISTS chk_schedule_catch_up_policy;
ALTER TABLE heart.schedules ADD CONSTRAINT chk_schedule_catch_up_policy
    CHECK (catch_up_policy IN ('skip_missed', 'enqueue_missed_with_cap'));

ALTER TABLE heart.schedules DROP CONSTRAINT IF EXISTS chk_schedule_catch_up_cap;
ALTER TABLE heart.schedules ADD CONSTRAINT chk_schedule_catch_up_cap
    CHECK (catch_up_cap IS NULL OR catch_up_cap BETWEEN 1 AND 25);

ALTER TABLE nous_system.execution_dags
    ADD COLUMN IF NOT EXISTS schedule_id UUID;

-- The scheduler's active-run read: one schedule's live DAGs.
CREATE INDEX IF NOT EXISTS idx_execution_dags_live_schedule
    ON nous_system.execution_dags (agent_id, schedule_id)
    WHERE schedule_id IS NOT NULL AND status IN ('pending', 'running');
