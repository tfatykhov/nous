-- scripts/ops/db_roles.sql
--
-- Database roles for the Nous deployment. Idempotent: safe to run again at any time.
--
-- Run it on the database host, as a superuser, with the two passwords in the environment
-- (they are read inside the container, so they never appear on a command line). A password is letters and
-- digits only (openssl rand -hex 24), because the application puts it in its connection URL without escaping:
--
--   docker exec -i -e NOUS_APP_DB_PASSWORD -e NOUS_SCORER_DB_PASSWORD nous-postgres \
--       psql -U nous_admin -d nous -X -v ON_ERROR_STOP=1 -f - < scripts/ops/db_roles.sql
--
-- The first run happens while the bootstrap superuser can still log in, so it uses -U nous
-- (the role the database image created). Later runs use -U nous_admin.
-- The first run changes the owner of every application table, which takes an ACCESS EXCLUSIVE lock on each:
-- stop the application first, or expect the run to wait for the application's open transactions.
--
-- Run it after the application has applied migrations 087 and 088: it refuses to run without scoring.task_scores
-- and scoring.scorer_heartbeat. Every refusal raises an error: psql prints "ERROR:  <reason>" and exits 3.
--
-- Roles after this script:
--   nous_admin   superuser, no password: reachable only through docker exec on the database host.
--                Owns the database, the scoring schema and its tables (scoring.task_scores, scoring.scorer_heartbeat).
--   nous_app     the application role: owns the brain, heart and nous_system schemas and their
--                tables, may create objects in the database, may read the two scoring tables.
--   nous_scorer  the scoring job: reads the tables named in task_score_job.SOURCE_TABLES, and
--                reads, inserts and updates scoring.task_scores and scoring.scorer_heartbeat. Nothing else.
--   <bootstrap>  the superuser the database image created (role oid 10). PostgreSQL does not allow
--                it to lose SUPERUSER, so its login is disabled and its password removed.
--
-- After the refusals the script sets up nous_admin and logs in as it (see below); everything after that login runs
-- in one transaction, so a failed step there leaves the database as it was. Every REVOKE cascades: it also
-- takes away what the role passed on through a grant option, so a run converges on a chain of grants.

\set ON_ERROR_STOP on
\set VERBOSITY terse

SELECT current_setting('is_superuser') = 'on' AS is_super \gset
\if :is_super
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: connect as a superuser (nous_admin, or the bootstrap role on the first run)'; END $$;
\endif

-- The bootstrap superuser is role oid 10. It cannot be demoted, so it is locked instead.
SELECT rolname AS bootstrap, rolname = 'nous_admin' AS bootstrap_is_admin FROM pg_roles WHERE oid = 10 \gset
\if :bootstrap_is_admin
    DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: the bootstrap superuser (role oid 10) is named nous_admin, and this script disables its login. Create the database with another POSTGRES_USER.'; END $$;
\endif
SELECT current_database() AS dbname \gset

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nous_app') AS app_exists,
       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nous_scorer') AS scorer_exists,
       (to_regclass('scoring.task_scores') IS NOT NULL
        AND to_regclass('scoring.scorer_heartbeat') IS NOT NULL) AS scoring_exists \gset

-- The passwords are tested in psql's own shell, so that no query but ALTER ROLE carries them.
\set app_password `printenv NOUS_APP_DB_PASSWORD`
\set scorer_password `printenv NOUS_SCORER_DB_PASSWORD`
\set has_app_pw `test -n "$NOUS_APP_DB_PASSWORD" && echo true || echo false`
\set has_scorer_pw `test -n "$NOUS_SCORER_DB_PASSWORD" && echo true || echo false`
\set app_pw_ok `case "$NOUS_APP_DB_PASSWORD" in *[!A-Za-z0-9]*) echo false;; *) echo true;; esac`
\set scorer_pw_ok `case "$NOUS_SCORER_DB_PASSWORD" in *[!A-Za-z0-9]*) echo false;; *) echo true;; esac`

\if :scoring_exists
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: scoring.task_scores or scoring.scorer_heartbeat does not exist. Start the application first (it applies migrations 087 and 088), then run this script.'; END $$;
\endif
\if :app_pw_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: NOUS_APP_DB_PASSWORD must be letters and digits only (openssl rand -hex 24)'; END $$;
\endif
\if :scorer_pw_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: NOUS_SCORER_DB_PASSWORD must be letters and digits only (openssl rand -hex 24)'; END $$;
\endif
\if :has_app_pw
\else
    \if :app_exists
    \else
        DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: set NOUS_APP_DB_PASSWORD for the first run'; END $$;
    \endif
