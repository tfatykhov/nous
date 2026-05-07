-- F061: Subtask Hardening — schema additions on heart.subtasks.
--
-- Backward-compatible: all new columns NULL or DEFAULT'd. Existing rows
-- (status / result / error) are unchanged. Cross-schema FK to
-- nous_system.dag_nodes; selective `pg_dump --schema=heart` restores
-- require nous_system.dag_nodes to exist first.
--
-- CHECK constraint on final_outcome is deferred to a follow-up migration
-- (042) after pre-flag rows are backfilled.

ALTER TABLE heart.subtasks
    ADD COLUMN IF NOT EXISTS report_jsonb     JSONB,
    ADD COLUMN IF NOT EXISTS final_outcome    VARCHAR(32),
    ADD COLUMN IF NOT EXISTS attempts         INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tokens_in        INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tokens_out       INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tool_calls_made  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_format    TEXT,
    ADD COLUMN IF NOT EXISTS success_criteria TEXT,
    ADD COLUMN IF NOT EXISTS dag_node_id      UUID NULL;

-- FK idempotency: PostgreSQL has no `ADD CONSTRAINT IF NOT EXISTS`. Drop
-- first, then add. Convention matches sql/migrations/016 and 033.
ALTER TABLE heart.subtasks
    DROP CONSTRAINT IF EXISTS fk_subtasks_dag_node;
ALTER TABLE heart.subtasks
    ADD CONSTRAINT fk_subtasks_dag_node
    FOREIGN KEY (dag_node_id) REFERENCES nous_system.dag_nodes(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_subtasks_outcome
    ON heart.subtasks (final_outcome, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_subtasks_dag_node
    ON heart.subtasks (dag_node_id) WHERE dag_node_id IS NOT NULL;

COMMENT ON COLUMN heart.subtasks.report_jsonb IS
    'F061: validated SubtaskReport payload from submit_final_report tool. NULL for pre-flag rows.';
COMMENT ON COLUMN heart.subtasks.final_outcome IS
    'F061: outcome enum: completed, incomplete_blocked, incomplete_no_terminal, validation_failed, timed_out, errored, cancelled. NULL for pre-flag rows.';
COMMENT ON COLUMN heart.subtasks.attempts IS
    'F061: total attempts including the original (1=no retry, 2=one retry).';
COMMENT ON COLUMN heart.subtasks.dag_node_id IS
    'F061: reverse link to nous_system.dag_nodes for per-node subtask history.';
