-- Gap-1 Formation: experiential co-occurrence linking.
-- Facts that were learned from the SAME source episode (mentioned together in one
-- conversation/occasion) get a co-activation edge — the associative link the
-- cosine-only graph misses when two co-experienced facts share no words and aren't
-- semantically near (the no-handle case). Distinct from F076 co-mention (shared entity).
--
-- Adds:
--   relation 'co_occurred'        — carries the semantics so the agent can contextualise
--                                   ("these happened in the same session"), unlike generic related_to
--   extraction_method 'co_occurrence' — own provenance tier (telemetry; escapes inferred penalty)

BEGIN;

ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from',
            'part_of', 'summarized_by', 'happened_before', 'co_occurred'
        )
    );

ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_extraction_method_check;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_extraction_method;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_extraction_method CHECK (
        extraction_method IN ('deterministic', 'heuristic', 'inferred', 'co_mention', 'co_occurrence')
    );

COMMIT;
