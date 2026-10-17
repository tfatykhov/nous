"""scripts/ops/db_roles.sql and verify_db_roles.sql: static checks that need no database.

The behaviour of the scripts is checked by the "DB roles" CI job, which runs them against Postgres.
"""

from __future__ import annotations

import re
from pathlib import Path

from nous.observability.task_score_job import HEARTBEAT_TABLE, SCORE_TABLE, SOURCE_TABLES

OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
ROLES = (OPS / "db_roles.sql").read_text(encoding="utf-8")
VERIFY = (OPS / "verify_db_roles.sql").read_text(encoding="utf-8")


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
    # nous_admin has no password and the run commits before verify runs, so the refusals come before both
    begin = ROLES.index("\nBEGIN;\n")
    for refusal in (
        "RAISE EXCEPTION 'db_roles.sql: pg_hba.conf has a line that does not parse",
        "RAISE EXCEPTION 'db_roles.sql: pg_hba.conf does not let nous_admin in through the local socket",
    ):
        assert ROLES.index(refusal) < begin < ROLES.index("ALTER ROLE nous_admin"), refusal
    # pg_hba_file_rules reads the file on disk: the server is made to enforce it before it is checked
    assert ROLES.index("pg_reload_conf()") < ROLES.index("FROM pg_hba_file_rules")
    # loopback only with a host-length netmask: "127.0.0.1 0.0.0.0" has address 127.0.0.1 and matches every client
    loopback = (
        "((r.address = '127.0.0.1' AND r.netmask = '255.255.255.255')"
        " OR (r.address = '::1' AND r.netmask = 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff')"
        " OR (r.address = 'localhost' AND r.netmask IS NULL))"
    )
    verify_sql = VERIFY.replace("''", "'")
    assert "r.address IN (" not in ROLES and "r.address IN (" not in verify_sql
    assert verify_sql.count(f"NOT {loopback}") == 2  # the trust/md5/password check and the network check
    # the same conditions as verify's pg_hba checks (whose SQL sits in a string, with its quotes doubled)
    for condition in (
        "FROM pg_hba_file_rules WHERE error IS NOT NULL",
        f"r.error IS NULL AND r.type <> 'local' AND NOT {loopback}"
        " AND r.auth_method NOT IN ('scram-sha-256', 'reject')",
        "coalesce((SELECT r.auth_method = 'trust' FROM pg_hba_file_rules r WHERE r.error IS NULL AND r.type = 'local'"
        " AND (r.user_name && ARRAY['all', 'nous_admin'] OR EXISTS (SELECT 1 FROM unnest(r.user_name) u"
        " WHERE u LIKE '+%' OR u LIKE '/%')) AND (r.database && ARRAY['all', current_database()::text]"
        " OR EXISTS (SELECT 1 FROM unnest(r.database) d WHERE d IN ('sameuser', 'samerole') OR d LIKE '/%'))"
        " ORDER BY r.line_number LIMIT 1), false)",
    ):
        assert condition in ROLES[:begin], condition
        assert condition in verify_sql, condition
    local_label = "pg_hba: nous_admin can log in through the local socket (the first matching local line is trust)"
    assert f"('{local_label}', 'nous_admin'," in VERIFY


def test_public_cannot_execute_application_functions_now_or_later():
    # PUBLIC's default EXECUTE would let the scorer, which has USAGE on these schemas, run them
    assert "REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA brain, heart, nous_system FROM PUBLIC;" in ROLES
    # ALL FUNCTIONS leaves procedures out; ALL ROUTINES covers both
    assert "ON ALL FUNCTIONS IN SCHEMA" not in ROLES
    # global: a per-schema REVOKE of the built-in default changes nothing
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE nous_app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;" in ROLES
    assert "('no function in brain, heart or nous_system is executable by the scorer', 'nous_admin'," in VERIFY
    assert "('functions the application role creates are not executable by PUBLIC by default', 'nous_admin'," in VERIFY
