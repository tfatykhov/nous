-- 088: scorer heartbeat
--
-- One row per agent that says when the scoring command last ran and how that pass ended.
-- last_scored_at on the scoreboard only moves when a task is scored, so it cannot tell a
-- stopped scorer from an agent with nothing to score. This row goes stale exactly when the
-- scorer stops or can no longer write to scoring.
--
-- Telemetry: written only by the one-shot scoring command
-- (python -m nous.observability.task_score_job), read by GET /dashboard/autonomy, and
-- reached by no agent tool. Every pass overwrites the agent's row (upsert on agent_id).
-- A pass that commits writes it in the same transaction as its scores, so last_status ok
-- means the scores committed. A pass that fails writes last_status error in a transaction
-- of its own and leaves last_ok_at and rows_submitted as they were.
--
-- last_run_at is the time of the latest pass that wrote the row, ok or error.
-- last_error is the exception class name only, never its message (the log has the detail).
-- last_ok_at and rows_submitted are those of the latest pass that committed.
--
-- The table lives in scoring beside task_scores (migration 087 created the schema). Like
-- 087 this creates the table and nothing else. No owner, no grant, no role here, and it is
-- idempotent. Apply it before any role hardening takes the app role's right to create
-- objects in scoring away.

CREATE TABLE IF NOT EXISTS scoring.scorer_heartbeat (
    agent_id        TEXT         PRIMARY KEY,
    scorer_version  INTEGER      NOT NULL,
    last_run_at     TIMESTAMPTZ  NOT NULL,
    last_status     VARCHAR(8)   NOT NULL,
    last_error      VARCHAR(120),
    last_ok_at      TIMESTAMPTZ,
    rows_submitted  INTEGER,
    CONSTRAINT ck_scorer_heartbeat_status CHECK (last_status IN ('ok', 'error')),
    CONSTRAINT ck_scorer_heartbeat_error CHECK ((last_status = 'error') = (last_error IS NOT NULL))
);
