-- 087: task scores (autonomy plan v2, Phase A1)
--
-- One deterministic row per finished task and scorer version, in its OWN schema, scoring.
-- Why a schema of its own: in Postgres the owner of a schema can drop any table in it, and
-- the app role owns brain, heart and nous_system, so a scores table in one of those could
-- never be protected from the app. The integrity plan's ops script creates the roles, makes
-- a separate scoring role the owner of this schema and table, and gives the app role SELECT
-- only. This migration runs as the app role through the migrator, so it creates the schema
-- and the table and nothing else. No owner, no grant, no role here, and it is idempotent.
--
-- A subtask is the unit that is scored (a leaf, with a score). A DAG run and an intention
-- root get a roll-up row whose score is NULL and that holds counts only, so that one failing
-- subtask inside a DAG inside a chain is penalised exactly once.
--
-- Telemetry: written only by the one-shot scoring command
-- (python -m nous.observability.task_score_job, run by the nous-scorer service), read by
-- GET /dashboard/autonomy, and reached by no agent tool.
--
-- Each run re-scores every task finished in the last 15 days, so a row is refreshed
-- in place (upsert on the unique key). settled is true once the task is older than
-- 14 days, when owner grades and undos have mostly arrived, and a settled row is
-- never rewritten. first_scored_at is set on insert and never changed: the earliest value
-- is when scoring began, and coverage is only measured from then. A new scorer_version adds
-- rows beside the old ones and never rewrites them.
--
-- outcome done_self is a task Nous reports as done. done_verified needs the owner
-- (a success grade) or a completed check node in the DAG. not_scored is a cancelled
-- task or a legacy close, with the reason in not_scored_reason.
-- task_id is TEXT because it holds a subtask, DAG or intention root UUID as a string.

CREATE SCHEMA IF NOT EXISTS scoring;

CREATE TABLE IF NOT EXISTS scoring.task_scores (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id             TEXT         NOT NULL,
    task_kind            VARCHAR(20)  NOT NULL,
    task_id              TEXT         NOT NULL,
    finished_at          TIMESTAMPTZ  NOT NULL,
    scorer_version       INTEGER      NOT NULL,
    outcome              VARCHAR(16)  NOT NULL,
    not_scored_reason    VARCHAR(20),
    owner_grade          VARCHAR(20),
    owner_overrides      INTEGER      NOT NULL DEFAULT 0,
    unverified_claims    INTEGER      NOT NULL DEFAULT 0,
    harness_errors       INTEGER      NOT NULL DEFAULT 0,
    refusals             INTEGER      NOT NULL DEFAULT 0,
    tokens               INTEGER,
    score                DOUBLE PRECISION,
    children_scored      INTEGER,
    children_mean_score  DOUBLE PRECISION,
    settled              BOOLEAN      NOT NULL DEFAULT FALSE,
    evidence             JSONB        NOT NULL DEFAULT '{}',
    first_scored_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    scored_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_task_scores_task
        UNIQUE (agent_id, task_kind, task_id, scorer_version),
    CONSTRAINT ck_task_scores_kind
        CHECK (task_kind IN ('subtask', 'dag', 'intention')),
    CONSTRAINT ck_task_scores_outcome
        CHECK (outcome IN ('done_self', 'done_verified', 'partial', 'failed', 'not_scored')),
    CONSTRAINT ck_task_scores_leaf_only
        CHECK (score IS NULL OR task_kind = 'subtask'),
    CONSTRAINT ck_task_scores_not_scored
        CHECK (outcome <> 'not_scored' OR score IS NULL)
);

-- The scoreboard reads one agent's rows of the current scorer version by finish time.
CREATE INDEX IF NOT EXISTS idx_task_scores_agent_version_finished
    ON scoring.task_scores (agent_id, scorer_version, finished_at DESC);
