-- 120: a goal that works once a day, from a time of day (brain.goals.daily_at_minute)
--
-- daily_at_minute is a minute of the day, 0 to 1439, in the one time zone the server is set to (NOUS_GOALS_TIMEZONE).
-- With goal pacing on, the goal driver starts at most one timer session of such a goal per day of that zone, not
-- before that minute. NULL: the goal runs as often as its limits allow, which every existing row keeps. With pacing
-- off the column is not read. The constraint is dropped before it is added, so the file applies twice.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Created by the
-- application role through the migrator.

ALTER TABLE brain.goals ADD COLUMN IF NOT EXISTS daily_at_minute INTEGER;
ALTER TABLE brain.goals DROP CONSTRAINT IF EXISTS chk_goals_daily_at_minute;
ALTER TABLE brain.goals ADD CONSTRAINT chk_goals_daily_at_minute
    CHECK (daily_at_minute IS NULL OR daily_at_minute BETWEEN 0 AND 1439);
