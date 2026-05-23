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
-- Discriminator (relation-conditional, NOT a blanket weight gate):
--
--   - For `discussed_in` / `extracted_from`: weight=1.0 is the literal
--     signature of `link_episode_deterministic`
--     (nous/brain/graph_linker.py:316,341), which writes structural
--     provenance. weight<1.0 is the signature of `DecisionGraphLinker`
--     (nous/handlers/decision_graph_linker.py:118,161; cosine-derived).
--     Backfill only the latter.
--
--   - For every other auto_linker relation (related_to, evidence_for,
--     supports, caused_by, informed_by): there is no structural
--     writer. Every auto_linked row is cosine-derived, regardless of
--     weight. The weight predicate does NOT apply here, so cosine
--     writes that happen to land at weight=1.0 (identical embeddings —
--     duplicate-content facts the dedup pass missed) still get
--     backfilled. Without this, the same writer would produce split
--     provenance based on row age (Codex P1 round 3, 2026-05-23).
--
--   - `supersedes` / `contradicts`: already classified by relation
--     in migration 047 (deterministic / inferred). The relation
--     filter is belt-and-braces.
--
-- Refresher on why `refines` isn't a concern: it is written by
-- `Facts._create_graph_edge` at nous/heart/facts.py:564 with
-- auto_linked=true + weight=0.8, but the ck_edges_relation CHECK
-- constraint rejects it. The surrounding try/except swallows the
-- failure, so zero `refines` rows exist in either prod or eval DB
-- (verified 2026-05-23).
--
-- A future migration may promote the weight=1.0 structural
-- discussed_in/extracted_from rows from 'heuristic' to 'deterministic'
-- to fully reflect their structural provenance, but that is orthogonal
-- to F065 and out of scope here.

BEGIN;

UPDATE brain.graph_edges
SET extraction_method = 'inferred'
WHERE auto_linked = true
  AND extraction_method = 'heuristic'
  AND relation NOT IN ('supersedes', 'contradicts')
  AND (
      -- Cosine-derived discussed_in / extracted_from rows:
      -- DecisionGraphLinker writes weight=float(similarity), < 1.0.
      -- Structural writes (link_episode_deterministic) use weight=1.0.
      (relation IN ('discussed_in', 'extracted_from') AND weight < 1.0)
      -- All other auto_linker relations have no structural writer;
      -- weight is irrelevant for routing.
      OR relation NOT IN ('discussed_in', 'extracted_from')
  );

COMMIT;
