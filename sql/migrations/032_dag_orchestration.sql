-- F038: Unified DAG Orchestration
BEGIN;

CREATE TABLE IF NOT EXISTS nous_system.execution_dags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'partial')),
    source VARCHAR(30) NOT NULL DEFAULT 'conversation'
        CHECK (source IN ('conversation', 'critic', 'heartbeat', 'schedule')),
    original_request TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    token_budget INT,
    tokens_consumed INT NOT NULL DEFAULT 0,
    result_summary TEXT,
    postmortem JSONB,
    CONSTRAINT chk_dag_budget CHECK (token_budget IS NULL OR token_budget > 0)
);
CREATE INDEX idx_dags_agent_status ON nous_system.execution_dags (agent_id, status);
CREATE INDEX idx_dags_created ON nous_system.execution_dags (created_at DESC);

CREATE TABLE IF NOT EXISTS nous_system.dag_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dag_id UUID NOT NULL REFERENCES nous_system.execution_dags(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    node_type VARCHAR(20) NOT NULL
        CHECK (node_type IN ('subtask', 'check', 'gate', 'callback')),
    subtask_id UUID,
    check_name VARCHAR(200),
    wave INT NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ready', 'running', 'completed', 'failed', 'blocked', 'cancelled')),
    instructions TEXT,
    tools JSONB,
    frame_type VARCHAR(30),
    model VARCHAR(100),
    timeout_seconds INT NOT NULL DEFAULT 120,
    completion_condition VARCHAR(100),
    result TEXT,
    error TEXT,
    tokens_used INT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    injected_context TEXT,
    CONSTRAINT uq_dag_node_name UNIQUE (dag_id, name)
);
CREATE INDEX idx_dag_nodes_dag ON nous_system.dag_nodes (dag_id);
CREATE INDEX idx_dag_nodes_status ON nous_system.dag_nodes (dag_id, status);
CREATE INDEX idx_dag_nodes_subtask ON nous_system.dag_nodes (subtask_id) WHERE subtask_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS nous_system.dag_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dag_id UUID NOT NULL REFERENCES nous_system.execution_dags(id) ON DELETE CASCADE,
    from_node_id UUID NOT NULL REFERENCES nous_system.dag_nodes(id) ON DELETE CASCADE,
    to_node_id UUID NOT NULL REFERENCES nous_system.dag_nodes(id) ON DELETE CASCADE,
    edge_type VARCHAR(20) NOT NULL DEFAULT 'dependency'
        CHECK (edge_type IN ('dependency', 'cancel_cascade', 'context_flow')),
    CONSTRAINT uq_dag_edge UNIQUE (dag_id, from_node_id, to_node_id, edge_type)
);
CREATE INDEX idx_dag_edges_dag ON nous_system.dag_edges (dag_id);
CREATE INDEX idx_dag_edges_to ON nous_system.dag_edges (to_node_id);

COMMIT;