\endif
\if :has_scorer_pw
\else
    \if :scorer_exists
    \else
        DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: set NOUS_SCORER_DB_PASSWORD for the first run'; END $$;
    \endif
\endif

-- pg_hba lets the two service roles in with scram-sha-256 only, which an MD5 verifier (password_encryption = md5)
-- or no password at all cannot pass. A new password is stored as SCRAM below; a run that sets none for a role
-- whose stored password is not a SCRAM verifier stops here.
SELECT EXISTS (SELECT 1 FROM pg_authid WHERE rolname = 'nous_app' AND (rolpassword IS NULL OR rolpassword NOT LIKE 'SCRAM-SHA-256$%')) AS app_pw_not_scram,
       EXISTS (SELECT 1 FROM pg_authid WHERE rolname = 'nous_scorer' AND (rolpassword IS NULL OR rolpassword NOT LIKE 'SCRAM-SHA-256$%')) AS scorer_pw_not_scram \gset
\if :app_pw_not_scram
    \if :has_app_pw
    \else
        DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: nous_app''s stored password is not a SCRAM verifier; set NOUS_APP_DB_PASSWORD (a new password is stored as SCRAM) and run again'; END $$;
    \endif
\endif
\if :scorer_pw_not_scram
    \if :has_scorer_pw
    \else
        DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: nous_scorer''s stored password is not a SCRAM verifier; set NOUS_SCORER_DB_PASSWORD (a new password is stored as SCRAM) and run again'; END $$;
    \endif
\endif

-- pg_hba_file_rules shows the file on disk, which the server enforces only from its next reload: reload first, so
-- that (a file with a parse error, which keeps the old rules, is refused below) the server enforces what is checked.
DO $$
BEGIN
    IF NOT pg_reload_conf() THEN
        RAISE EXCEPTION 'db_roles.sql: the server did not accept the configuration reload (pg_reload_conf returned false); fix that, then run this script';
    END IF;
    PERFORM pg_sleep(1);
END $$;

