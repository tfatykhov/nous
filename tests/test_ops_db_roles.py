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


def test_public_cannot_execute_application_functions_now_or_later():
    # PUBLIC's default EXECUTE would let the scorer, which has USAGE on these schemas, run them
    assert "REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA brain, heart, nous_system FROM PUBLIC;" in ROLES
    # ALL FUNCTIONS leaves procedures out; ALL ROUTINES covers both
    assert "ON ALL FUNCTIONS IN SCHEMA" not in ROLES
    # global: a per-schema REVOKE of the built-in default changes nothing
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE nous_app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;" in ROLES
    assert "('no function in brain, heart or nous_system is executable by the scorer', 'nous_admin'," in VERIFY
    assert "('functions the application role creates are not executable by PUBLIC by default', 'nous_admin'," in VERIFY
