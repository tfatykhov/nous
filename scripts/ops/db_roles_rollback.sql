-- scripts/ops/db_roles_rollback.sql
--
-- Undo the login lock-out of the bootstrap superuser, so the application can run as it again.
-- Ownership and grants stay as db_roles.sql left them (a superuser ignores both), and running
-- db_roles.sql again afterwards restores the lock-out.
--
--   docker exec -i -e NOUS_LEGACY_DB_PASSWORD nous-postgres \
--       psql -U nous_admin -d nous -X -v ON_ERROR_STOP=1 -f - < scripts/ops/db_roles_rollback.sql
--
-- NOUS_LEGACY_DB_PASSWORD is the password the application used before the roles were split (the
-- DB_PASSWORD line of the host .env). Then set NOUS_APP_DB_USER to the bootstrap role's name
-- (nous) and NOUS_APP_DB_PASSWORD to that password in .env, and recreate the application container.
--
-- A refusal raises an error: psql prints "ERROR:  <reason>" and exits 3.

\set ON_ERROR_STOP on
\set VERBOSITY terse
SELECT current_setting('is_superuser') = 'on' AS is_super \gset
\if :is_super
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles_rollback.sql: connect as nous_admin'; END $$;
\endif
\set legacy_password `printenv NOUS_LEGACY_DB_PASSWORD`
SELECT length(:'legacy_password') > 0 AS has_pw \gset
\if :has_pw
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles_rollback.sql: set NOUS_LEGACY_DB_PASSWORD'; END $$;
\endif
SELECT rolname AS bootstrap FROM pg_roles WHERE oid = 10 \gset
ALTER ROLE :"bootstrap" LOGIN PASSWORD :'legacy_password';
\echo 'db_roles_rollback.sql: done'
