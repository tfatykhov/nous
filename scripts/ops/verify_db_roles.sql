-- scripts/ops/verify_db_roles.sql
--
-- Checks the roles that scripts/ops/db_roles.sql sets up. It first reloads the server's configuration (below);
-- apart from that it is read-only in effect: the checks run in one transaction that is rolled back, so a
-- statement that unexpectedly succeeds changes nothing.
--
--   docker exec -i nous-postgres psql -U nous_admin -d nous -X -v ON_ERROR_STOP=1 -f - < scripts/ops/verify_db_roles.sql
--
-- Exit status 0 and a final "verify_db_roles: N checks passed" line mean every check held. Any other
-- outcome raises an error ("ERROR:  FAIL: <check> ...", or "ERROR:  <reason>" for a refusal) and psql exits 3.
--
-- nous_admin has no password and is meant to be reachable only through docker exec on the database host.
-- That holds only while pg_hba.conf lets nothing but the local socket and loopback in without a password:
-- every network line beyond loopback, whichever users it names, must use scram-sha-256 (a role with no
-- password cannot pass it) or reject, and no line beyond loopback may use trust, md5 or password. The pg_hba
-- checks below read pg_hba_file_rules, which shows the file on disk; this script reloads the configuration and
-- waits until it has taken effect before it reads it, so the rules it audits are the ones the server enforces.
-- That nous_admin can log in through the local socket is shown by this script itself: it runs as nous_admin
-- through docker exec.

\set ON_ERROR_STOP on
\set VERBOSITY terse

SELECT current_setting('is_superuser') = 'on' AS is_super \gset
\if :is_super
\else
    DO $$ BEGIN RAISE EXCEPTION 'verify_db_roles: connect as nous_admin'; END $$;
\endif

-- Reload, as db_roles.sql does: pg_reload_conf() only signals the postmaster, which re-reads the files and then
-- signals every session. This session takes the new configuration at the start of its next top-level command
-- after that signal, and only then does its pg_conf_load_time() advance; within one command it never does. So the
-- reload, the wait and each look at pg_conf_load_time() are separate top-level commands.
SELECT pg_conf_load_time() AS conf_t0 \gset
DO $$
BEGIN
    IF NOT pg_reload_conf() THEN
        RAISE EXCEPTION 'verify_db_roles: the server did not accept the configuration reload (pg_reload_conf returned false); fix that, then run verify again';
    END IF;
END $$;
DO $$ BEGIN PERFORM pg_sleep(1); END $$;
SELECT pg_conf_load_time() > :'conf_t0'::timestamptz AS conf_reloaded \gset
\if :conf_reloaded
\else
    DO $$ BEGIN PERFORM pg_sleep(4); END $$;
    SELECT pg_conf_load_time() > :'conf_t0'::timestamptz AS conf_reloaded \gset
    \if :conf_reloaded
    \else
        DO $$ BEGIN RAISE EXCEPTION 'verify_db_roles: the server has not finished reloading its configuration; run verify again'; END $$;
    \endif
\endif

SELECT to_regclass('scoring.task_scores') IS NOT NULL
       AND to_regclass('scoring.scorer_heartbeat') IS NOT NULL AS scoring_exists \gset
\if :scoring_exists
\else
    DO $$ BEGIN RAISE EXCEPTION 'FAIL: scoring.task_scores or scoring.scorer_heartbeat does not exist. Start the application first (it applies migrations 087 and 088), then run db_roles.sql and this script.'; END $$;
\endif

BEGIN;

DO $verify$
DECLARE
    c record;
    ok boolean;
    flag boolean;
    err text;
    passed int := 0;
    upsert text := 'INSERT INTO scoring.task_scores (agent_id, task_kind, task_id, finished_at, scorer_version, outcome) '
                   || 'VALUES (''verify'', ''subtask'', ''verify-1'', now(), 0, ''done_self'') '
                   || 'ON CONFLICT (agent_id, task_kind, task_id, scorer_version) DO UPDATE SET scored_at = now()';
    heartbeat text := 'INSERT INTO scoring.scorer_heartbeat (agent_id, scorer_version, last_run_at, last_status, last_ok_at, rows_submitted) '
                      || 'VALUES (''verify'', 0, now(), ''ok'', now(), 0) '
                      || 'ON CONFLICT (agent_id) DO UPDATE SET last_run_at = now()';
