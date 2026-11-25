-- 124: nous_system.model_call_log.tool_loading, what a model call would have left out (F120, tool loading, watch only)
--
-- Written by the app's model-call recorder only while NOUS_TOOL_LOADING_ENABLED is on and the owner's setting is
-- Watch only for the call's kind. NULL on every other row and on every row written before this migration. Names and
-- counts only: the kind of call, how many tools the call sent and how many it would have left out, their estimated
-- tokens and those of the list of names that would replace them, the names kept in full, and whether the call read
-- the tools from the prompt cache or wrote them. No request changes with it.
--
-- The db_query reader may read it: model_call_log has no excluded column in scripts/ops/db_roles.sql, so the column
-- is granted the next time that script runs. No grant here.
-- Rollback is ALTER TABLE nous_system.model_call_log DROP COLUMN IF EXISTS tool_loading and deleting version 124 from
-- schema_migrations.

ALTER TABLE nous_system.model_call_log ADD COLUMN IF NOT EXISTS tool_loading JSONB;
