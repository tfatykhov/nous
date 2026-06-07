-- Procedure dedup: enforce one active procedure per (agent_id, lower(name)).
-- Audit: docs/reviews/procedure-subsystem-audit-2026-06-06.md (§6 B6).
--
-- Runs AFTER migration 058 (the one-time consolidation) in the same deploy, so the
-- active duplicates are already archived and this index creates cleanly. Pairs with the
-- case-insensitive get_procedure_by_name lookup (shipped in #488) so write paths
-- (learn_skill, bootstrap, K-line) can no longer mint a second active row that differs
-- only by casing/whitespace. Splitter-safe plain CREATE UNIQUE INDEX (no DO block / no
-- BEGIN/COMMIT — run_migrations wraps the file in one transaction).
--
-- If this ever fails with "could not create unique index ... contains duplicated values",
-- a duplicate active (agent_id, lower(name)) slipped past 058 — inspect with:
--   SELECT agent_id, lower(name), count(*) FROM heart.procedures WHERE active
--   GROUP BY 1,2 HAVING count(*) > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_procedures_active_lower_name
    ON heart.procedures (agent_id, lower(name))
    WHERE active;
