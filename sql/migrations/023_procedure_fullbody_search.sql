-- 023: Expand procedure search_tsv to include full body text (issue #197)
--
-- Before: to_tsvector('english', name || ' ' || COALESCE(description, ''))
-- After:  includes core_patterns, implementation_notes, goals, core_tools, core_concepts
--
-- PostgreSQL's array_to_string() is STABLE not IMMUTABLE, so it can't be used
-- directly in GENERATED ALWAYS columns. We create an IMMUTABLE wrapper.

-- Immutable wrapper for array_to_string (safe for text[] with constant delimiter)
CREATE OR REPLACE FUNCTION immutable_array_to_string(arr text[], sep text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT array_to_string(arr, sep) $$;

ALTER TABLE heart.procedures DROP COLUMN search_tsv;

ALTER TABLE heart.procedures
ADD COLUMN search_tsv tsvector
GENERATED ALWAYS AS (
  to_tsvector('english',
    name || ' '
    || COALESCE(description, '') || ' '
    || COALESCE(immutable_array_to_string(core_patterns, ' '), '') || ' '
    || COALESCE(immutable_array_to_string(implementation_notes, ' '), '') || ' '
    || COALESCE(immutable_array_to_string(goals, ' '), '') || ' '
    || COALESCE(immutable_array_to_string(core_tools, ' '), '') || ' '
    || COALESCE(immutable_array_to_string(core_concepts, ' '), '')
  )
) STORED;

-- Recreate GIN index (dropped with column)
CREATE INDEX idx_procedures_fts ON heart.procedures USING GIN(search_tsv);
