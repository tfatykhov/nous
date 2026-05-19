-- F064.1 + F064.2: DAG schema additions (folded into one migration so the
-- ORM never precedes its DB column).
--
-- F064.1 adds last_activity_at on dag_nodes for stall-timeout enforcement.
-- Updated by runner._tool_loop on every iteration boundary inside a subtask
-- (fire-and-forget under asyncio.shield); read by
-- orchestrator._check_stalled_nodes once per tick.
--
-- F064.2 adds max_concurrent_by_frame_type JSONB on execution_dags for
-- per-frame-type dispatch caps. Read by orchestrator._dispatch_ready_nodes
-- when NOUS_DAG_FRAME_CONCURRENCY_ENABLED is true.
--
-- Both columns are backward-compatible: nullable, no default. Existing rows
-- carry NULL; the stall scan treats NULL last_activity_at as "no ping yet,
-- fall back to wall-clock timeout" (see plan §4.3 NULL-fallback policy).

ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stall_timeout_seconds INTEGER;

ALTER TABLE nous_system.execution_dags
    ADD COLUMN IF NOT EXISTS max_concurrent_by_frame_type JSONB;

-- Partial index supports the orchestrator stall scan:
-- WHERE status='running' AND last_activity_at IS NOT NULL
-- AND last_activity_at < (now() - stall_timeout). Partial WHERE clause
-- keeps the index small (only running nodes matter for stall detection).
CREATE INDEX IF NOT EXISTS idx_dag_nodes_last_activity_running
    ON nous_system.dag_nodes (last_activity_at)
    WHERE status = 'running';

COMMENT ON COLUMN nous_system.dag_nodes.last_activity_at IS
    'F064.1: per-node activity timestamp updated by runner._tool_loop on every iteration boundary. Read by orchestrator stall scan. NULL means no ping yet (orchestrator treats as not-stalled; wall-clock timeout is the fallback).';
COMMENT ON COLUMN nous_system.dag_nodes.stall_timeout_seconds IS
    'F064.1: per-node stall timeout (seconds). NULL or 0 = stall detection disabled for this node (today is default behavior). Clamped at insert to NOUS_DAG_NODE_MAX_STALL_TIMEOUT.';
COMMENT ON COLUMN nous_system.execution_dags.max_concurrent_by_frame_type IS
    'F064.2: per-DAG per-frame-type dispatch cap dict {frame_type: max_concurrent}. NULL means no per-DAG cap (use env override or unlimited). Read by orchestrator._dispatch_ready_nodes when NOUS_DAG_FRAME_CONCURRENCY_ENABLED is true.';
