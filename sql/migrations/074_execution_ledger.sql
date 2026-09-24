-- 074: Persisted execution ledger (harness-autonomy roadmap Phase 1b)
--
-- The F026 ExecutionLedger is in-memory and session-scoped: it is dropped at
-- end_conversation, on eviction and on restart, and its entries have no ids.
-- This table is the durable record of what the agent DID. One row per
-- side-effecting tool call. Reads are not recorded here (F091 covers them).
--
-- Lifecycle: the runner inserts the row as 'pending' BEFORE dispatching the
-- tool and closes it after. A call cancelled mid-flight is closed 'unknown'
-- because its side effect may or may not have happened. Rows left 'pending'
-- by a dead process are swept to 'unknown' at startup, which assumes ONE Nous
-- process per (database, agent_id) - true for the docker-compose deployment.
--
-- key_args never holds bodies, code or secrets: see
-- nous/cognitive/ledger_store.py durable_key_args.
-- idempotency_key and external_ref are reserved for Phase 2b.

CREATE TABLE IF NOT EXISTS nous_system.execution_ledger (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id           TEXT         NOT NULL,
    session_id         TEXT,
    parent_session_id  TEXT,
    context_kind       VARCHAR(32)  NOT NULL,
    subtask_id         UUID,
    dag_id             UUID,
    dag_node_id        UUID,
    turn               INTEGER,
    tool_name          VARCHAR(100) NOT NULL,
    side_effect_type   VARCHAR(20)  NOT NULL,
    key_args           JSONB        NOT NULL DEFAULT '{}',
    status             VARCHAR(20)  NOT NULL DEFAULT 'pending',
    result_summary     TEXT,
    idempotency_key    TEXT,
    external_ref       TEXT,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ,
    CONSTRAINT ck_execution_ledger_status
        CHECK (status IN ('pending', 'success', 'error', 'blocked', 'unknown')),
    CONSTRAINT ck_execution_ledger_side_effect
        CHECK (side_effect_type IN ('write', 'external', 'irreversible'))
);

CREATE INDEX IF NOT EXISTS idx_execution_ledger_agent_created
    ON nous_system.execution_ledger (agent_id, created_at DESC);

-- The orphan sweeps scan only pending rows.
CREATE INDEX IF NOT EXISTS idx_execution_ledger_pending
    ON nous_system.execution_ledger (agent_id, created_at)
    WHERE status = 'pending';
