-- Migration 083: Intentions (F099 Phase 1)
--
-- One row per spawn of background work: why it was started, where it was
-- started from, and what should happen to its result (wake_policy). The
-- store that creates the work row writes it in the same transaction.
-- Phase 1 only records and closes (close_reason 'legacy'): no result is
-- routed by these rows yet.
--
-- root_id, parent_id and depth are the lineage. A root has root_id = id.
-- root_cancelled_at and root_expired_at are set on root rows only.
-- source_id is TEXT: a subtask, DAG or schedule id.

CREATE TABLE IF NOT EXISTS brain.intentions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    root_id UUID NOT NULL REFERENCES brain.intentions(id),
    parent_id UUID REFERENCES brain.intentions(id),
    depth INTEGER NOT NULL DEFAULT 0,
    source_kind VARCHAR(20) NOT NULL,
    source_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    origin_kind VARCHAR(40) NOT NULL,
    origin_session_id TEXT,
    origin_channel TEXT,
    origin_decision_id UUID,
    wake_policy VARCHAR(20) NOT NULL,
    authority VARCHAR(20) NOT NULL DEFAULT 'owner',
    expected_result TEXT,
    assumptions JSONB,
    deadline TIMESTAMPTZ,
    state VARCHAR(20) NOT NULL DEFAULT 'pending',
    close_reason VARCHAR(20),
    root_cancelled_at TIMESTAMPTZ,
    root_expired_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    claim_token UUID,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    result_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_intentions_source UNIQUE (agent_id, source_kind, source_id),
    CONSTRAINT chk_intentions_source_kind CHECK (source_kind IN ('subtask', 'dag', 'schedule')),
    CONSTRAINT chk_intentions_wake_policy CHECK (wake_policy IN ('continue', 'remember', 'report', 'none', 'container')),
    CONSTRAINT chk_intentions_authority CHECK (authority IN ('owner', 'internal_only')),
    CONSTRAINT chk_intentions_state CHECK (state IN ('pending', 'result_ready', 'deciding', 'awaiting_owner', 'closed', 'cancelled', 'expired')),
    CONSTRAINT chk_intentions_close_reason CHECK (close_reason IS NULL OR close_reason IN ('resolved', 'legacy', 'delivered', 'cancelled', 'expired', 'fallback_report', 'failed_report'))
);

CREATE INDEX IF NOT EXISTS idx_intentions_open
    ON brain.intentions (agent_id, state)
    WHERE state IN ('pending', 'result_ready', 'deciding', 'awaiting_owner');

CREATE INDEX IF NOT EXISTS idx_intentions_root
    ON brain.intentions (agent_id, root_id);

-- The intention an inbox row belongs to. No foreign key: a failed check
-- must never cost a result its inbox row.
ALTER TABLE heart.result_inbox
    ADD COLUMN IF NOT EXISTS intention_id UUID;
