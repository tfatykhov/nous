-- 023: Expand procedure search_tsv to include full body text (issue #197)
--
-- Before: to_tsvector('english', name || ' ' || COALESCE(description, ''))
-- After:  includes implementation_notes, goals, core_tools, core_concepts
--
-- PostgreSQL recomputes all rows automatically on generated column rebuild.
-- The GIN index idx_procedures_fts is also rebuilt automatically.

ALTER TABLE heart.procedures DROP COLUMN search_tsv;

ALTER TABLE heart.procedures
ADD COLUMN search_tsv tsvector
GENERATED ALWAYS AS (
  to_tsvector('english',
    name || ' '
    || COALESCE(description, '') || ' '
    || COALESCE(array_to_string(core_patterns, ' '), '') || ' '
    || COALESCE(array_to_string(implementation_notes, ' '), '') || ' '
    || COALESCE(array_to_string(goals, ' '), '') || ' '
    || COALESCE(array_to_string(core_tools, ' '), '') || ' '
    || COALESCE(array_to_string(core_concepts, ' '), '')
  )
) STORED;

-- Recreate GIN index (dropped with column)
CREATE INDEX idx_procedures_fts ON heart.procedures USING GIN(search_tsv);
