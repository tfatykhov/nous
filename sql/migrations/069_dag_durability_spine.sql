-- F087 DAG Durability Spine
--
-- Reaching a terminal status and having the result delivered become two
-- separate transitions. The orchestrator marks a DAG terminal as it does
-- today, then a sweep phase picks up terminal-but-undelivered DAGs and
-- delivers them. A crash between the two writes re-delivers on the next
-- tick instead of losing the notification, because the queue is a table
-- rather than in-memory state.
--
-- EventBus.emit drops on QueueFull and never blocks, so the bus can be a
-- delivery leg but never the durability mechanism. Hence these columns.

ALTER TABLE nous_system.execution_dags
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS delivery_error TEXT;

-- The sweep's exact predicate. Partial so it stays small: rows leave the
-- index permanently once delivered_at is set.
CREATE INDEX IF NOT EXISTS idx_execution_dags_undelivered
    ON nous_system.execution_dags (agent_id, completed_at)
    WHERE delivered_at IS NULL
      AND status IN ('completed', 'failed', 'partial', 'cancelled');

-- Token accounting idempotency guard. _sync_subtask_node is re-entrant
-- across ticks, so the running -> terminal edge is marked exactly once and
-- add_tokens is skipped on every re-entry.
ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS tokens_counted BOOLEAN NOT NULL DEFAULT false;
