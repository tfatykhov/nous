-- 086: the execution ledger keeps what a call ran and what came back
-- (owner decision 2026-10-13). Written only while
-- NOUS_EXECUTION_LEDGER_DETAIL_ENABLED is on, redacted and bounded by
-- nous/cognitive/ledger_store.py, and cleared after
-- NOUS_EXECUTION_LEDGER_DETAIL_RETENTION_DAYS.
ALTER TABLE nous_system.execution_ledger ADD COLUMN IF NOT EXISTS input_text TEXT;
ALTER TABLE nous_system.execution_ledger ADD COLUMN IF NOT EXISTS output_text TEXT;
