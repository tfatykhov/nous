-- F070 (2026-05-25): Allow chunk node type and new chunk relations in brain.graph_edges
--
-- Existing check constraints (added by migration 016) restrict:
--   source_type / target_type to ('decision', 'fact', 'episode', 'procedure')
--   relation to ('supports', 'contradicts', 'supersedes', 'related_to',
--                'caused_by', 'informed_by', 'evidence_for', 'discussed_in',
--                'extracted_from')
--
-- F070 adds 'chunk' as a node type and two new relations:
--   'part_of'         — chunk -> source episode (FK encoded as graph edge)
--   'summarized_by'   — chunk -> fact extracted from same episode
--
-- Migration is purely additive: existing edges remain valid. No data changes.

-- Note: 016 added ck_edges_* but didn't drop the auto-named CHECK constraints
-- from init.sql (graph_edges_*_check). Both are active. We need to drop both.

-- 1. Extend source_type
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_source_type;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_source_type_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_source_type CHECK (
        source_type IN ('decision', 'fact', 'episode', 'procedure', 'chunk')
    );

-- 2. Extend target_type
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_target_type;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_target_type_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_target_type CHECK (
        target_type IN ('decision', 'fact', 'episode', 'procedure', 'chunk')
    );

-- 3. Extend relation
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from',
            -- F070 additions:
            'part_of', 'summarized_by'
        )
    );
