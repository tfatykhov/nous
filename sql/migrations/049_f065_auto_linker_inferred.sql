-- Migration 049: F065 phase 4 follow-up — reclassify auto-linker rows as inferred.
--
-- Migration 047 backfilled extraction_method using ONLY the relation
-- string (supersedes→deterministic, contradicts→inferred, else→heuristic).
-- That left `inferred` empty in prod because the F027 classifier at
-- nous/heart/facts.py:35-70 is deliberately biased away from CONTRADICTION
-- (UPDATE is listed first to counter the false-positive bias the team
-- observed pre-F027). As a result the F065 penalty multiplier had
-- nothing to apply to and shipped dormant.
--
-- The fix at the write path (nous/brain/edge_provenance.py::classify +
-- source="auto_linker" call-site tagging in nous/brain/graph_linker.py)
-- makes new writes correctly classify cosine-auto-linker edges as
-- `inferred`. This migration backfills the EXISTING rows that were
-- written by the same code paths pre-fix.
--
-- Discriminator: auto_linked=true AND extraction_method='heuristic' AND
-- relation NOT IN the structural-provenance set:
--   - 'supersedes' and 'contradicts' are already classified by
--     migration 047 (supersedes→deterministic, contradicts→inferred);
--     re-running over them would be a no-op anyway.
--   - 'discussed_in' and 'extracted_from' are written by
--     `link_episode_deterministic` in nous/brain/graph_linker.py:320,345
--     which calls classify(..., source='structural') for new writes,
--     yielding 'deterministic'. But rows created before migration 047
--     were backfilled to 'heuristic' (relation-only rule). Codex P1
--     (2026-05-23): without excluding those relations here, the
--     backfill would silently flip legacy structural edges to
--     'inferred', applying the F065 penalty to deterministic
--     provenance.
--
-- F027 supersedes/contradicts writes also set auto_linked=true (see
-- nous/heart/facts.py:179) but the relation filter above protects them.

BEGIN;

UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE auto_linked = true
  AND extraction_method = 'heuristic'
  AND relation NOT IN (
      'supersedes', 'contradicts',         -- already classified by relation
      'discussed_in', 'extracted_from'      -- structural per graph_linker
  );

COMMIT;
