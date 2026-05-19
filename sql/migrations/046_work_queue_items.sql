-- F064.6: work-queue ingress tracking table.
--
-- Records every item the work-queue adapter has seen, plus the DAG it
-- was dispatched to. `dispatched_at IS NULL` is the "claimed but not
-- yet dispatched" sentinel — the orchestrator's cross-tick reconciler
-- queries this state to recover orphan rows from partial-commit failures.

CREATE TABLE IF NOT EXISTS nous_system.work_queue_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- nullable on purpose. NULL = "claimed but not yet linked to a DAG".
    -- Reconciler at heartbeat/work_queue.py queries WHERE dispatched_at
    -- IS NULL AND created_at < now() - interval 5 min to recover orphans.
    dispatched_at   TIMESTAMPTZ NULL,
    dag_id          UUID NULL,
    terminal_state  TEXT NULL,
    payload         JSONB NULL,
    UNIQUE (agent_id, source, external_id)
);

-- Partial index supports the reconciler scan — only rows in the
-- claimed-but-not-yet-dispatched state matter.
CREATE INDEX IF NOT EXISTS idx_work_queue_items_undispatched
    ON nous_system.work_queue_items (agent_id, source)
    WHERE dispatched_at IS NULL;

-- Partial index supports the terminal-state cancel path.
CREATE INDEX IF NOT EXISTS idx_work_queue_items_dag_id
    ON nous_system.work_queue_items (dag_id)
    WHERE dag_id IS NOT NULL;

COMMENT ON TABLE nous_system.work_queue_items IS
    'F064.6: per-agent record of external work items the work-queue adapter has seen. Unique on (agent_id, source, external_id) so the same external item never produces two DAGs.';
COMMENT ON COLUMN nous_system.work_queue_items.dispatched_at IS
    'F064.6: NULL = claimed but not yet linked to a DAG (partial-commit sentinel). Set inside the same transaction as dag_create so commit-success is atomic.';
COMMENT ON COLUMN nous_system.work_queue_items.dag_id IS
    'F064.6: the DAG dispatched for this item. NULL until mark_dispatched commits in the same transaction as dag_create.';
COMMENT ON COLUMN nous_system.work_queue_items.terminal_state IS
    'F064.6: the adapter-supplied terminal state name when the source marks the item done. Set by mark_terminal AFTER cancel_dag succeeds, so a failed cancel forces a retry next tick.';
