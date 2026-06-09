-- 060: Memory-integrity audit fixes (2026-06-09)
--
-- (a) Audit D1: remove graph edges left dangling by decision hard-deletes.
--     Migration 016 dropped the FK constraints on brain.graph_edges when the
--     endpoints went polymorphic, but Brain._delete kept relying on CASCADE
--     until the matching code fix in this change. Every decision delete since
--     then stranded its edges, feeding ghost node ids to spreading
--     activation, Brain.neighbors, and the adjacency boost, and inflating
--     the graph-density metric that auto-enables spreading activation.
--
-- (b) Audit D4: expression index for subject-supersession lookups.
--     FactManager._supersede_by_subject filters on lower(subject) for every
--     learned fact with a subject, and find_contradiction_candidates
--     self-joins on LOWER(subject). The only existing index is on the raw
--     column, which those expressions cannot use.

DELETE FROM brain.graph_edges e
WHERE e.source_type = 'decision'
  AND NOT EXISTS (SELECT 1 FROM brain.decisions d WHERE d.id = e.source_id);

DELETE FROM brain.graph_edges e
WHERE e.target_type = 'decision'
  AND NOT EXISTS (SELECT 1 FROM brain.decisions d WHERE d.id = e.target_id);

CREATE INDEX IF NOT EXISTS idx_facts_agent_subject_lower
    ON heart.facts (agent_id, lower(subject))
    WHERE active = TRUE;
