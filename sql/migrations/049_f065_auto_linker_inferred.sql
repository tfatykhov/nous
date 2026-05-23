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
-- Discriminator: auto_linked=true AND extraction_method='heuristic'
-- AND weight < 1.0 AND relation NOT IN ('supersedes', 'contradicts').
--
-- The weight predicate splits the two writers that produce
-- discussed_in / extracted_from rows:
--
--   - `link_episode_deterministic` in nous/brain/graph_linker.py:316,341
--     writes weight=1.0 literally — structural provenance.
--   - `DecisionGraphLinker` in
--     nous/handlers/decision_graph_linker.py:118,161 writes
--     weight=float(cosine_similarity) — cosine-derived, statistically
--     always strictly < 1.0.
--
-- Without the weight predicate, two distinct concerns would collide:
--
--   1. Codex P1 round 1 (2026-05-23): excluding discussed_in /
--      extracted_from entirely from the backfill protects legacy
--      structural rows but ALSO skips legacy COSINE rows of those same
--      relations. After the migration, the same cosine writer would
--      produce split provenance depending on whether the row pre-dates
--      this migration.
--
--   2. Codex P1 round 2 (2026-05-23): the weight-based split picks up
--      the legacy cosine discussed_in / extracted_from rows while
--      leaving structural rows alone.
--
-- supersedes / contradicts already have correct extraction_method
-- from migration 047 (relation-based), so the relation filter is
-- belt-and-braces.
--
-- A future migration may want to additionally promote weight=1.0
-- discussed_in / extracted_from rows from 'heuristic' to
-- 'deterministic' to fully reflect their structural provenance, but
-- that is orthogonal to F065 and out of scope here.

BEGIN;

UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE auto_linked = true
  AND extraction_method = 'heuristic'
  AND weight < 1.0
  AND relation NOT IN ('supersedes', 'contradicts');

COMMIT;
