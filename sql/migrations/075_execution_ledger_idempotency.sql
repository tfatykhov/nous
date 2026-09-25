-- Harness Phase 2b: idempotent sends.
-- dispatched_at is set by a second write immediately before a KEYED call is
-- dispatched. A keyed row still pending with dispatched_at NULL was never
-- sent, so the orphan sweep can free its key instead of holding it forever.
ALTER TABLE nous_system.execution_ledger
    ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMPTZ;

-- At most one LIVE row per key: pending (in flight), success (sent) or
-- unknown (maybe delivered, never auto-resent). error and blocked free it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_ledger_idempotency
    ON nous_system.execution_ledger (agent_id, tool_name, idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND status IN ('pending', 'success', 'unknown');
