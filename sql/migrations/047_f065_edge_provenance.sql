-- Migration 047: F065 — Edge Provenance & God-Node Surfacing
-- Adds `extraction_method` provenance column to brain.graph_edges with
-- relation-based backfill, plus a brain.graph_hub_snapshots table for
-- session-start hub-shift detection.
--
-- Backfill discriminator is relation, NOT auto_linked. Every production
-- writer hard-codes auto_linked=TRUE (verified 2026-05-23: Brain.link()
-- has zero callers); using auto_linked would classify zero rows as
-- 'deterministic'. Supersession is the one relation whose writer carries
-- structural provenance — facts.py and brain.py write 'supersedes' from
-- explicit supersession decisions, never from cosine matching.

BEGIN;

-- Step 1: Add nullable column (no table lock at this size).
ALTER TABLE brain.graph_edges
    ADD COLUMN IF NOT EXISTS extraction_method VARCHAR(20)
        CHECK (extraction_method IN ('deterministic', 'heuristic', 'inferred'));

-- Step 2: Backfill — deterministic = supersession relations only.
UPDATE brain.graph_edges
SET extraction_method = 'deterministic'
WHERE relation = 'supersedes'
  AND extraction_method IS NULL;

-- Step 3: Backfill — LLM-inferred contradictions (F027 contradiction path).
UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE relation = 'contradicts'
  AND extraction_method IS NULL;

-- Step 4: Backfill — everything else is heuristic (cosine-derived).
UPDATE brain.graph_edges
SET extraction_method = 'heuristic'
WHERE extraction_method IS NULL;

-- Step 5: Tighten to NOT NULL with default.
ALTER TABLE brain.graph_edges
    ALTER COLUMN extraction_method SET NOT NULL,
    ALTER COLUMN extraction_method SET DEFAULT 'heuristic';

-- Step 6: Index for filtered recall queries.
CREATE INDEX IF NOT EXISTS idx_graph_edges_extraction_method
    ON brain.graph_edges(agent_id, extraction_method);

-- Step 7: Hub-snapshot table for session-start shift detection.
-- Lives in brain schema with no FTS / vector index so baseline rows
-- never appear as candidates in recall_deep (audit P1).
CREATE TABLE IF NOT EXISTS brain.graph_hub_snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     TEXT NOT NULL,
    node_id      UUID NOT NULL,
    node_type    VARCHAR(20) NOT NULL,
    degree       INTEGER NOT NULL,
    rank         INTEGER,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Most-recent-per-node lookup (pre_turn hub-shift hook).
CREATE INDEX IF NOT EXISTS idx_graph_hub_snapshots_agent_node
    ON brain.graph_hub_snapshots (agent_id, node_id, captured_at DESC);

-- Retention prune query (sleep handler).
CREATE INDEX IF NOT EXISTS idx_graph_hub_snapshots_agent_captured
    ON brain.graph_hub_snapshots (agent_id, captured_at);

COMMIT;
