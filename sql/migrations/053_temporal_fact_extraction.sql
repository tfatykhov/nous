-- F075: temporal fact extraction + date-aware retrieval
-- Spec: docs/features/F075-temporal-fact-extraction.md (v2.17, PR #460)
-- Plan: docs/superpowers/plans/2026-05-28-f075-temporal-fact-extraction.md

BEGIN;

-- F075 Layer 1: date-anchored event tracking on heart.facts
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS event_date DATE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS event_date_classified_at TIMESTAMPTZ DEFAULT NULL;

-- Partial index for date-range queries (Layer 3 + Layer 2 edge build)
CREATE INDEX IF NOT EXISTS idx_facts_event_date_agent
    ON heart.facts(agent_id, event_date)
    WHERE event_date IS NOT NULL;

-- Partial index for backfill eligibility scan
CREATE INDEX IF NOT EXISTS idx_facts_event_date_unclassified_agent
    ON heart.facts(agent_id, learned_at)
    WHERE event_date_classified_at IS NULL;

COMMENT ON COLUMN heart.facts.event_date IS
    'F075: ISO date of the event this fact describes. NULL = stable fact (not event-anchored) OR pre-F075 row pending backfill.';
COMMENT ON COLUMN heart.facts.event_date_classified_at IS
    'F075: timestamp the backfill (or live extractor) classified this row for event_date. NULL = never classified, eligible for backfill. NOT NULL with event_date IS NULL = classified but no date found (terminal state, do NOT re-classify).';

-- F075 Layer 2: extend brain.graph_edges relation CHECK to allow 'happened_before'
-- Mirrors migration 051_f070_chunk_graph_edges.sql pattern that added 'part_of'/'summarized_by'.
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from',
            'part_of', 'summarized_by',
            'happened_before'
        )
    );

COMMIT;
