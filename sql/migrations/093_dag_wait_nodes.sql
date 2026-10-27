-- 093: typed waits for lineage DAGs.
-- New node type wait: the orchestrator evaluates a typed condition (a Claude
-- Code job, a pull request or a workspace file) in-process, with no model and
-- no shell. wait_for holds that condition. The poll cadence and clock reuse
-- the completion_check columns, and the node waits in awaiting_check, so no
-- status changes. Plain statements, no DO block and no semicolon in a comment.
-- Drop both possible constraint names first (the 076 pattern).
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS dag_nodes_node_type_check;
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_type;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_type
    CHECK (node_type IN ('subtask', 'check', 'gate', 'callback', 'fix', 'approval', 'wait'));

ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS wait_for JSONB;
