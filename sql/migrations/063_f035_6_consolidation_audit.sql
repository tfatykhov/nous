-- F035.6 Consolidation Audit Diff
-- Per-sleep-cycle reviewable changelog of every memory mutation.
-- Two tables in nous_system: one envelope row per cycle, N action rows per cycle.
-- All writes are gated by NOUS_CONSOLIDATION_AUDIT_ENABLED (default false) at the
-- application layer, so this migration is inert until the flag is flipped.

-- Cycle envelope: one row per completed (or failed) sleep cycle.
CREATE TABLE IF NOT EXISTS nous_system.consolidation_cycles (
    cycle_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id     varchar(12),              -- F035.2 causal short-hash link (NOT a uuid)
    agent_id     varchar(100) NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running',  -- running | completed | failed
    phases_run   text[],
    totals       jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_consolidation_cycles_status
        CHECK (status IN ('running', 'completed', 'failed'))
);

-- Per-action changelog. cycle_id is NULLABLE so that if the envelope write
-- fails, action rows are still persisted (orphans) and recoverable by trace_id.
-- ON DELETE SET NULL means retention pruning the envelope never cascades into
-- mid-write action rows.
CREATE TABLE IF NOT EXISTS nous_system.consolidation_actions (
    action_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cycle_id     uuid REFERENCES nous_system.consolidation_cycles(cycle_id) ON DELETE SET NULL,
    trace_id     varchar(12),              -- denormalized so orphan rows stay causally linked
    agent_id     varchar(100) NOT NULL,
    phase        text NOT NULL,            -- f031_contradiction | f027_consolidate | reflect | stale_scan | ...
    op           text NOT NULL,            -- merge | supersede | deactivate | edge_add | edge_reweight | ...
    target_ids   uuid[],                   -- fact/episode/edge ids touched
    before       jsonb,                    -- list of {id, content_preview} for multi-source merge; null when N/A
    after        jsonb,                    -- post-op snapshot; null for prune/deactivate/tombstone
    rationale    text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_consolidation_actions_cycle
    ON nous_system.consolidation_actions (cycle_id);
CREATE INDEX IF NOT EXISTS idx_consolidation_actions_created
    ON nous_system.consolidation_actions (created_at);
CREATE INDEX IF NOT EXISTS idx_consolidation_actions_trace
    ON nous_system.consolidation_actions (trace_id);
CREATE INDEX IF NOT EXISTS idx_consolidation_actions_agent
    ON nous_system.consolidation_actions (agent_id);
CREATE INDEX IF NOT EXISTS idx_consolidation_cycles_started
    ON nous_system.consolidation_cycles (agent_id, started_at DESC);
