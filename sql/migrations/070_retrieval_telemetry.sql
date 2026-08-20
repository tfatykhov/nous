-- 070: F091 Retrieval Telemetry — what recall retrieved, and what it dropped
--
-- One row per RETRIEVAL, not per candidate. A candidate-per-row table would be
-- roughly 40x the write volume for no query benefit at this scale, so anything
-- that needs to be filtered or aggregated is hoisted to a header column and the
-- detail stays in JSONB.
--
-- Correlation is (agent_id, session_id, turn_number) -> nous_system.context_log.
-- NOT trace_id: the causal-chain trace id is minted on the turn_completed event
-- in post_turn (cognitive/layer.py), which is AFTER retrieval has already run,
-- so no trace id exists at the moment a retrieval happens. The column is kept
-- nullable and RESERVED for a future pre_turn-minted id, and is deliberately
-- left unindexed until something populates it.

CREATE TABLE IF NOT EXISTS nous_system.retrieval_log (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    session_id      TEXT,
    turn_number     INTEGER,
    -- Reserved. See the correlation note above. Nothing populates this yet.
    trace_id        VARCHAR(12),
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- 'pipeline' (recall_deep -> run_recall_pipeline) or 'context'
    -- (ContextEngine.build, which runs every turn and does NOT use the pipeline)
    path            TEXT NOT NULL,
    query           TEXT,
    duration_ms     REAL,

    -- Per-leg summaries. Carries attempted/skip_reason so a leg that ran and
    -- found nothing is distinguishable from one that never ran.
    legs            JSONB NOT NULL DEFAULT '[]',
    -- Memory types dropped from the pool BEFORE search (e.g. F080 coherent
    -- ranking removing censor/procedure). No candidates exist to attribute.
    excluded_types  JSONB NOT NULL DEFAULT '[]',

    n_candidates    INTEGER NOT NULL DEFAULT 0,
    n_rendered      INTEGER NOT NULL DEFAULT 0,
    n_expansions    INTEGER NOT NULL DEFAULT 0,
    -- disposition -> count, so systemic drops are queryable without opening
    -- the candidates array
    disposition_counts JSONB NOT NULL DEFAULT '{}',

    -- NULL when this retrieval was not sampled for candidate capture. The
    -- header, legs and expansions above are still recorded.
    candidates      JSONB,
    -- seed -> edge -> neighbor rows. Pure capture, no extra query.
    expansions      JSONB NOT NULL DEFAULT '[]',

    truncated       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_retrieval_log_time  ON nous_system.retrieval_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_agent ON nous_system.retrieval_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_sess  ON nous_system.retrieval_log(session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_path  ON nous_system.retrieval_log(path, timestamp DESC);
