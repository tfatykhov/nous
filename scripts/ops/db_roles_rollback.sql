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
-- It must be letters and digits only, as the application puts it in its connection URL without escaping;
-- if the old one is not, use a new one (openssl rand -hex 24) in its place.
--
-- A refusal raises an error: psql prints "ERROR:  <reason>" and exits 3.

\set ON_ERROR_STOP on
\set VERBOSITY terse
SELECT current_setting('is_superuser') = 'on' AS is_super \gset
\if :is_super
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles_rollback.sql: connect as nous_admin'; END $$;
\endif
-- The password is tested in psql's own shell, so that no query but ALTER ROLE carries it.
\set legacy_password `printenv NOUS_LEGACY_DB_PASSWORD`
\set has_pw `test -n "$NOUS_LEGACY_DB_PASSWORD" && echo true || echo false`
\set pw_ok `case "$NOUS_LEGACY_DB_PASSWORD" in *[!A-Za-z0-9]*) echo false;; *) echo true;; esac`
\if :has_pw
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles_rollback.sql: set NOUS_LEGACY_DB_PASSWORD'; END $$;
\endif
\if :pw_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles_rollback.sql: NOUS_LEGACY_DB_PASSWORD must be letters and digits only (openssl rand -hex 24)'; END $$;
\endif
SELECT rolname AS bootstrap FROM pg_roles WHERE oid = 10 \gset
-- stored as a SCRAM verifier whatever the server's password_encryption: pg_hba accepts nothing else
SET password_encryption = 'scram-sha-256';
ALTER ROLE :"bootstrap" LOGIN PASSWORD :'legacy_password';
\echo 'db_roles_rollback.sql: done'
