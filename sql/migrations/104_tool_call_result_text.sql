-- 104: the full result of a tool call (F108 agent work view, full result)
--
-- tool_call_log.result_text: the call's result as the tool returned it, before SmartCompress, redacted and cut at
-- 20,000 characters (head and tail kept, a marker between). It is written only when the 500-character result_preview
-- was cut, so a short result has none, and an error call has none (its error_text holds the message). The recorder
-- writes it with the row. The retention loop sets it to NULL once the row is older than
-- NOUS_MODEL_CALL_TEXT_RETENTION_DAYS (7), and the row itself goes with the rest of tool_call_log after
-- NOUS_TOOL_CALL_RETENTION_DAYS (30). The dashboard reads it for one call at a time.
-- Writer: the app. Reader: the app only. The db_query reader does not read this column: scripts/ops/db_roles.sql lists
-- it as excluded from the tool_call_log grant, so run that script again after this migration (never before: it stops
-- on an excluded column that does not exist yet).
-- Nullable, no default: adding it rewrites nothing.

ALTER TABLE nous_system.tool_call_log ADD COLUMN IF NOT EXISTS result_text TEXT;
