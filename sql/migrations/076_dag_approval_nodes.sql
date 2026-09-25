-- Harness Phase 3: approval nodes wait durably on an answer from a person.
-- New node type approval and new status awaiting_input, plus the columns
-- that hold the authored question, the deadline and the answer on the node.
-- Drop both possible constraint names first (the 048 pattern): 032 created
-- the constraints inline, so Postgres named them itself.
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS dag_nodes_node_type_check;
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_type;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_type
    CHECK (node_type IN ('subtask', 'check', 'gate', 'callback', 'fix', 'approval'));

ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS dag_nodes_status_check;
ALTER TABLE nous_system.dag_nodes
    DROP CONSTRAINT IF EXISTS chk_dag_node_status;
ALTER TABLE nous_system.dag_nodes
    ADD CONSTRAINT chk_dag_node_status
    CHECK (status IN (
        'pending', 'ready', 'running', 'awaiting_check', 'awaiting_input',
        'completed', 'failed', 'blocked', 'cancelled', 'skipped'
    ));

-- answer_source says where an answer came from, not who gave it.
ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS approval_spec JSONB,
    ADD COLUMN IF NOT EXISTS answer_deadline TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS surface_id TEXT,
    ADD COLUMN IF NOT EXISTS answer TEXT,
    ADD COLUMN IF NOT EXISTS answered_by TEXT,
    ADD COLUMN IF NOT EXISTS answered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS answer_source TEXT
        CONSTRAINT chk_dag_node_answer_source
        CHECK (answer_source IN ('companion', 'deadline')),
    ADD COLUMN IF NOT EXISTS answer_history JSONB;

-- The sweep reads waiting nodes of terminal DAGs through this index,
-- never the whole DAG history.
CREATE INDEX IF NOT EXISTS idx_dag_nodes_awaiting_input
    ON nous_system.dag_nodes (dag_id)
    WHERE status = 'awaiting_input';
