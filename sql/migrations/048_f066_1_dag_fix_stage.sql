-- Migration 048: F066.1 — DAG Fix-Stage Recovery (Phase 1)
--
-- Adds the `fix` node type, the `skipped` terminal state, and per-fix
-- columns (parent_node, fix_actions, max_fix_attempts, fix_attempts_used,
-- expected_modes) to dag_nodes.
--
-- Phase 1 ships free-form LLM dispatch only. Typed-dispatch via
-- expected_modes lands in Phase 2 (the column is added now for forward
-- compatibility).

BEGIN;

-- Step 1: extend node_type CHECK constraint to include 'fix'.
-- The DROP + ADD pattern takes a brief AccessExclusiveLock. Acceptable
-- here because the dag_nodes table is bounded (small in prod).
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_type;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_type
    CHECK (node_type IN ('subtask', 'check', 'gate', 'callback', 'fix'));

-- Step 2: extend status CHECK constraint to include 'skipped'.
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_status;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_status
    CHECK (status IN (
        'pending', 'ready', 'running', 'awaiting_check',
        'completed', 'failed', 'blocked', 'cancelled', 'skipped'
    ));

-- Step 3: new fix-stage columns.
ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS parent_node        VARCHAR(100),
    ADD COLUMN IF NOT EXISTS fix_actions        JSONB,
    ADD COLUMN IF NOT EXISTS max_fix_attempts   INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS fix_attempts_used  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS expected_modes     JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Step 4: index for fast fix-child lookup when a parent fails.
CREATE INDEX IF NOT EXISTS idx_dag_nodes_parent_node
    ON nous_system.dag_nodes (dag_id, parent_node)
    WHERE parent_node IS NOT NULL;

COMMIT;
