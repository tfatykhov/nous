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

-- delivery_summary caches an AGENT-AUTHORED summary across retries. Without
-- it, a required channel (Telegram) failing after the optional summary leg
-- succeeded would re-run a full LLM turn on every sweep -- up to
-- dag_delivery_max_attempts turns, plus a duplicate episode each time, for
-- one transient outage. The deterministic template is cheap and is never
-- cached here.
ALTER TABLE nous_system.execution_dags
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS delivery_error TEXT,
    ADD COLUMN IF NOT EXISTS delivery_summary TEXT;

-- Backfill BEFORE the queue semantics take effect. delivered_at is nullable,
-- so on an upgrade every historical terminal DAG would read as undelivered --
-- and since delivery is on by default, the next heartbeats would announce the
-- entire backlog to Telegram five at a time. Only outcomes that terminalize
-- AFTER this migration should be announced. completed_at is the honest
-- delivery timestamp for a historical row; created_at covers the rare
-- terminal row that never got one.
UPDATE nous_system.execution_dags
SET delivered_at = COALESCE(completed_at, created_at)
WHERE delivered_at IS NULL
  AND status IN ('completed', 'failed', 'partial', 'cancelled');

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
