-- Migration 037: F051 retrieval evaluation run history.
--
-- One row per harness invocation (`python -m nous_eval.retrieval ...`).
-- Populated by `nous_eval/report.py::persist_run_history` when
-- NOUS_EVAL_RUN_HISTORY_ENABLED=true (the default). INSERT is wrapped in
-- `asyncio.wait_for(..., timeout=NOUS_EVAL_RUN_HISTORY_INSERT_TIMEOUT_S)` so
-- a stalled eval DB cannot block the CLI (P1-7 in plan v2.1).
--
-- gen_random_uuid() is a Postgres 13+ built-in; no CREATE EXTENSION pgcrypto
-- required (consistent with sql/init.sql and other migrations).

CREATE TABLE IF NOT EXISTS nous_system.eval_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    git_sha         TEXT NOT NULL,
    fixture_version TEXT NOT NULL,
    configs         JSONB NOT NULL,
    metrics         JSONB NOT NULL,
    qrel_counts     JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    report_path     TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_created_at ON nous_system.eval_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_eval_runs_agent_id   ON nous_system.eval_runs(agent_id);

COMMENT ON TABLE nous_system.eval_runs IS
    'F051: retrieval evaluation run history — one row per harness invocation.';
