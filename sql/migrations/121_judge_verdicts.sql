-- 121: nous_system.judge_verdicts, the outside judge's verdicts (F118 PR 1)
--
-- One row per subject (a decision the agent resolved, later a finished task report), judge name and judge version.
-- The judge is harness code with fixed instructions: it reads the subject's record and the tool calls that were run,
-- and its verdict is kept here beside the claim it was compared with. against_grader and against_grade are that claim
-- as it stood when it was handed to the judge, for the attempt that wrote the row. status says whether a call gave the
-- verdict (ok), none was made (skipped, verdict cannot_tell, a label says why), or the call failed (error, timeout).
-- evidence holds counts only. reason is at most one redacted sentence of the judge, 200 characters. calls is the
-- number of model requests sent for the row (0 for a skipped row), the daily cap's count. An error or timeout row is
-- replaced by a later attempt at the same version, at most three (attempts), its calls added. No foreign key: a
-- verdict outlives its subject for the numbers. The second index serves the daily call count and the route's window.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Rollback is
-- DROP TABLE IF EXISTS nous_system.judge_verdicts and deleting version 121 from schema_migrations.

CREATE TABLE IF NOT EXISTS nous_system.judge_verdicts (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id       TEXT        NOT NULL,
    subject_kind   TEXT        NOT NULL CHECK (subject_kind IN ('decision', 'task_report')),
    subject_id     UUID        NOT NULL,
    judge_name     TEXT        NOT NULL,
    judge_version  TEXT        NOT NULL,
    model          TEXT,
    status         TEXT        NOT NULL CHECK (status IN ('ok', 'skipped', 'error', 'timeout')),
    verdict        TEXT        NOT NULL,
    confidence     REAL        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    labels         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    reason         TEXT        CHECK (reason IS NULL OR char_length(reason) <= 200),
    against_grader TEXT,
    against_grade  TEXT,
    evidence       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    latency_ms     INTEGER,
    calls          SMALLINT    NOT NULL DEFAULT 0 CHECK (calls >= 0),
    attempts       SMALLINT    NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    judged_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_judge_verdicts_verdict CHECK (
        (subject_kind = 'decision'    AND verdict IN ('success', 'partial', 'failure', 'cannot_tell')) OR
        (subject_kind = 'task_report' AND verdict IN ('supported', 'not_supported', 'cannot_tell'))),
    CONSTRAINT uq_judge_verdicts_subject UNIQUE (agent_id, subject_kind, subject_id, judge_name, judge_version)
);

CREATE INDEX IF NOT EXISTS idx_judge_verdicts_agent_judged ON nous_system.judge_verdicts (agent_id, judged_at);