BEGIN
    FOR c IN
        SELECT * FROM (VALUES
        -- (label, run as, statement, expectation, check evaluated afterwards as the administrator)
        -- nous_app: reads the scores and cannot change them
        ('app reads scoring.task_scores', 'nous_app', 'SELECT count(*) FROM scoring.task_scores', 'ok', NULL),
        ('app cannot INSERT', 'nous_app', upsert, 'error', NULL),
        ('app cannot UPDATE', 'nous_app', 'UPDATE scoring.task_scores SET score = 1', 'error', NULL),
        ('app cannot DELETE', 'nous_app', 'DELETE FROM scoring.task_scores', 'error', NULL),
        ('app cannot TRUNCATE', 'nous_app', 'TRUNCATE scoring.task_scores', 'error', NULL),
        ('app cannot DROP the table', 'nous_app', 'DROP TABLE scoring.task_scores', 'error', NULL),
        ('app cannot ALTER the table', 'nous_app', 'ALTER TABLE scoring.task_scores ADD COLUMN x int', 'error', NULL),
        ('app cannot add a trigger', 'nous_app',
            'CREATE TRIGGER t BEFORE INSERT ON scoring.task_scores FOR EACH ROW EXECUTE FUNCTION update_timestamp()', 'error', NULL),
        ('app cannot DROP SCHEMA scoring', 'nous_app', 'DROP SCHEMA scoring CASCADE', 'error', NULL),
        ('app cannot create in scoring', 'nous_app', 'CREATE TABLE scoring.x (i int)', 'error', NULL),
        ('app GRANT to itself has no effect', 'nous_app', 'GRANT INSERT ON scoring.task_scores TO nous_app', 'ok',
            'NOT has_table_privilege(''nous_app'', ''scoring.task_scores'', ''INSERT'')'),
        ('app reads scoring.scorer_heartbeat', 'nous_app', 'SELECT count(*) FROM scoring.scorer_heartbeat', 'ok', NULL),
        ('app cannot INSERT a heartbeat', 'nous_app', heartbeat, 'error', NULL),
        ('app cannot UPDATE a heartbeat', 'nous_app', 'UPDATE scoring.scorer_heartbeat SET rows_submitted = 1', 'error', NULL),
        ('app cannot DELETE a heartbeat', 'nous_app', 'DELETE FROM scoring.scorer_heartbeat', 'error', NULL),
        ('app cannot TRUNCATE the heartbeat table', 'nous_app', 'TRUNCATE scoring.scorer_heartbeat', 'error', NULL),
        ('app cannot DROP the heartbeat table', 'nous_app', 'DROP TABLE scoring.scorer_heartbeat', 'error', NULL),
        ('app cannot ALTER the heartbeat table', 'nous_app', 'ALTER TABLE scoring.scorer_heartbeat ADD COLUMN x int', 'error', NULL),
        -- nous_app: cannot touch roles, the database, extensions or the server
        ('app cannot ALTER the scorer password', 'nous_app', 'ALTER ROLE nous_scorer PASSWORD ''x''', 'error', NULL),
        ('app cannot change the scorer search_path', 'nous_app', 'ALTER ROLE nous_scorer SET search_path = public', 'error', NULL),
        ('app cannot CREATE ROLE', 'nous_app', 'CREATE ROLE zz_probe', 'error', NULL),
        ('app cannot make itself superuser', 'nous_app', 'ALTER ROLE nous_app SUPERUSER', 'error', NULL),
        ('app cannot join the scorer role', 'nous_app', 'GRANT nous_scorer TO nous_app', 'error', NULL),
        ('app REVOKE CONNECT from the scorer has no effect', 'nous_app',
            'REVOKE CONNECT ON DATABASE ' || quote_ident(current_database()) || ' FROM nous_scorer', 'ok',
            'has_database_privilege(''nous_scorer'', current_database(), ''CONNECT'')'),
        ('app cannot take over the database', 'nous_app',
            'ALTER DATABASE ' || quote_ident(current_database()) || ' OWNER TO nous_app', 'error', NULL),
        ('app cannot drop the vector extension', 'nous_app', 'DROP EXTENSION vector', 'error', NULL),
        ('app cannot update an extension', 'nous_app', 'ALTER EXTENSION vector UPDATE', 'error', NULL),
        ('app CREATE EXTENSION IF NOT EXISTS is a no-op', 'nous_app', 'CREATE EXTENSION IF NOT EXISTS vector', 'ok', NULL),
        ('app CREATE SCHEMA IF NOT EXISTS is a no-op', 'nous_app', 'CREATE SCHEMA IF NOT EXISTS scoring', 'ok', NULL),
        ('app cannot run a program', 'nous_app', 'COPY (SELECT 1) TO PROGRAM ''true''', 'error', NULL),
        ('app cannot read a server file', 'nous_app', 'SELECT pg_read_file(''/etc/hostname'')', 'error', NULL),
        ('no parameter privilege (SET or ALTER SYSTEM) is granted to the app, the scorer or PUBLIC', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_parameter_acl p, LATERAL aclexplode(p.paracl) a WHERE a.grantee IN (0, ''nous_app''::regrole, ''nous_scorer''::regrole))', 'true', NULL),
        ('app cannot write to public', 'nous_app', 'CREATE TABLE public.zz (i int)', 'error', NULL),
        ('app still works in its own schemas', 'nous_app',
            'CREATE TABLE brain.zz_verify (i int); INSERT INTO brain.zz_verify VALUES (1); DROP TABLE brain.zz_verify', 'ok', NULL),
        -- nous_scorer: reads its inputs, writes only the scores
        ('scorer reads a source table', 'nous_scorer', 'SELECT count(*) FROM heart.subtasks', 'ok', NULL),
        ('scorer upserts a score', 'nous_scorer', upsert, 'ok', NULL),
        ('scorer upserts a heartbeat', 'nous_scorer', heartbeat, 'ok', NULL),
        ('scorer cannot read other tables', 'nous_scorer', 'SELECT count(*) FROM heart.facts', 'error', NULL),
        ('scorer cannot write a source table', 'nous_scorer', 'DELETE FROM heart.subtasks', 'error', NULL),
        ('scorer cannot update a source table', 'nous_scorer', 'UPDATE nous_system.execution_ledger SET status = status', 'error', NULL),
        ('scorer cannot delete a score', 'nous_scorer', 'DELETE FROM scoring.task_scores', 'error', NULL),
        ('scorer cannot truncate the scores', 'nous_scorer', 'TRUNCATE scoring.task_scores', 'error', NULL),
        ('scorer cannot delete a heartbeat', 'nous_scorer', 'DELETE FROM scoring.scorer_heartbeat', 'error', NULL),
        ('scorer cannot truncate the heartbeat table', 'nous_scorer', 'TRUNCATE scoring.scorer_heartbeat', 'error', NULL),
        ('scorer cannot create a table', 'nous_scorer', 'CREATE TABLE scoring.zz (i int)', 'error', NULL),
        ('scorer cannot create a temp table', 'nous_scorer', 'CREATE TEMP TABLE zz (i int)', 'error', NULL),
        -- catalog state (as the administrator)
        ('no elevated attribute on the app or scorer role', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN (''nous_app'', ''nous_scorer'') AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls))', 'true', NULL),
        ('no role is a member of the app, scorer or admin role, and neither is a member of anything', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid WHERE r.rolname IN (''nous_app'', ''nous_scorer'') OR g.rolname IN (''nous_app'', ''nous_scorer'', ''nous_admin''))', 'true', NULL),
        ('neither limited role can SET ROLE to another role', 'nous_admin',
            'SELECT NOT pg_has_role(''nous_app'', ''nous_scorer'', ''MEMBER'') AND NOT pg_has_role(''nous_app'', ''nous_admin'', ''MEMBER'') AND NOT pg_has_role(''nous_app'', (SELECT rolname FROM pg_roles WHERE oid = 10), ''MEMBER'') AND NOT pg_has_role(''nous_scorer'', ''nous_app'', ''MEMBER'') AND NOT pg_has_role(''nous_scorer'', ''nous_admin'', ''MEMBER'') AND NOT pg_has_role(''nous_scorer'', (SELECT rolname FROM pg_roles WHERE oid = 10), ''MEMBER'')', 'true', NULL),
        ('nous_admin has no password', 'nous_admin',
            'SELECT rolpassword IS NULL AND rolsuper AND rolcanlogin FROM pg_authid WHERE rolname = ''nous_admin''', 'true', NULL),
        ('the bootstrap superuser cannot log in and has no password', 'nous_admin',
            'SELECT rolpassword IS NULL AND NOT rolcanlogin FROM pg_authid WHERE oid = 10', 'true', NULL),
        ('the only superusers are nous_admin and the bootstrap role', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolsuper AND oid <> 10 AND rolname <> ''nous_admin'')', 'true', NULL),
        ('nous_admin owns the database', 'nous_admin',
            'SELECT pg_get_userbyid(datdba) = ''nous_admin'' FROM pg_database WHERE datname = current_database()', 'true', NULL),
        ('PUBLIC cannot connect to the database', 'nous_admin',
            'SELECT NOT has_database_privilege(0::oid, (SELECT oid FROM pg_database WHERE datname = current_database()), ''CONNECT'')', 'true', NULL),
        ('the app owns every object in its schemas', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace::regnamespace::text IN (''brain'', ''heart'', ''nous_system'') AND c.relkind IN (''r'', ''p'', ''v'', ''m'', ''f'', ''S'') AND pg_get_userbyid(c.relowner) <> ''nous_app'')', 'true', NULL),
        ('nous_admin owns the scoring schema and its tables', 'nous_admin',
            'SELECT pg_get_userbyid((SELECT nspowner FROM pg_namespace WHERE nspname = ''scoring'')) = ''nous_admin'' AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = ''scoring''::regnamespace AND c.relkind IN (''r'', ''p'', ''v'', ''m'', ''f'', ''S'') AND pg_get_userbyid(c.relowner) <> ''nous_admin'')', 'true', NULL),
        ('PUBLIC has nothing on scoring', 'nous_admin',
            'SELECT NOT has_schema_privilege(0::oid, ''scoring'', ''USAGE'') AND NOT has_table_privilege(0::oid, ''scoring.task_scores'', ''SELECT'') AND NOT has_table_privilege(0::oid, ''scoring.scorer_heartbeat'', ''SELECT'')', 'true', NULL),
        ('scorer holds SELECT on exactly as many application tables as SOURCE_TABLES names, and nothing else there', 'nous_admin',
            'SELECT (SELECT count(*) FROM pg_class c WHERE c.relnamespace::regnamespace::text IN (''brain'', ''heart'', ''nous_system'') AND c.relkind IN (''r'', ''p'', ''v'', ''m'', ''f'') AND has_table_privilege(''nous_scorer'', c.oid, ''SELECT'')) = 12 AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace::regnamespace::text IN (''brain'', ''heart'', ''nous_system'') AND c.relkind IN (''r'', ''p'', ''v'', ''m'', ''f'') AND (has_table_privilege(''nous_scorer'', c.oid, ''INSERT'') OR has_table_privilege(''nous_scorer'', c.oid, ''UPDATE'') OR has_table_privilege(''nous_scorer'', c.oid, ''DELETE'') OR has_table_privilege(''nous_scorer'', c.oid, ''TRUNCATE'') OR has_table_privilege(''nous_scorer'', c.oid, ''TRIGGER'') OR has_table_privilege(''nous_scorer'', c.oid, ''REFERENCES'')))', 'true', NULL),
        ('scorer holds only SELECT, INSERT, UPDATE on task_scores', 'nous_admin',
            'SELECT has_table_privilege(''nous_scorer'', ''scoring.task_scores'', ''SELECT'') AND has_table_privilege(''nous_scorer'', ''scoring.task_scores'', ''INSERT'') AND has_table_privilege(''nous_scorer'', ''scoring.task_scores'', ''UPDATE'') AND NOT has_table_privilege(''nous_scorer'', ''scoring.task_scores'', ''DELETE'') AND NOT has_table_privilege(''nous_scorer'', ''scoring.task_scores'', ''TRUNCATE'') AND NOT has_table_privilege(''nous_scorer'', ''scoring.task_scores'', ''TRIGGER'')', 'true', NULL),
        ('scorer holds only SELECT, INSERT, UPDATE on scorer_heartbeat', 'nous_admin',
            'SELECT has_table_privilege(''nous_scorer'', ''scoring.scorer_heartbeat'', ''SELECT'') AND has_table_privilege(''nous_scorer'', ''scoring.scorer_heartbeat'', ''INSERT'') AND has_table_privilege(''nous_scorer'', ''scoring.scorer_heartbeat'', ''UPDATE'') AND NOT has_table_privilege(''nous_scorer'', ''scoring.scorer_heartbeat'', ''DELETE'') AND NOT has_table_privilege(''nous_scorer'', ''scoring.scorer_heartbeat'', ''TRUNCATE'') AND NOT has_table_privilege(''nous_scorer'', ''scoring.scorer_heartbeat'', ''TRIGGER'')', 'true', NULL),
        ('scoring.task_scores admits every outcome the scorer writes, done_checked included (re-run db_roles.sql if not)', 'nous_admin',
            'SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = ''scoring.task_scores''::regclass AND conname = ''ck_task_scores_outcome'' AND pg_get_constraintdef(oid) LIKE ''%''''done_checked''''%'')', 'true', NULL),
        ('nous_admin owns scoring.scorer_heartbeat', 'nous_admin',
            'SELECT pg_get_userbyid(c.relowner) = ''nous_admin'' FROM pg_class c WHERE c.oid = ''scoring.scorer_heartbeat''::regclass', 'true', NULL),
        ('scorer has no CREATE or TEMP on the database or any schema', 'nous_admin',
            'SELECT NOT has_database_privilege(''nous_scorer'', current_database(), ''CREATE'') AND NOT has_database_privilege(''nous_scorer'', current_database(), ''TEMP'') AND NOT has_schema_privilege(''nous_scorer'', ''scoring'', ''CREATE'') AND NOT has_schema_privilege(''nous_scorer'', ''public'', ''CREATE'') AND NOT has_schema_privilege(''nous_scorer'', ''brain'', ''CREATE'') AND NOT has_schema_privilege(''nous_scorer'', ''heart'', ''CREATE'') AND NOT has_schema_privilege(''nous_scorer'', ''nous_system'', ''CREATE'')', 'true', NULL),
        ('PUBLIC holds no CREATE on brain, heart, nous_system, scoring or public', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a WHERE n.nspname IN (''brain'', ''heart'', ''nous_system'', ''scoring'', ''public'') AND a.grantee = 0 AND a.privilege_type = ''CREATE'')', 'true', NULL),
        ('no function in brain, heart, nous_system or scoring is executable by the scorer, and none in scoring by the application role', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE (n.nspname IN (''brain'', ''heart'', ''nous_system'', ''scoring'') AND has_function_privilege(''nous_scorer'', p.oid, ''EXECUTE'')) OR (n.nspname = ''scoring'' AND has_function_privilege(''nous_app'', p.oid, ''EXECUTE'')))', 'true', NULL),
        ('functions the application role creates are not executable by PUBLIC by default', 'nous_admin',
            'SELECT EXISTS (SELECT 1 FROM pg_default_acl d WHERE d.defaclrole = ''nous_app''::regrole AND d.defaclnamespace = 0 AND d.defaclobjtype = ''f'' AND NOT EXISTS (SELECT 1 FROM aclexplode(d.defaclacl) a WHERE a.grantee = 0))', 'true', NULL),
        ('no column-level privilege reaches past the table-level grants', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class r ON r.oid = a.attrelid JOIN pg_namespace n ON n.oid = r.relnamespace CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE a.attnum > 0 AND NOT a.attisdropped AND ((n.nspname = ''scoring'' AND x.grantee IN (0, ''nous_app''::regrole, ''nous_scorer''::regrole)) OR (n.nspname IN (''brain'', ''heart'', ''nous_system'') AND x.grantee IN (0, ''nous_scorer''::regrole))))', 'true', NULL),
        ('both service roles store a SCRAM verifier', 'nous_admin',
            'SELECT (SELECT count(*) FROM pg_authid WHERE rolname IN (''nous_app'', ''nous_scorer'') AND rolpassword LIKE ''SCRAM-SHA-256$%'') = 2', 'true', NULL),
        ('the service roles have no connection limit, expiry or setting but the scorer pins', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN (''nous_app'', ''nous_scorer'') AND (rolconnlimit <> -1 OR (rolvaliduntil IS NOT NULL AND rolvaliduntil <> ''infinity''))) AND NOT EXISTS (SELECT 1 FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = ''nous_app'' OR (r.rolname = ''nous_scorer'' AND NOT (s.setdatabase = 0 AND s.setconfig @> ARRAY[''search_path=pg_catalog, scoring'', ''row_security=off''] AND cardinality(s.setconfig) = 2)))', 'true', NULL),
        ('the application''s tables and sequences grant nothing to PUBLIC, now or by default', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x WHERE c.relnamespace::regnamespace::text IN (''brain'', ''heart'', ''nous_system'') AND c.relkind IN (''r'', ''p'', ''v'', ''m'', ''f'', ''S'') AND x.grantee = 0) AND NOT EXISTS (SELECT 1 FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace CROSS JOIN LATERAL aclexplode(d.defaclacl) a WHERE d.defaclrole = ''nous_app''::regrole AND d.defaclobjtype IN (''r'', ''S'') AND (d.defaclnamespace = 0 OR n.nspname IN (''brain'', ''heart'', ''nous_system'')) AND (a.grantee = 0 OR a.grantee = ''nous_scorer''::regrole))', 'true', NULL),
        ('scorer search_path and row_security are pinned', 'nous_admin',
            'SELECT EXISTS (SELECT 1 FROM pg_db_role_setting s WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = ''nous_scorer'') AND s.setdatabase = 0 AND ''search_path=pg_catalog, scoring'' = ANY (s.setconfig) AND ''row_security=off'' = ANY (s.setconfig))', 'true', NULL),
        ('pg_hba: every line of pg_hba.conf parses (with a broken line the server keeps rules this check cannot see)', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL)', 'true', NULL),
        ('pg_hba: no trust, md5 or password line reaches beyond the local socket and loopback', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_hba_file_rules r WHERE r.error IS NULL AND r.type <> ''local'' AND r.auth_method IN (''trust'', ''md5'', ''password'') AND NOT ((r.address = ''127.0.0.1'' AND r.netmask = ''255.255.255.255'') OR (r.address = ''::1'' AND r.netmask = ''ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'')))', 'true', NULL),
        ('pg_hba: every network line beyond loopback uses scram-sha-256 or reject', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_hba_file_rules r WHERE r.error IS NULL AND r.type <> ''local'' AND NOT ((r.address = ''127.0.0.1'' AND r.netmask = ''255.255.255.255'') OR (r.address = ''::1'' AND r.netmask = ''ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'')) AND r.auth_method NOT IN (''scram-sha-256'', ''reject''))', 'true', NULL),
        ('pg_hba: a network line beyond loopback accepts a password login (scram-sha-256)', 'nous_admin',
            'SELECT EXISTS (SELECT 1 FROM pg_hba_file_rules r WHERE r.error IS NULL AND r.type <> ''local'' AND NOT ((r.address = ''127.0.0.1'' AND r.netmask = ''255.255.255.255'') OR (r.address = ''::1'' AND r.netmask = ''ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'')) AND r.auth_method = ''scram-sha-256'')', 'true', NULL),
        ('the database holds only the schemas this deployment creates', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN (''brain'', ''heart'', ''nous_system'', ''scoring'', ''public'', ''information_schema'') AND nspname !~ ''^pg_'')', 'true', NULL),
        ('the application role owns no schema but its own three', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = ''nous_app''::regrole AND nspname NOT IN (''brain'', ''heart'', ''nous_system''))', 'true', NULL),
        ('the application role owns no relation, function or type outside its three schemas', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relowner = ''nous_app''::regrole AND c.relkind <> ''t'' AND c.relnamespace::regnamespace::text NOT IN (''brain'', ''heart'', ''nous_system'', ''pg_toast'')) AND NOT EXISTS (SELECT 1 FROM pg_proc p WHERE p.proowner = ''nous_app''::regrole AND p.pronamespace::regnamespace::text NOT IN (''brain'', ''heart'', ''nous_system'')) AND NOT EXISTS (SELECT 1 FROM pg_type t WHERE t.typowner = ''nous_app''::regrole AND t.typtype IN (''e'', ''d'', ''r'', ''m'') AND t.typnamespace::regnamespace::text NOT IN (''brain'', ''heart'', ''nous_system''))', 'true', NULL),
        ('the administrator and scorer search_path do not begin with the login name', 'nous_admin',
            'SELECT (SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole WHERE s.setdatabase = 0 AND r.rolname IN (''nous_admin'', ''nous_scorer'') AND EXISTS (SELECT 1 FROM unnest(s.setconfig) c WHERE c LIKE ''search_path=%'' AND c NOT LIKE ''%$user%'')) = 2', 'true', NULL),
        ('scoring default privileges give the application role SELECT only, and the application role sets none', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a WHERE d.defaclnamespace = ''scoring''::regnamespace AND (d.defaclrole = ''nous_app''::regrole OR (a.grantee = ''nous_app''::regrole AND a.privilege_type <> ''SELECT'') OR a.grantee = 0))', 'true', NULL),
        ('the scorer inputs are plain tables without row policies', 'nous_admin',
            'SELECT NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace::regnamespace::text IN (''brain'', ''heart'', ''nous_system'') AND has_table_privilege(''nous_scorer'', c.oid, ''SELECT'') AND (c.relkind <> ''r'' OR c.relrowsecurity OR c.relforcerowsecurity)) AND NOT EXISTS (SELECT 1 FROM pg_policy p WHERE has_table_privilege(''nous_scorer'', p.polrelid, ''SELECT''))', 'true', NULL)
        ) AS t(label, run_as, stmt, expect, after)
    LOOP
        EXECUTE format('SET LOCAL ROLE %I', c.run_as);
        err := NULL;
        flag := NULL;
        BEGIN
            IF c.expect = 'true' THEN
                EXECUTE c.stmt INTO flag;
                ok := coalesce(flag, false);
            ELSE
                EXECUTE c.stmt;
                ok := c.expect = 'ok';
            END IF;
        EXCEPTION WHEN OTHERS THEN
            -- a refusal must be a privilege error (42501): a missing column or table, a timeout or a statement that
            -- cannot run here would prove nothing
            err := SQLSTATE || ' ' || SQLERRM;
            ok := c.expect = 'error' AND SQLSTATE = '42501';
        END;
        RESET ROLE;
        IF ok AND c.after IS NOT NULL THEN
            EXECUTE 'SELECT ' || c.after INTO flag;
            ok := coalesce(flag, false);
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION 'FAIL: % (expected %, got %)', c.label, c.expect, coalesce(err, 'no error');
        END IF;
        passed := passed + 1;
    END LOOP;
    RAISE NOTICE 'verify_db_roles: % checks passed', passed;
END
$verify$;

ROLLBACK;
