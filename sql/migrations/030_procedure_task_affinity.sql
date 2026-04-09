-- F037: Utility-Boosted Procedure Retrieval
-- Tracks per-frame-type activation and outcome counts for procedures.
-- Used to compute affinity boosts during hybrid search scoring.
CREATE TABLE heart.procedure_task_affinity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    procedure_id UUID NOT NULL REFERENCES heart.procedures(id) ON DELETE CASCADE,
    frame_type TEXT NOT NULL,
    activation_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_activated_at TIMESTAMPTZ,
    agent_id TEXT NOT NULL,
    UNIQUE(procedure_id, frame_type, agent_id)
);

CREATE INDEX idx_proc_task_affinity_proc ON heart.procedure_task_affinity(procedure_id);
CREATE INDEX idx_proc_task_affinity_agent ON heart.procedure_task_affinity(agent_id);
