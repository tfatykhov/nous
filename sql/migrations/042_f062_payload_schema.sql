-- F062: typed spawn_sync — caller-supplied JSON Schema for the subtask's
-- result payload + post-execution validation flag.
--
-- Backward-compatible: both columns are nullable. Pre-flag rows
-- (subtask_payload_schema_enabled=False) leave both NULL. Schema-validated
-- runs persist the caller-supplied schema verbatim alongside a tri-state
-- pass/fail flag.
--
-- payload_schema_valid semantics:
--   NULL   — schema validation did not run (no schema supplied, flag off,
--            or final_outcome != 'completed').
--   TRUE   — payload validated against payload_schema; final_outcome='completed'.
--   FALSE  — payload failed schema validation; final_outcome='validation_failed'
--            (after retry exhaustion).

ALTER TABLE heart.subtasks
    ADD COLUMN IF NOT EXISTS payload_schema       JSONB,
    ADD COLUMN IF NOT EXISTS payload_schema_valid BOOLEAN;

COMMENT ON COLUMN heart.subtasks.payload_schema IS
    'F062: caller-supplied JSON Schema for submit_final_report.payload. NULL when spawn_sync was not invoked with a schema or NOUS_SUBTASK_PAYLOAD_SCHEMA_ENABLED=false.';
COMMENT ON COLUMN heart.subtasks.payload_schema_valid IS
    'F062: tri-state — NULL=not-run, TRUE=passed, FALSE=failed. Only set when payload_schema is non-NULL and final_outcome is completed or validation_failed.';
