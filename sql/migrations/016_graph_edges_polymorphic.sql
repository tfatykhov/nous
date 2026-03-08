-- Migration 016: Make graph_edges polymorphic for cross-type edges (F022)

-- 1. Drop FK constraints
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT IF EXISTS graph_edges_source_id_fkey,
    DROP CONSTRAINT IF EXISTS graph_edges_target_id_fkey;

-- 2. Add new columns
ALTER TABLE brain.graph_edges
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    ADD COLUMN IF NOT EXISTS target_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    ADD COLUMN IF NOT EXISTS agent_id VARCHAR(100);

-- 3. Backfill agent_id from source decisions
UPDATE brain.graph_edges e
SET agent_id = d.agent_id
FROM brain.decisions d
WHERE e.source_id = d.id
  AND e.agent_id IS NULL;

-- 4. Set default for any orphaned edges
UPDATE brain.graph_edges SET agent_id = 'nous-default' WHERE agent_id IS NULL;

-- 5. Make agent_id NOT NULL
ALTER TABLE brain.graph_edges ALTER COLUMN agent_id SET NOT NULL;

-- 6. Add type check constraints
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_source_type CHECK (
        source_type IN ('decision', 'fact', 'episode', 'procedure')
    ),
    ADD CONSTRAINT ck_edges_target_type CHECK (
        target_type IN ('decision', 'fact', 'episode', 'procedure')
    );

-- 7. Extend relation check constraint
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from'
        )
    );

-- 8. New indexes
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_type ON brain.graph_edges(source_id, source_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_type ON brain.graph_edges(target_id, target_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_agent ON brain.graph_edges(agent_id);
