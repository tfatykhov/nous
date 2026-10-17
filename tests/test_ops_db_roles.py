"""scripts/ops/db_roles.sql and verify_db_roles.sql: static checks that need no database.

The behaviour of the scripts is checked by the "DB roles" CI job, which runs them against Postgres.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nous.observability.task_score_job import HEARTBEAT_TABLE, SCORE_TABLE, SOURCE_TABLES

OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
ROLES = (OPS / "db_roles.sql").read_text(encoding="utf-8")
VERIFY = (OPS / "verify_db_roles.sql").read_text(encoding="utf-8")
ROLLBACK = (OPS / "db_roles_rollback.sql").read_text(encoding="utf-8")


def _statements(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_the_scorer_is_granted_exactly_the_tables_the_job_reads():
    block = ROLES.split("-- source tables begin", 1)[1].split("-- source tables end", 1)[0]
    granted = re.findall(r"^GRANT SELECT ON (\S+) TO nous_scorer;$", block, re.M)
    assert sorted(granted) == sorted(SOURCE_TABLES)
    assert len(granted) == len(set(granted))
    assert f"GRANT SELECT, INSERT, UPDATE ON {SCORE_TABLE} TO nous_scorer;" in ROLES
    assert f"GRANT SELECT, INSERT, UPDATE ON {HEARTBEAT_TABLE} TO nous_scorer;" in ROLES


def test_verify_expects_the_scorer_to_read_as_many_tables_as_the_job_reads():
    counts = re.findall(r"has_table_privilege\(''nous_scorer'', c\.oid, ''SELECT''\)\) = (\d+)", VERIFY)
    assert counts == [str(len(SOURCE_TABLES))]


def test_nothing_else_is_granted_to_the_scorer():
    grants = re.findall(r"^GRANT .* TO [^;]*nous_scorer[^;]*;$", _statements(ROLES), re.M)
    allowed = {f"GRANT SELECT ON {t} TO nous_scorer;" for t in SOURCE_TABLES} | {
        f"GRANT SELECT, INSERT, UPDATE ON {SCORE_TABLE} TO nous_scorer;",
        f"GRANT SELECT, INSERT, UPDATE ON {HEARTBEAT_TABLE} TO nous_scorer;",
        "GRANT USAGE ON SCHEMA scoring TO nous_app, nous_scorer;",
        "GRANT USAGE ON SCHEMA brain, heart, nous_system TO nous_scorer;",
        'GRANT CONNECT ON DATABASE :"dbname" TO nous_scorer;',
    }
    assert set(grants) <= allowed, set(grants) - allowed


def test_no_password_is_written_in_a_script():
    assert not re.search(r"PASSWORD\s+'", _statements(ROLES), re.I)
    assert "PASSWORD :'app_password'" in ROLES and "PASSWORD :'scorer_password'" in ROLES


def test_the_run_is_one_transaction_and_the_lock_out_comes_after_every_grant():
    body = _statements(ROLES)
    assert body.index("BEGIN;") < body.index("GRANT") and body.rstrip().splitlines()[-1].startswith("\\echo")
    assert "COMMIT;" in body
    lockout = body.index('ALTER ROLE :"bootstrap" NOLOGIN PASSWORD NULL;')
    assert lockout > body.rindex("GRANT ")
    assert lockout > body.index("ALTER DATABASE")


def test_verify_never_commits():
    body = _statements(VERIFY)
    assert body.count("ROLLBACK;") == 1 and "COMMIT" not in body


def test_the_logins_that_can_create_or_read_widely_do_not_resolve_names_through_a_schema_named_after_them():
    assert "ALTER ROLE nous_admin SET search_path = pg_catalog, public;" in ROLES
    assert "ALTER ROLE nous_scorer SET search_path = pg_catalog, scoring;" in ROLES
    assert "pg_hba_file_rules" in VERIFY and "the database holds only the schemas" in VERIFY


def test_verify_fails_when_pg_hba_conf_has_a_line_the_server_cannot_parse():
    # the reload then fails and the server keeps rules that pg_hba_file_rules does not show
    assert "'SELECT NOT EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL)', 'true'" in VERIFY


def test_every_pg_hba_network_line_is_judged_whatever_users_it_names():
    # a user selector such as /^nous_admin$ or +group would slip past a filter on user_name
    label = "('pg_hba: every network line beyond loopback uses scram-sha-256 or reject', 'nous_admin',"
    assert label in VERIFY
    check = VERIFY.split(label, 1)[1].split("\n        ('", 1)[0]
    assert "user_name" not in check, check


def test_the_script_refuses_to_run_while_pg_hba_lets_a_network_login_in_without_scram():
    # nous_admin has no password and the run commits before verify runs: the network refusal comes before nous_admin
    # is set up, then the script logs in as nous_admin, and only then locks the bootstrap role
    order = [
        ROLES.index("RAISE EXCEPTION 'db_roles.sql: pg_hba.conf has a line that does not parse"),
        ROLES.index("CREATE ROLE nous_admin"),
        ROLES.index("ALTER ROLE nous_admin WITH SUPERUSER LOGIN PASSWORD NULL;"),
        ROLES.index("\\connect - nous_admin"),
        ROLES.index("\nBEGIN;\n"),
        ROLES.index('ALTER ROLE :"bootstrap" NOLOGIN PASSWORD NULL;'),
    ]
    assert order == sorted(order), order
    begin = order[4]
    # the view drops the quoting of selectors, so local access is proven by the login, not predicted
    assert "admin_local_ok" not in ROLES
    assert _statements(ROLES).count("\\connect") == 1
    # pg_hba_file_rules reads the file on disk: the server is made to enforce it before it is checked
    assert ROLES.index("pg_reload_conf()") < ROLES.index("FROM pg_hba_file_rules")
    # loopback only with a host-length netmask: "127.0.0.1 0.0.0.0" has address 127.0.0.1 and matches every client;
    # and only the numeric addresses: a host name such as localhost is matched by reverse and forward DNS
    loopback = (
        "((r.address = '127.0.0.1' AND r.netmask = '255.255.255.255')"
        " OR (r.address = '::1' AND r.netmask = 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'))"
    )
    assert "'localhost'" not in ROLES and "''localhost''" not in VERIFY
    verify_sql = VERIFY.replace("''", "'")
    assert "r.address IN (" not in ROLES and "r.address IN (" not in verify_sql
    # the trust/md5/password check, the network check and the password-path check
    assert verify_sql.count(f"NOT {loopback}") == 3
    # the services log in over the network: a run needs at least one password path there
    path_refusal = ROLES.index("RAISE EXCEPTION 'db_roles.sql: pg_hba.conf has no network line that accepts a password")
    assert path_refusal < ROLES.index("CREATE ROLE nous_admin")
    # the same conditions as verify's pg_hba checks (whose SQL sits in a string, with its quotes doubled)
    for condition in (
        "FROM pg_hba_file_rules WHERE error IS NOT NULL",
        f"r.error IS NULL AND r.type <> 'local' AND NOT {loopback}"
        " AND r.auth_method NOT IN ('scram-sha-256', 'reject')",
        f"r.error IS NULL AND r.type <> 'local' AND NOT {loopback} AND r.auth_method = 'scram-sha-256'",
    ):
        assert condition in ROLES[:begin], condition
        assert condition in verify_sql, condition
    # verify runs as nous_admin through docker exec, which is itself the proof of local access
    assert "r.type = 'local'" not in verify_sql
    assert "through the local socket" not in VERIFY.split("$verify$", 1)[1]


def test_public_cannot_execute_application_functions_now_or_later():
    # PUBLIC's default EXECUTE would let the scorer, which has USAGE on these schemas, run them
    assert "REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA brain, heart, nous_system FROM PUBLIC;" in ROLES
    # ALL FUNCTIONS leaves procedures out; ALL ROUTINES covers both
    assert "ON ALL FUNCTIONS IN SCHEMA" not in ROLES
    # global: a per-schema REVOKE of the built-in default changes nothing
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE nous_app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;" in ROLES
    # a function nous_admin creates in scoring later gets PUBLIC EXECUTE until db_roles.sql runs again: verify sees it
    label = (
        "no function in brain, heart, nous_system or scoring is executable by the scorer,"
        " and none in scoring by the application role"
    )
    assert f"('{label}', 'nous_admin'," in VERIFY
    check = VERIFY.split(label, 1)[1].split("\n        ('", 1)[0].replace("''", "'")
    assert "n.nspname IN ('brain', 'heart', 'nous_system', 'scoring')" in check, check
    assert "has_function_privilege('nous_app', p.oid, 'EXECUTE')" in check, check
    assert "('functions the application role creates are not executable by PUBLIC by default', 'nous_admin'," in VERIFY


def test_column_privileges_are_revoked_before_the_grants_and_verified():
    # pg_attribute.attacl is separate from the table's ACL: a column grant to PUBLIC in the application schemas,
    # where the table-level revokes name only the scorer, would survive them
    first_grant = ROLES.index("GRANT USAGE ON SCHEMA scoring")
    for revoke in (
        "REVOKE ALL (%I) ON %s FROM PUBLIC, nous_app, nous_scorer CASCADE",
        "REVOKE ALL (%I) ON %s FROM PUBLIC, nous_scorer CASCADE",
    ):
        assert -1 < ROLES.find(revoke) < first_grant, revoke
    assert "('no column-level privilege reaches past the table-level grants', 'nous_admin'," in VERIFY


def test_a_refused_reload_stops_the_run():
    reload = ROLES.index("IF NOT pg_reload_conf() THEN")
    refusal = ROLES.index("RAISE EXCEPTION 'db_roles.sql: the server did not accept the configuration reload")
    assert reload < refusal < ROLES.index("FROM pg_hba_file_rules")


@pytest.mark.parametrize(
    ("script", "name"), [(ROLES, "db_roles.sql"), (VERIFY, "verify_db_roles")], ids=["db_roles", "verify"]
)
def test_the_run_waits_until_this_session_has_reloaded_the_configuration(script, name):
    # pg_reload_conf() only signals; this session takes the new configuration at the start of a later top-level
    # command, and pg_conf_load_time() then advances. Both scripts reload before they read pg_hba_file_rules, so
    # the rules they audit are the ones the server enforces.
    t0 = script.index("SELECT pg_conf_load_time() AS conf_t0 \\gset")
    reload = script.index("IF NOT pg_reload_conf() THEN")
    block = script[reload : script.index("END $$;", reload)]
    assert "pg_sleep" not in block, block
    assert f"RAISE EXCEPTION '{name}: the server did not accept the configuration reload" in block, block
    check = "SELECT pg_conf_load_time() > :'conf_t0'::timestamptz AS conf_reloaded \\gset"
    first = script.index(check)
    second = script.index(check, first + 1)
    refusal = script.index(f"RAISE EXCEPTION '{name}: the server has not finished reloading its configuration")
    assert t0 < reload < first < second < refusal < script.index("FROM pg_hba_file_rules")


def test_the_rollback_stores_a_scram_verifier():
    assert "SET password_encryption = 'scram-sha-256';\nALTER ROLE :\"bootstrap\" LOGIN PASSWORD" in ROLLBACK


def test_the_policy_check_looks_only_at_relations_the_scorer_reads():
    sql = VERIFY.replace("''", "'")
    assert "NOT EXISTS (SELECT 1 FROM pg_policy)" not in sql
    scoped = "NOT EXISTS (SELECT 1 FROM pg_policy p WHERE has_table_privilege('nous_scorer', p.polrelid, 'SELECT'))"
    assert scoped in sql


def test_the_service_roles_store_scram_verifiers_and_a_run_requires_them():
    # with password_encryption = md5 a new password would be an MD5 verifier, which the scram-only pg_hba rejects
    begin = ROLES.index("\nBEGIN;\n")
    scram = ROLES.index("SET LOCAL password_encryption = 'scram-sha-256';")
    assert begin < scram < ROLES.index("PASSWORD :'app_password'") < ROLES.index("PASSWORD :'scorer_password'")
    for refusal in (
        "RAISE EXCEPTION 'db_roles.sql: nous_app''s stored password is not a SCRAM verifier",
        "RAISE EXCEPTION 'db_roles.sql: nous_scorer''s stored password is not a SCRAM verifier",
    ):
        assert -1 < ROLES.find(refusal) < begin, refusal
    assert "RAISE EXCEPTION 'password is not a SCRAM verifier: %'" in ROLES[ROLES.index("DO $post$") :]
    assert "('both service roles store a SCRAM verifier', 'nous_admin'," in VERIFY


def test_inherited_login_restrictions_and_settings_are_reset_before_the_scorer_pins():
    # a connection limit, an expiry or a setting such as default_transaction_read_only left on a service role
    # would stop it after the lock-out
    for role in ("nous_app", "nous_scorer"):
        assert f"ALTER ROLE {role} CONNECTION LIMIT -1 VALID UNTIL 'infinity';" in ROLES, role
    reset = ROLES.index("ALTER ROLE nous_scorer RESET ALL;")
    assert "ALTER ROLE nous_app RESET ALL;" in ROLES
    assert "IN DATABASE %I RESET ALL" in ROLES  # per-database settings of the two roles
    assert reset < ROLES.index("ALTER ROLE nous_scorer SET search_path") < ROLES.index("ALTER ROLE nous_scorer SET row")
    assert "RAISE EXCEPTION 'login restriction or setting left on: %'" in ROLES[ROLES.index("DO $post$") :]
    label = "the service roles have no connection limit, expiry or setting but the scorer pins"
    assert f"('{label}', 'nous_admin'," in VERIFY


def test_nobody_but_the_administrator_creates_in_public():
    # clusters initialised with the pre-15 defaults let PUBLIC create in schema public
    begin = ROLES.index("\nBEGIN;\n")
    assert begin < ROLES.find("REVOKE CREATE ON SCHEMA public FROM PUBLIC, nous_app, nous_scorer CASCADE;")
    # verify: the application's probe and the scorer's privilege check, both of which count PUBLIC's grants
    assert "('app cannot write to public', 'nous_app', 'CREATE TABLE public.zz (i int)', 'error', NULL)" in VERIFY
    assert "NOT has_schema_privilege(''nous_scorer'', ''public'', ''CREATE'')" in VERIFY


def test_the_application_relations_grant_nothing_to_public_now_or_by_default():
    # the scorer reaches whatever PUBLIC may: a re-run takes back PUBLIC's grants and the defaults that would hand
    # new tables or sequences to PUBLIC or the scorer, before the scorer's own grants
    first_scorer_grant = ROLES.index("GRANT USAGE ON SCHEMA brain, heart, nous_system TO nous_scorer;")
    for revoke in (
        "REVOKE ALL ON ALL TABLES IN SCHEMA brain, heart, nous_system FROM PUBLIC CASCADE;",
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA brain, heart, nous_system FROM PUBLIC CASCADE;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE nous_app %s REVOKE ALL ON %s FROM %s",
    ):
        assert -1 < ROLES.find(revoke) < first_scorer_grant, revoke
    label = "the application''s tables and sequences grant nothing to PUBLIC, now or by default"
    assert f"('{label}', 'nous_admin'," in VERIFY
