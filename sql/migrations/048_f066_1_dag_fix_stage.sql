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
-- Drop BOTH possible names (Postgres-auto from inline CHECK in F038
-- migration 032 + the ORM-defined name) to be safe; only one will exist
-- at any given time depending on the deployment history.
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS dag_nodes_node_type_check;
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_type;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_type
    CHECK (node_type IN ('subtask', 'check', 'gate', 'callback', 'fix'));

-- Step 2: extend status CHECK constraint to include 'skipped'.
-- Drop both possible names (mirrors the F033 pattern at
-- 033_dag_completion_check.sql).
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS dag_nodes_status_check;
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_status;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_status
    CHECK (status IN (
        'pending', 'ready', 'running', 'awaiting_check',
        'completed', 'failed', 'blocked', 'cancelled', 'skipped'
    ));

-- Step 2b: extend dag_edges edge_type CHECK constraint to include 'on_failure'.
ALTER TABLE nous_system.dag_edges
    DROP CONSTRAINT IF EXISTS dag_edges_edge_type_check;
ALTER TABLE nous_system.dag_edges
    DROP CONSTRAINT IF EXISTS chk_dag_edge_type;
ALTER TABLE nous_system.dag_edges
    ADD CONSTRAINT chk_dag_edge_type
    CHECK (edge_type IN ('dependency', 'cancel_cascade', 'context_flow', 'on_failure'));

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
