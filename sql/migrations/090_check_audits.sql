-- 090: check audits
--
-- The owner's spot checks of completion checks the agent wrote itself. Once a week the app
-- samples a few finished subtasks whose own DAG node check passed (nous/audits/check_audit.py)
-- and pushes one companion card. The owner answers each item right or wrong (the audit.verdict
-- action). The task scorer reads the answers: a verdict counts as the owner's grade on that
-- subtask, and a check kind whose latest answers are all right is trusted.
--
-- One row per sampled subtask (UNIQUE agent_id, task_id), so a subtask is never sampled twice.
-- A row is written with surface_id NULL, then the card is pushed, then surface_id is set. A row
-- whose push failed keeps surface_id NULL and is offered again. verdict, actor and answered_at
-- are set once, by the action handler, and only on a row that names the card's own surface.
-- check_kind is task_scoring.check_kind() of the node's completion_check.
--
-- Writer: the app (the sampler and the action handler). Reader: the scoring job, which needs
-- SELECT on this table: re-run scripts/ops/db_roles.sql after this migration has been applied.
-- Created by the application role through the migrator: the table and its index, nothing else.

CREATE TABLE IF NOT EXISTS nous_system.check_audits (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT         NOT NULL,
    task_id      UUID         NOT NULL,
    node_id      UUID         NOT NULL,
    check_kind   TEXT         NOT NULL,
    sampled_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    surface_id   TEXT,
    verdict      TEXT,
    actor        TEXT,
    answered_at  TIMESTAMPTZ,
    CONSTRAINT uq_check_audits_task UNIQUE (agent_id, task_id),
    CONSTRAINT ck_check_audits_verdict CHECK (verdict IN ('right', 'wrong'))
);

-- The scorer's trust read: one agent's answered audits of a kind, by answer time.
CREATE INDEX IF NOT EXISTS idx_check_audits_agent_kind_answered
    ON nous_system.check_audits (agent_id, check_kind, answered_at);
