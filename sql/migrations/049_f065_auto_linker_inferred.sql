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
-- relation NOT IN ('supersedes', 'contradicts'). The relation filter
-- protects rows that migration 047 already classified by structural
-- provenance — those keep their tier.
--
-- F027 supersedes/contradicts writes also set auto_linked=true (see
-- nous/heart/facts.py:179) but their extraction_method was already set
-- correctly by relation in migration 047, so they're skipped by the
-- relation-filter.

BEGIN;

UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE auto_linked = true
  AND extraction_method = 'heuristic'
  AND relation NOT IN ('supersedes', 'contradicts');

COMMIT;