-- nous_admin gets no password and the run commits before verify runs, so pg_hba.conf must already keep every
-- network login beyond loopback on scram-sha-256 or reject. verify_db_roles.sql checks the same conditions.
SELECT NOT EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL)
       AND NOT EXISTS (SELECT 1 FROM pg_hba_file_rules r WHERE r.error IS NULL AND r.type <> 'local' AND NOT ((r.address = '127.0.0.1' AND r.netmask = '255.255.255.255') OR (r.address = '::1' AND r.netmask = 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff') OR (r.address = 'localhost' AND r.netmask IS NULL)) AND r.auth_method NOT IN ('scram-sha-256', 'reject')) AS hba_ok \gset
\if :hba_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'db_roles.sql: pg_hba.conf has a line that does not parse or lets a network login in without scram-sha-256; fix it, reload (select pg_reload_conf()), then run this script'; END $$;
\endif

-- Prove that nous_admin can log in before the bootstrap role is locked. pg_hba_file_rules drops the quoting of
-- selectors ("all" and all, "+nous_admin" and +nous_admin read the same), so the test is a real login, not a
-- prediction. \connect - keeps the database, host and port, so it takes this session's path: the local socket
-- under docker exec, TCP when this session came over TCP. If the login fails, psql reports it and exits 3 before
-- anything below runs (without a terminal it may first print a password prompt; the login still fails, since
-- nous_admin has no password). The bootstrap role then keeps its login, and nous_admin exists as a superuser
-- without a password, which only a pg_hba line that trusts it lets in.
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nous_admin') AS admin_exists \gset
\if :admin_exists
\else
    CREATE ROLE nous_admin;
\endif
ALTER ROLE nous_admin WITH SUPERUSER LOGIN PASSWORD NULL;
\echo 'db_roles.sql: logging in as nous_admin through the same connection path, without a password'
\connect - nous_admin

BEGIN;

-- 1. Roles. Created by a superuser, so the creator holds no membership in them.
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nous_app') THEN
        CREATE ROLE nous_app;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nous_scorer') THEN
        CREATE ROLE nous_scorer;
    END IF;
END
$roles$;

ALTER ROLE nous_app WITH NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS LOGIN;
ALTER ROLE nous_scorer WITH NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS LOGIN;

-- A new password is stored as a SCRAM verifier whatever the server's password_encryption.
SET LOCAL password_encryption = 'scram-sha-256';
\if :has_app_pw
    ALTER ROLE nous_app PASSWORD :'app_password';
\endif
\if :has_scorer_pw
    ALTER ROLE nous_scorer PASSWORD :'scorer_password';
\endif

-- Neither the administrator nor the scorer resolves names through a schema named after the login: the application
-- role may create schemas, and a schema called nous_admin or nous_scorer would otherwise come first.
ALTER ROLE nous_admin SET search_path = pg_catalog, public;

-- The scorer reads tables that the application owns. Pin what it resolves and refuse row policies,
-- so that nothing the application defines can run with the scorer's privileges.
ALTER ROLE nous_scorer SET search_path = pg_catalog, scoring;
ALTER ROLE nous_scorer SET row_security = off;

-- 2. Database: the administrator owns it (so grants on it are the administrator's alone). Only the
--    two named roles may connect.
ALTER DATABASE :"dbname" OWNER TO nous_admin;
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC, nous_app, nous_scorer CASCADE;
GRANT CONNECT, TEMP ON DATABASE :"dbname" TO nous_app;
GRANT CREATE ON DATABASE :"dbname" TO nous_app;
GRANT CONNECT ON DATABASE :"dbname" TO nous_scorer;

-- 3. Ownership. The application owns its three schemas and everything in them, the administrator owns
--    the scoring schema. Only objects with another owner are touched, so a second run changes nothing.
DO $own$
DECLARE
    s record;
    o record;
BEGIN
    FOR s IN
        SELECT * FROM (VALUES ('brain', 'nous_app'), ('heart', 'nous_app'), ('nous_system', 'nous_app'),
                              ('scoring', 'nous_admin')) AS v(sch, owner_role)
    LOOP
        IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = s.sch) <> s.owner_role THEN
            EXECUTE format('ALTER SCHEMA %I OWNER TO %I', s.sch, s.owner_role);
        END IF;
        -- tables, views, foreign tables first (their indexes, toast tables and owned sequences follow)
        FOR o IN
            SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = s.sch AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_get_userbyid(c.relowner) <> s.owner_role
        LOOP
            EXECUTE format('ALTER %s %I.%I OWNER TO %I',
                           CASE o.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                          WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
                           s.sch, o.relname, s.owner_role);
        END LOOP;
        FOR o IN
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = s.sch AND c.relkind = 'S' AND pg_get_userbyid(c.relowner) <> s.owner_role
        LOOP
            EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO %I', s.sch, o.relname, s.owner_role);
        END LOOP;
        FOR o IN
            SELECT p.oid::regprocedure AS sig, p.prokind FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = s.sch AND pg_get_userbyid(p.proowner) <> s.owner_role
        LOOP
            EXECUTE format('ALTER %s %s OWNER TO %I',
                           CASE o.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
                           o.sig, s.owner_role);
        END LOOP;
        -- enums, domains and ranges (a range's multirange type follows it; PostgreSQL refuses to alter one directly)
        FOR o IN
            SELECT t.oid::regtype AS tname, t.typtype FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = s.sch AND t.typtype IN ('e', 'd', 'r') AND pg_get_userbyid(t.typowner) <> s.owner_role
        LOOP
            EXECUTE format('ALTER %s %s OWNER TO %I', CASE o.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
                           o.tname, s.owner_role);
        END LOOP;
    END LOOP;
END
$own$;

-- 4. scoring: owned by nous_admin, written by the scorer, read by the application.
REVOKE ALL ON SCHEMA scoring FROM PUBLIC, nous_app, nous_scorer CASCADE;
REVOKE ALL ON ALL TABLES IN SCHEMA scoring FROM PUBLIC, nous_app, nous_scorer CASCADE;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA scoring FROM PUBLIC, nous_app, nous_scorer CASCADE;
REVOKE ALL ON ALL ROUTINES IN SCHEMA scoring FROM PUBLIC, nous_app, nous_scorer CASCADE;
-- A function nous_admin creates in scoring later gets PUBLIC's EXECUTE until this script runs again, and verify
-- fails on it meanwhile. No default privilege is set for nous_admin to prevent that: it also creates extension
-- functions (CREATE or ALTER EXTENSION), which the application must be able to execute.
-- Column privileges (pg_attribute.attacl) sit apart from a table's ACL. A table-level REVOKE also takes back the
-- same grantee's column privileges, but in brain, heart and nous_system the table-level revokes name only the
-- scorer, so a column grant to PUBLIC there would survive them. Take back every column privilege of PUBLIC, the
-- application and the scorer in scoring, and of PUBLIC and the scorer in the application schemas (the application
-- owns those tables; its own grants stay). The table-level grants below then set what each role may do.
DO $columns$
DECLARE
    c record;
BEGIN
    FOR c IN
        SELECT a.attrelid::regclass AS rel, a.attname, n.nspname
          FROM pg_attribute a JOIN pg_class r ON r.oid = a.attrelid JOIN pg_namespace n ON n.oid = r.relnamespace
         WHERE a.attacl IS NOT NULL AND a.attnum > 0 AND NOT a.attisdropped
           AND n.nspname IN ('scoring', 'brain', 'heart', 'nous_system')
         ORDER BY 1, 2
    LOOP
        IF c.nspname = 'scoring' THEN
            EXECUTE format('REVOKE ALL (%I) ON %s FROM PUBLIC, nous_app, nous_scorer CASCADE', c.attname, c.rel);
        ELSE
            EXECUTE format('REVOKE ALL (%I) ON %s FROM PUBLIC, nous_scorer CASCADE', c.attname, c.rel);
        END IF;
    END LOOP;
END
$columns$;
GRANT USAGE ON SCHEMA scoring TO nous_app, nous_scorer;
GRANT SELECT ON ALL TABLES IN SCHEMA scoring TO nous_app;
-- (the ownership loop of section 3 and the line above cover every table in scoring, so both are owned by
-- nous_admin and readable by the application)
GRANT SELECT, INSERT, UPDATE ON scoring.task_scores TO nous_scorer;
GRANT SELECT, INSERT, UPDATE ON scoring.scorer_heartbeat TO nous_scorer;
-- The application role may set default privileges for objects it creates, in any schema it can use. In scoring
-- it creates nothing, so remove every default it has set there: take away each grantee (PUBLIC, the administrator,
-- the scorer, itself). A per-schema entry left with no grantee is deleted, which leaves the built-in default.
DO $defacl$
DECLARE
    d record;
    -- pg_default_acl.defaclobjtype to the keyword ALTER DEFAULT PRIVILEGES takes
    kinds constant jsonb := '{"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES"}';
BEGIN
    FOR d IN
        SELECT DISTINCT x.defaclobjtype AS objtype, a.grantee
          FROM pg_default_acl x, LATERAL aclexplode(x.defaclacl) a
         WHERE x.defaclrole = 'nous_app'::regrole AND x.defaclnamespace = 'scoring'::regnamespace
    LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE nous_app IN SCHEMA scoring REVOKE ALL ON %s FROM %s',
                       kinds ->> d.objtype::text,
                       CASE WHEN d.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(d.grantee)) END);
    END LOOP;
END
$defacl$;
ALTER DEFAULT PRIVILEGES FOR ROLE nous_admin IN SCHEMA scoring REVOKE ALL ON TABLES FROM PUBLIC, nous_app, nous_scorer;
ALTER DEFAULT PRIVILEGES FOR ROLE nous_admin IN SCHEMA scoring GRANT SELECT ON TABLES TO nous_app;

-- 5. The scorer reads exactly the tables that task_score_job.SOURCE_TABLES names (a test compares this
--    list with that constant), and nothing else in the application schemas.
REVOKE ALL ON ALL TABLES IN SCHEMA brain, heart, nous_system FROM nous_scorer CASCADE;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA brain, heart, nous_system FROM nous_scorer CASCADE;
REVOKE ALL ON ALL ROUTINES IN SCHEMA brain, heart, nous_system FROM nous_scorer CASCADE;
-- PUBLIC's default EXECUTE would let the scorer run any function in these schemas: take it from the existing ones,
-- and from those nous_app creates later (globally: a per-schema REVOKE of a built-in default changes nothing).
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA brain, heart, nous_system FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE nous_app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
GRANT USAGE ON SCHEMA brain, heart, nous_system TO nous_scorer;
-- source tables begin
GRANT SELECT ON brain.decisions TO nous_scorer;
GRANT SELECT ON brain.intention_arrivals TO nous_scorer;
GRANT SELECT ON brain.intention_proposals TO nous_scorer;
GRANT SELECT ON brain.intentions TO nous_scorer;
GRANT SELECT ON heart.subtasks TO nous_scorer;
GRANT SELECT ON nous_system.compensation_snapshots TO nous_scorer;
GRANT SELECT ON nous_system.dag_nodes TO nous_scorer;
GRANT SELECT ON nous_system.events TO nous_scorer;
GRANT SELECT ON nous_system.execution_dags TO nous_scorer;
GRANT SELECT ON nous_system.execution_ledger TO nous_scorer;
-- source tables end

-- 5b. No role is a member of the three roles above, and none of them is a member of any role.
DO $members$
DECLARE
    m record;
BEGIN
    FOR m IN
        SELECT g.rolname AS grp, r.rolname AS mem
          FROM pg_auth_members am JOIN pg_roles g ON g.oid = am.roleid JOIN pg_roles r ON r.oid = am.member
         WHERE g.rolname IN ('nous_app', 'nous_scorer', 'nous_admin')
            OR r.rolname IN ('nous_app', 'nous_scorer', 'nous_admin')
         ORDER BY 1, 2
    LOOP
        EXECUTE format('REVOKE %I FROM %I CASCADE', m.grp, m.mem);
    END LOOP;
END
$members$;

-- 5c. Neither the application, the scorer nor PUBLIC holds a privilege on a server parameter (SET or ALTER SYSTEM).
--     %I quotes a dotted name such as plpgsql.extra_warnings as one identifier, which the command accepts.
DO $params$
DECLARE
    p record;
BEGIN
    FOR p IN
        SELECT DISTINCT x.parname, a.grantee
          FROM pg_parameter_acl x, LATERAL aclexplode(x.paracl) a
         WHERE a.grantee IN (0, 'nous_app'::regrole, 'nous_scorer'::regrole)
         ORDER BY 1, 2
    LOOP
        EXECUTE format('REVOKE ALL ON PARAMETER %I FROM %s CASCADE', p.parname,
                       CASE WHEN p.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(p.grantee)) END);
    END LOOP;
END
$params$;

-- 6. Last: the bootstrap superuser can no longer log in, and has no password. Everything before
--    this ran as a role that holds its own privileges, so nothing above depended on it.
ALTER ROLE :"bootstrap" NOLOGIN PASSWORD NULL;

-- 7. Postconditions. A failure here rolls the whole run back.
DO $post$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(rolname, ', ') INTO bad FROM pg_roles
     WHERE rolname IN ('nous_app', 'nous_scorer')
       AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls);
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'role has an elevated attribute: %', bad; END IF;

    SELECT string_agg(rolname, ', ') INTO bad FROM pg_authid
     WHERE rolname IN ('nous_app', 'nous_scorer') AND (rolpassword IS NULL OR rolpassword NOT LIKE 'SCRAM-SHA-256$%');
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'password is not a SCRAM verifier: %', bad; END IF;

    SELECT string_agg(r.rolname || ' in ' || g.rolname, ', ') INTO bad
      FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid
     WHERE r.rolname IN ('nous_app', 'nous_scorer') OR g.rolname IN ('nous_app', 'nous_scorer', 'nous_admin');
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'unexpected role membership: %', bad; END IF;

    SELECT string_agg(DISTINCT x.parname || ' to '
                      || CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END, ', ') INTO bad
      FROM pg_parameter_acl x, LATERAL aclexplode(x.paracl) a
     WHERE a.grantee IN (0, 'nous_app'::regrole, 'nous_scorer'::regrole);
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'parameter privilege granted: %', bad; END IF;

    SELECT string_agg(n.nspname || '.' || c.relname, ', ') INTO bad
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
       AND ((n.nspname IN ('brain', 'heart', 'nous_system') AND pg_get_userbyid(c.relowner) <> 'nous_app')
         OR (n.nspname = 'scoring' AND pg_get_userbyid(c.relowner) <> 'nous_admin'));
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'object with the wrong owner: %', bad; END IF;

    IF (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) <> 'nous_admin' THEN
        RAISE EXCEPTION 'nous_admin does not own the database';
    END IF;

    IF NOT (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'nous_admin') THEN
        RAISE EXCEPTION 'nous_admin cannot log in';
    END IF;
END
$post$;

COMMIT;

\echo 'db_roles.sql: done'
