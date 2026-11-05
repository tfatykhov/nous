-- 099: the daily memory write-health rollup
--
-- One row per agent per UTC day, written by the hourly memory_write_health heartbeat check
-- (nous/observability/memory_write_health.py). The current day's row is overwritten on each run.
-- metrics: the day's counts (facts by source, tier-1 inflow, deactivations by reason, admission rejects,
-- episodes) and, on days the check observed live, the point-in-time values the hard-delete detector compares
-- against its baseline (row totals, ledger ids with no row, dangling edges, running drop counters).
-- metrics_version: bumped when a key changes meaning, so a reader can tell old rows apart.
-- Writer: the app. Reader: the dashboard (the app) and the db_query reader (scripts/ops/db_roles.sql).
-- Created by the application role through the migrator: the table, nothing else.

CREATE TABLE IF NOT EXISTS nous_system.memory_write_health_daily (
    agent_id         VARCHAR(100) NOT NULL,
    day              DATE         NOT NULL,
    metrics          JSONB        NOT NULL,
    metrics_version  INT          NOT NULL DEFAULT 1,
    computed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, day)
);
