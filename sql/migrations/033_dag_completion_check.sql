-- F038.1: DAG Node Completion Check
-- Adds completion_check polling and awaiting_check status

-- New columns
ALTER TABLE nous_system.dag_nodes ADD COLUMN completion_check TEXT;
ALTER TABLE nous_system.dag_nodes ADD COLUMN completion_check_interval INTEGER;
ALTER TABLE nous_system.dag_nodes ADD COLUMN max_check_attempts INTEGER;
ALTER TABLE nous_system.dag_nodes ADD COLUMN check_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE nous_system.dag_nodes ADD COLUMN awaiting_check_at TIMESTAMPTZ;
ALTER TABLE nous_system.dag_nodes ADD COLUMN last_check_at TIMESTAMPTZ;

-- Update status CHECK constraint to include 'awaiting_check'
-- Drop both possible names: auto-generated from inline CHECK and ORM-defined name
ALTER TABLE nous_system.dag_nodes DROP CONSTRAINT IF EXISTS dag_nodes_status_check;
ALTER TABLE nous_system.dag_nodes DROP CONSTRAINT IF EXISTS chk_dag_node_status;
ALTER TABLE nous_system.dag_nodes ADD CONSTRAINT chk_dag_node_status
    CHECK (status IN ('pending', 'ready', 'running', 'awaiting_check', 'completed', 'failed', 'blocked', 'cancelled'));
