-- Migration 082: Result memory log (F098 Phase C)
--
-- A finished background subtask result becomes memory: one episode marked as
-- unverified subtask output, plus the full text as document chunks. This
-- table records one write-or-skip decision per source and makes the write
-- idempotent and crash-safe: the primary key decides which writer owns a
-- source, and episode_id is committed with the episode itself, so a retry
-- never creates a second one.
--
-- No backfill: rows appear only for subtasks that finish (or that the
-- reconciler finds inside NOUS_RESULT_MEMORY_SWEEP_LOOKBACK_HOURS) after
-- NOUS_RESULT_MEMORY_ENABLED is turned on.

CREATE TABLE IF NOT EXISTS heart.result_memory_log (
    agent_id      VARCHAR(100) NOT NULL,
    source_kind   VARCHAR(20)  NOT NULL CHECK (source_kind IN ('subtask')),
    source_id     UUID         NOT NULL,
    decision      VARCHAR(10)  NOT NULL CHECK (decision IN ('write', 'skip')),
    -- tier1 | tier2 | dag_node | inline | status | too_short | secret_detected |
    -- scheduled_off | scheduled_failure | launcher_stub | background
    reason        VARCHAR(40)  NOT NULL,
    state         VARCHAR(10)  NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'written', 'skipped', 'failed')),
    episode_id    UUID NULL REFERENCES heart.episodes(id) ON DELETE SET NULL,
    chunks        INT  NOT NULL DEFAULT 0,
    -- Why a written row has no chunks: short (fits the summary), ingest_disabled, too_short,
    -- no_embeddings (no embedding provider, so chunks cannot be embedded).
    chunk_reason  VARCHAR(40) NULL,
    attempts      INT  NOT NULL DEFAULT 0,
    last_error    TEXT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, source_kind, source_id)
);

-- The reconciler's retry scan.
CREATE INDEX IF NOT EXISTS idx_result_memory_log_retry
    ON heart.result_memory_log (agent_id, updated_at) WHERE state IN ('pending', 'failed');

-- Metrics window.
CREATE INDEX IF NOT EXISTS idx_result_memory_log_created
    ON heart.result_memory_log (agent_id, created_at DESC);
