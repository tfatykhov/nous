-- 101: the model-call log and its attempts (F108 agent work view, Phase 1 recording, model-call numbers)
--
-- model_call_log: one row per model call, written when the call ends by the clients' recorder
-- (NOUS_MODEL_CALL_LOG_ENABLED, off by default). purpose says what the call was for (unlabelled when the caller named
-- nothing, so a missing label is counted, not dropped), label_source where that came from (explicit, context or
-- none), call_kind how it was made (call, stream, aggregated, embedding, rerank or browser_step). status is ok, error
-- or cancelled, http_status the last HTTP status, error_text redacted and capped by the writer. input_total is
-- computed by the database: uncached input plus cache reads plus cache writes, a missing part counting as zero.
-- attempts is the number of HTTP attempts, one row each in model_call_attempt. Embedding and rerank calls are
-- counted per minute instead: one row per minute, purpose, model and status, call_count calls, their tokens and
-- durations summed. group_id joins calls that are one logical call (a re-send), is_fallback marks a call made on a
-- fallback model. The context columns (section_tokens_est to cache_break_tokens_lost) are the runner's estimate of
-- what the call's context held: section_tokens_est is context_log.token_breakdown renamed, because a column that
-- starts with token_ is refused by the reader's credential check. turn_id names an agent_turn_log row with no
-- foreign key (the turn row is written when the turn ends). No column holds message text.
--
-- model_call_attempt: one row per HTTP attempt of a call (the retries are its attempts), deleted with its call.
-- error_text is redacted and capped by the writer.
--
-- No column is named like a credential (scripts/ops/db_roles.sql section 5d refuses to grant one).
-- Writer: the app. Readers: the app, and the db_query reader for both tables (scripts/ops/db_roles.sql).
-- Created by the application role through the migrator: the tables and their indexes, nothing else.

CREATE TABLE IF NOT EXISTS nous_system.model_call_log (
    id                       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id                 TEXT         NOT NULL,
    group_id                 UUID,
    is_fallback              BOOLEAN      NOT NULL DEFAULT FALSE,
    started_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    purpose                  TEXT         NOT NULL,
    label_source             TEXT         NOT NULL,
    call_kind                TEXT         NOT NULL,
    model                    TEXT,
    session_id               TEXT,
    parent_session_id        TEXT,
    turn_id                  UUID,
    turn_number              INT,
    frame_id                 TEXT,
    subtask_id               UUID,
    dag_id                   UUID,
    trace_id                 TEXT,
    status                   TEXT         NOT NULL,
    http_status              INT,
    error_text               TEXT,
    stop_reason              TEXT,
    input_uncached           INT,
    cache_read               INT,
    cache_write              INT,
    input_total              INT          GENERATED ALWAYS AS
                                          (coalesce(input_uncached, 0) + coalesce(cache_read, 0)
                                           + coalesce(cache_write, 0)) STORED,
    output_tokens            INT,
    duration_ms              INT,
    attempts                 SMALLINT     NOT NULL DEFAULT 1,
    call_count               INT          NOT NULL DEFAULT 1,
    section_tokens_est       JSONB,
    total_tokens_est         INT,
    context_window_size      INT,
    utilization_pct          REAL,
    sections_present         TEXT[],
    tools_count              INT,
    tool_names               TEXT[],
    messages_count           INT,
    message_roles            JSONB,
    loaded_facts             INT,
    loaded_decisions         INT,
    loaded_procedures        INT,
    loaded_episodes          INT,
    recent_conversations     INT,
    cache_break              BOOLEAN      NOT NULL DEFAULT FALSE,
    cache_break_components   TEXT[],
    cache_break_tokens_lost  INT
);

-- Recent calls, and the retention sweep.
CREATE INDEX IF NOT EXISTS idx_model_call_log_agent_started
    ON nous_system.model_call_log (agent_id, started_at DESC);

-- Recent calls of one session.
CREATE INDEX IF NOT EXISTS idx_model_call_log_agent_session_started
    ON nous_system.model_call_log (agent_id, session_id, started_at DESC);

-- Recent calls for one purpose.
CREATE INDEX IF NOT EXISTS idx_model_call_log_agent_purpose_started
    ON nous_system.model_call_log (agent_id, purpose, started_at DESC);

-- A turn's calls.
CREATE INDEX IF NOT EXISTS idx_model_call_log_turn
    ON nous_system.model_call_log (turn_id);

CREATE TABLE IF NOT EXISTS nous_system.model_call_attempt (
    call_id      UUID         NOT NULL REFERENCES nous_system.model_call_log (id) ON DELETE CASCADE,
    agent_id     TEXT         NOT NULL,
    attempt_no   SMALLINT     NOT NULL,
    started_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    duration_ms  INT,
    http_status  INT,
    error_type   TEXT,
    error_text   TEXT,
    PRIMARY KEY (call_id, attempt_no)
);
