-- Migration 084: Intention arrivals and proposals (F099 Phase 2b)
--
-- brain.intention_arrivals: one row per arrival decision of a root's
-- continuation. brain.intention_proposals: an action the continuation may not
-- take itself, staged for the owner. Phase 2b creates the tables and the
-- widened inbox. Nothing writes the two new tables until Phase 2c and 2d.
--
-- brain.intentions needs no new column: claimed_at, claim_token, attempts and
-- deadline already exist in migration 083.

CREATE TABLE IF NOT EXISTS brain.intention_arrivals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    root_id UUID NOT NULL REFERENCES brain.intentions(id),
    n INTEGER NOT NULL,
    intention_ids UUID[] NOT NULL,
    inbox_ids UUID[] NOT NULL DEFAULT '{}',
    report_ids UUID[] NOT NULL DEFAULT '{}',
    claim_token UUID NOT NULL,
    decision VARCHAR(20),
    note TEXT,
    progress_claimed BOOLEAN,
    progress BOOLEAN,
    confidence REAL,
    gate_reason VARCHAR(40),
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    decision_record_id UUID,
    outcome VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ,
    CONSTRAINT uq_intention_arrivals_root_n UNIQUE (agent_id, root_id, n),
    CONSTRAINT chk_intention_arrivals_decision CHECK (decision IS NULL OR decision IN ('continue', 'revise', 'drop', 'report', 'ask')),
    CONSTRAINT chk_intention_arrivals_outcome CHECK (outcome IN ('resolved', 'fallback_report', 'failed_report')),
    CONSTRAINT chk_intention_arrivals_gate_reason CHECK (gate_reason IS NULL OR gate_reason IN ('cancelled', 'expired', 'past_deadline', 'budget_turns', 'budget_tokens', 'budget_stall', 'limit_depth', 'limit_spawns', 'plan_resolved'))
);

CREATE INDEX IF NOT EXISTS idx_intention_arrivals_root
    ON brain.intention_arrivals (agent_id, root_id, n);

CREATE TABLE IF NOT EXISTS brain.intention_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    intention_id UUID NOT NULL REFERENCES brain.intentions(id),
    root_id UUID NOT NULL REFERENCES brain.intentions(id),
    arrival_id UUID REFERENCES brain.intention_arrivals(id),
    tool VARCHAR(100) NOT NULL,
    arguments JSONB NOT NULL,
    rationale TEXT NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'staged',
    claim_token UUID NOT NULL,
    deadline TIMESTAMPTZ,
    ledger_key TEXT,
    decided_at TIMESTAMPTZ,
    decided_by TEXT,
    executed_at TIMESTAMPTZ,
    result TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_intention_proposals_state CHECK (state IN ('staged', 'pending', 'approved', 'executing', 'rejected', 'expired', 'executed', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_intention_proposals_open
    ON brain.intention_proposals (agent_id, state)
    WHERE state IN ('staged', 'pending', 'approved', 'executing');

CREATE INDEX IF NOT EXISTS idx_intention_proposals_arrival
    ON brain.intention_proposals (agent_id, arrival_id);

-- Owner-facing rows (reports, questions, proposals) join the inbox as a new
-- source kind, and the UNIQUE key gains agent_id (spec section 4.3 item 4).
ALTER TABLE heart.result_inbox
    DROP CONSTRAINT IF EXISTS chk_result_inbox_source_kind;
ALTER TABLE heart.result_inbox
    ADD CONSTRAINT chk_result_inbox_source_kind
    CHECK (source_kind IN ('subtask', 'dag', 'intention_report'));

ALTER TABLE heart.result_inbox
    DROP CONSTRAINT IF EXISTS chk_result_inbox_msg_type;
ALTER TABLE heart.result_inbox
    ADD CONSTRAINT chk_result_inbox_msg_type
    CHECK (msg_type IN ('INFORM', 'FAILURE', 'BLOCKED', 'REPORT', 'QUESTION', 'PROPOSAL'));

ALTER TABLE heart.result_inbox
    DROP CONSTRAINT IF EXISTS uq_result_inbox_source;
-- agent_id goes LAST. The reconciler's correlated has_row lookups prefix on
-- (source_kind, source_id) and keep using this index.
ALTER TABLE heart.result_inbox
    ADD CONSTRAINT uq_result_inbox_source UNIQUE (source_kind, source_id, source_generation, agent_id);

ALTER TABLE heart.result_inbox
    ADD COLUMN IF NOT EXISTS arrival_id UUID,
    ADD COLUMN IF NOT EXISTS proposal_id UUID,
    ADD COLUMN IF NOT EXISTS push_after TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS push_message_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_result_inbox_intention_undelivered
    ON heart.result_inbox (agent_id, intention_id) WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_result_inbox_push_due
    ON heart.result_inbox (agent_id, push_after) WHERE pushed_at IS NULL AND push_after IS NOT NULL;
