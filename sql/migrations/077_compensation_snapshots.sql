-- Migration 077: Compensation snapshots (harness Phase 2.8)
--
-- Stores the prior state before a compensable tool call, linked to its
-- ledger row. The compensator reads this to undo the call on review.revert.

CREATE TABLE IF NOT EXISTS nous_system.compensation_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ledger_entry_id UUID NOT NULL,
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    snapshot_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverted_at TIMESTAMPTZ,
    revert_result TEXT
);

CREATE INDEX IF NOT EXISTS idx_compensation_snapshots_ledger
    ON nous_system.compensation_snapshots(ledger_entry_id);

CREATE INDEX IF NOT EXISTS idx_compensation_snapshots_agent
    ON nous_system.compensation_snapshots(agent_id, created_at DESC);

-- Harness Phase 2.8: undoable flag on DAG nodes (enforced at runtime).
ALTER TABLE nous_system.dag_nodes
    ADD COLUMN IF NOT EXISTS undoable BOOLEAN NOT NULL DEFAULT false;
