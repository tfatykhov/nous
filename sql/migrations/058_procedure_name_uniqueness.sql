-- Procedure dedup PR2: enforce one active procedure per (agent_id, lower(name)).
-- Audit: docs/reviews/procedure-subsystem-audit-2026-06-06.md (§6 B6).
--
-- ⚠ DEPLOY ORDER MATTERS. This runs on every deploy regardless of feature flags.
-- It CANNOT succeed while duplicate active names exist (e.g. the 3 live
-- "Send Email via Gmail SMTP" rows). Run the Phase 0 consolidation FIRST
-- (scripts/diag/proc_consolidate.py --commit on prod) so the DB is collision-free,
-- THEN deploy this migration. If deployed early, CREATE UNIQUE INDEX fails with
-- Postgres' own "could not create unique index ... contains duplicated values"
-- and the deploy aborts loudly — re-run the consolidation, then redeploy.
--
-- Splitter-safe for the auto-migrator: a plain CREATE UNIQUE INDEX, no DO $$ block
-- (the migrator's SQL splitter does not understand dollar quoting) and no
-- BEGIN/COMMIT (run_migrations wraps the file in one transaction).
--
-- Precheck the collision set before deploying with:
--   SELECT agent_id, lower(name), count(*) FROM heart.procedures WHERE active
--   GROUP BY agent_id, lower(name) HAVING count(*) > 1;
--
-- Case-insensitive, agent-scoped, active-only. Pairs with the case-insensitive
-- get_procedure_by_name lookup so write paths (learn_skill, bootstrap, K-line) can
-- no longer mint a second active row that differs only by casing/whitespace.

CREATE UNIQUE INDEX IF NOT EXISTS uq_procedures_active_lower_name
    ON heart.procedures (agent_id, lower(name))
    WHERE active;
