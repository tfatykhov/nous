-- 113: goals worked on in background sessions (F114)
--
-- brain.goals: one row per goal. short_id is what the owner types (G-0007). The limits are columns, so the
-- database holds the two mandatory ones (total_budget and the deadline) and the arithmetic between the others.
-- state and pause_reason move together (CHECK). deadline_at is set when the goal is approved.
-- mode and mode_until are reserved for a later owner setting: nothing writes them yet.
-- pending_request is the one slot through which a process that is not the app (the host command) asks the
-- driver for run_now, stop or cancel. Tokens are the continuation accounting unit (uncached input plus output).
--
-- brain.goal_sessions: one row per session. first_subtask_id is written BEFORE the subtask exists, so a start
-- that stops between its two transactions can be repaired from the intention of that subtask. At most one open
-- session per goal (partial unique index).
--
-- brain.goal_journal: append-only record (the store has no update or delete). Text is bounded.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Created by the
-- application role through the migrator.

CREATE TABLE IF NOT EXISTS brain.goals (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    short_id             BIGSERIAL   UNIQUE,
    agent_id             TEXT        NOT NULL,
    title                TEXT        NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    objective            TEXT        NOT NULL CHECK (char_length(objective) BETWEEN 1 AND 4000),
    done_when            TEXT        NOT NULL CHECK (char_length(done_when) BETWEEN 1 AND 2000),
    preset               TEXT        NOT NULL CHECK (preset IN ('small', 'medium', 'large', 'custom')),
    total_budget         BIGINT      NOT NULL CHECK (total_budget BETWEEN 1 AND 1000000000),
    day_budget           BIGINT      NOT NULL CHECK (day_budget > 0),
    deadline_days        INTEGER     NOT NULL CHECK (deadline_days BETWEEN 1 AND 90),
    sessions_per_day     INTEGER     NOT NULL CHECK (sessions_per_day BETWEEN 1 AND 24),
    min_interval_seconds INTEGER     NOT NULL CHECK (min_interval_seconds >= 60),
    jobs_per_day         INTEGER     NOT NULL CHECK (jobs_per_day BETWEEN 0 AND 20),
    quiet_hours_ok       BOOLEAN     NOT NULL DEFAULT FALSE,
    stall_limit          INTEGER     NOT NULL DEFAULT 3 CHECK (stall_limit BETWEEN 1 AND 10),
    mode                 TEXT        NOT NULL DEFAULT 'contained' CHECK (mode IN ('contained', 'full_autonomy')),
    mode_until           TIMESTAMPTZ,
    state                TEXT        NOT NULL DEFAULT 'draft'
                         CHECK (state IN ('draft', 'active', 'paused', 'done', 'failed', 'cancelled')),
    pause_reason         TEXT        CHECK (pause_reason IS NULL OR pause_reason IN
                         ('owner', 'budget', 'stalled', 'waiting_for_owner', 'error', 'deadline')),
    plan                 JSONB       NOT NULL DEFAULT '[]'::jsonb
                         CHECK (jsonb_typeof(plan) = 'array' AND jsonb_array_length(plan) <= 20),
    proposed_by          TEXT        NOT NULL CHECK (proposed_by IN ('owner', 'agent', 'host')),
    origin_channel       TEXT,
    pending_request      TEXT        CHECK (pending_request IS NULL OR pending_request IN ('run_now', 'stop', 'cancel')),
    pending_request_at   TIMESTAMPTZ,
    approved_at          TIMESTAMPTZ,
    deadline_at          TIMESTAMPTZ,
    next_wake_at         TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_goals_pause CHECK ((state = 'paused') = (pause_reason IS NOT NULL)),
    CONSTRAINT chk_goals_deadline CHECK (state IN ('draft', 'cancelled') OR deadline_at IS NOT NULL),
    CONSTRAINT chk_goals_day_within_total CHECK (day_budget <= total_budget),
    CONSTRAINT chk_goals_request CHECK ((pending_request IS NULL) = (pending_request_at IS NULL)),
    CONSTRAINT chk_goals_mode CHECK (mode = 'contained' OR mode_until IS NOT NULL)
);

-- The driver start scan.
CREATE INDEX IF NOT EXISTS idx_goals_active_wake ON brain.goals (agent_id, next_wake_at) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS brain.goal_sessions (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id         TEXT        NOT NULL,
    goal_id          UUID        NOT NULL REFERENCES brain.goals(id),
    n                INTEGER     NOT NULL CHECK (n >= 1),
    trigger          TEXT        NOT NULL CHECK (trigger IN ('timer', 'run_now')),
    first_subtask_id UUID        NOT NULL,
    root_id          UUID,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    outcome          TEXT        CHECK (outcome IN ('step_closed', 'no_step', 'asked', 'error', 'interrupted')),
    spent            BIGINT      NOT NULL DEFAULT 0 CHECK (spent >= 0),
    steps_closed     INTEGER     NOT NULL DEFAULT 0 CHECK (steps_closed >= 0),
    summary          TEXT        CHECK (summary IS NULL OR char_length(summary) <= 4000),
    CONSTRAINT uq_goal_sessions_n UNIQUE (goal_id, n),
    CONSTRAINT uq_goal_sessions_first_subtask UNIQUE (first_subtask_id),
    CONSTRAINT chk_goal_sessions_ended CHECK ((ended_at IS NULL) = (outcome IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_sessions_one_open ON brain.goal_sessions (goal_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_goal_sessions_started ON brain.goal_sessions (agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_goal_sessions_root ON brain.goal_sessions (root_id) WHERE root_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS brain.goal_journal (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    seq        BIGSERIAL   NOT NULL UNIQUE,
    agent_id   TEXT        NOT NULL,
    goal_id    UUID        NOT NULL REFERENCES brain.goals(id),
    session_n  INTEGER,
    kind       TEXT        NOT NULL CHECK (kind IN
               ('plan', 'progress', 'report', 'question', 'answer', 'owner_note', 'state', 'done_claim')),
    actor      TEXT        NOT NULL CHECK (actor IN ('owner', 'host', 'chain', 'harness')),
    text       TEXT        NOT NULL CHECK (char_length(text) <= 4000),
    evidence   JSONB       NOT NULL DEFAULT '[]'::jsonb
               CHECK (jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) <= 10),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_goal_journal_goal ON brain.goal_journal (goal_id, seq DESC);
