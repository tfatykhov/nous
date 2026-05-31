-- F076: co-mention / shared-entity linking — allow 'co_mention' extraction_method
-- Spec: docs/features/F076-comention-entity-linking.md
-- Migration 047 added extraction_method with an inline (auto-named) CHECK named
-- graph_edges_extraction_method_check. Drop both the auto name and the ck_ variant,
-- then re-add including 'co_mention' (mirrors edge_provenance.VALID_METHODS).

BEGIN;

ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_extraction_method_check;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_extraction_method;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_extraction_method CHECK (
        extraction_method IN ('deterministic', 'heuristic', 'inferred', 'co_mention')
    );

COMMIT;
