-- 107: the obligation ledger (F111 PR A)
--
-- brain.commitments: one row per forward-looking obligation ("I'll do X by T"), written by the `commit` tool or by
-- the turn-close promise gate (nous/cognitive/promise_gate.py) when a reply promises work that no artifact backs.
-- short_id is what the owner sees and types (C-0042, `/drop C-0042`). dedupe_key makes an insert idempotent: the
-- store writes md5(turn key || normalised what), so a retried write of the same promise in the same turn is one row.
-- status: open (nothing linked), backed (an artifact is linked), proposed (a background turn staged it), done,
-- broken, cancelled, deferred, superseded. The overdue watcher of PR B reads open/backed/proposed only.
-- artifact_kind adds 'check' (a heartbeat_check_create id) to the spec's list: that tool backs a promise too.
--
-- nous_system.promise_gate_log: one row per turn the gate ran on, clean turns included: the denominator of
-- detected, backed and unbacked promises. agent_id is added to the spec's columns (every table is agent-scoped).
--
-- No column is named like a credential. Writer: the app. Readers: the app only; no reader grant, and
-- scripts/ops/db_roles.sql is not touched. Created by the application role through the migrator.

CREATE TABLE IF NOT EXISTS brain.commitments (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    short_id           BIGSERIAL    UNIQUE,
    agent_id           TEXT         NOT NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    source_kind        TEXT         NOT NULL CHECK (source_kind IN
                       ('chat_stream', 'chat_turn', 'subtask', 'dag_node', 'scheduled', 'heartbeat', 'continuation',
                        'decision', 'sweep', 'manual')),
    session_id         TEXT,
    turn_id            TEXT,
    episode_id         UUID,
    origin_decision_id UUID,
    origin_subtask_id  UUID,
    detected_by        TEXT         NOT NULL CHECK (detected_by IN ('tool', 'gate', 'decision_link', 'bg_report', 'manual')),
    detector_version   TEXT,
    confidence         REAL,
    promise_text       TEXT         NOT NULL CHECK (char_length(promise_text) <= 500),
    what               TEXT         NOT NULL,
    kind               TEXT         NOT NULL DEFAULT 'action' CHECK (kind IN
                       ('action', 'goal', 'sweep_apply', 'follow_up', 'awaiting_user')),
    trigger_spec       JSONB,
    action_spec        JSONB,
    consent            TEXT         NOT NULL DEFAULT 'none' CHECK (consent IN ('none', 'explicit', 'silence')),
    due_at             TIMESTAMPTZ  NOT NULL,
    grace_seconds      INT          NOT NULL DEFAULT 900,
    status             TEXT         NOT NULL DEFAULT 'open' CHECK (status IN
                       ('open', 'backed', 'proposed', 'done', 'broken', 'cancelled', 'deferred', 'superseded')),
    artifact_kind      TEXT         CHECK (artifact_kind IN
                       ('schedule', 'dag', 'subtask', 'intention', 'proposal', 'job', 'file', 'pr', 'check')),
    artifact_id        TEXT,
    artifact_linked_at TIMESTAMPTZ,
    evidence           JSONB,
    closed_at          TIMESTAMPTZ,
    close_reason       TEXT,
    last_reconciled_at TIMESTAMPTZ,
    notified_at        TIMESTAMPTZ,
    notify_count       INT          NOT NULL DEFAULT 0,
    dedupe_key         TEXT         NOT NULL,
    UNIQUE (agent_id, dedupe_key)
);

-- The watcher's query (PR B): live rows by deadline.
CREATE INDEX IF NOT EXISTS commitments_watch_idx
    ON brain.commitments (status, due_at)
    WHERE status IN ('open', 'backed', 'proposed');

CREATE TABLE IF NOT EXISTS nous_system.promise_gate_log (
    id           BIGSERIAL    PRIMARY KEY,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    agent_id     TEXT         NOT NULL,
    session_id   TEXT,
    turn_id      TEXT,
    path         TEXT         NOT NULL CHECK (path IN ('stream', 'run_turn')),
    context_kind TEXT,
    mode         TEXT         NOT NULL,
    detected     INT          NOT NULL,
    backed       INT          NOT NULL,
    unbacked     INT          NOT NULL,
    reentries    INT          NOT NULL DEFAULT 0,
    outcome      TEXT         NOT NULL,
    latency_ms   INT
);

-- The weekly counts read the newest rows of an agent.
CREATE INDEX IF NOT EXISTS idx_promise_gate_log_agent_created
    ON nous_system.promise_gate_log (agent_id, created_at DESC);
