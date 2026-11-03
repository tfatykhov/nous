-- 095: allow the 'refines' graph-edge relation.
-- The F027 classifier links a new fact to an existing one it adds detail to
-- (REFINEMENT) with a 'refines' edge. The relation was never in
-- ck_edges_relation, so every such write failed inside its savepoint and was
-- dropped. No existing relation means the same thing: 'supports' and
-- 'evidence_for' are evidence, 'related_to' carries no direction.
-- The value list is the one from migration 055 plus 'refines'.
-- Plain statements, no DO block and no semicolon in a comment.
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from',
            'part_of', 'summarized_by', 'happened_before', 'co_occurred',
            'refines'
        )
    );
