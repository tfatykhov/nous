"""Migrations after 088 run as the application role, which is not a superuser and owns only its own schemas.

A statement that needs more, or that resolves to the public schema, would stop the application from starting on the
next deploy, so the obvious cases are caught here. This is a cheap hint, not the gate: the gate is the "DB roles" CI
job, which applies every migration as the application role. DDL that a migration builds as a string inside a DO block
or a function body is not read here (string literals and dollar-quoted bodies are blanked before matching) and is
left to that job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "sql" / "migrations"
PROBES = ROOT / "tests" / "fixtures" / "migration_probe"
FIRST_CHECKED = 89  # 087 and 088 create the scoring tables while the application still runs as a superuser

_APP = r"(brain|heart|nous_system)\."  # the schemas the application role owns
_NOT_APP = rf"(?!{_APP})"
# possessive (?+): without it the optional word is skipped and the lookahead then sees "IF NOT EXISTS" itself
_IF_NOT_EXISTS = r"(IF\s+NOT\s+EXISTS\s+)?+"

_RULES: tuple[tuple[str, str], ...] = (
    (r"\bCREATE\s+EXTENSION\b", "extensions are installed by the administrator"),
    (r"\b(GRANT|REVOKE)\b", "grants belong to scripts/ops/db_roles.sql"),
    (r"\bOWNER\s+TO\b", "ownership belongs to scripts/ops/db_roles.sql"),
    (r"\bCREATE\s+(ROLE|USER|DATABASE)\b", "roles and databases belong to the administrator"),
    (r"\bscoring\s*\.", "the scoring schema is written by the scorer and changed by the administrator"),
    (
        r"\bpublic\s*\.",
        "do not name public: the application role cannot create there, and its helpers resolve through search_path",
    ),
    (
        rf"\bCREATE\s+(OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE|AGGREGATE|OPERATOR|TYPE|DOMAIN|VIEW"
        rf"|MATERIALIZED\s+VIEW)\s+{_NOT_APP}",
        "qualify it with brain, heart or nous_system (an unqualified name lands in public)",
    ),
    (
        rf"\bCREATE\s+(UNLOGGED\s+)?+(TABLE|SEQUENCE)\s+{_IF_NOT_EXISTS}{_NOT_APP}",
        "qualify the table or sequence with brain, heart or nous_system (an unqualified name lands in public)",
    ),
    (
        rf"\bCREATE\s+(UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?+{_IF_NOT_EXISTS}[\w\"]+\s+ON\s+(ONLY\s+)?+{_NOT_APP}",
        "qualify the table an index is created on (an index lives in its table's schema)",
    ),
    (
        rf"\bCREATE\s+(OR\s+REPLACE\s+)?(CONSTRAINT\s+)?TRIGGER\s+[\w\"]+[^;]*?\bON\s+{_NOT_APP}",
        "qualify the table a trigger is created on",
    ),
    (
        rf"\bSELECT\b[^;]*?\bINTO\s+(?!TEMP\b|TEMPORARY\b)(UNLOGGED\s+)?+(TABLE\s+)?+{_NOT_APP}",
        "SELECT ... INTO creates a table: qualify it, or use CREATE TABLE ... AS with a qualified name",
    ),
    (
        rf"\bCREATE\s+(OR\s+REPLACE\s+)?RULE\s+[\w\"]+\s+AS\s+ON\s+\w+\s+TO\s+{_NOT_APP}",
        "qualify the table a rule is created on",
    ),
    (
        rf"\bCOMMENT\s+ON\s+(TABLE|COLUMN|INDEX|SEQUENCE|VIEW|MATERIALIZED\s+VIEW|FUNCTION|TYPE|DOMAIN)\s+{_NOT_APP}",
        "qualify the object a comment is set on",
    ),
    (
        rf"\bCOMMENT\s+ON\s+(TRIGGER|CONSTRAINT|RULE|POLICY)\s+[\w\"]+\s+ON\s+(DOMAIN\s+)?+{_NOT_APP}",
        "qualify the table a comment is set on",
    ),
    (
        rf"\bALTER\s+SEQUENCE\s+(IF\s+EXISTS\s+)?+{_NOT_APP}",
        "qualify the sequence",
    ),
    (
        rf"\b(ALTER|DROP)\s+TABLE\s+(IF\s+EXISTS\s+)?+(ONLY\s+)?+{_NOT_APP}",
        "qualify the table (an unqualified name is looked up on the search_path, not in the application schemas)",
    ),
)


# one leftmost-wins pass, so a quote inside a comment or a -- inside a string is read as part of what encloses it
_LITERALS_AND_COMMENTS = re.compile(r"'(?:[^']|'')*'|/\*.*?\*/|--[^\n]*|\$(\w*)\$.*?\$\1\$", re.S)


def _blank_one(match: re.Match[str]) -> str:
    text = match.group(0)
    if text.startswith("'"):
        return "''"
    if text.startswith("$"):
        return "$$ $$"
    return " "


def _blank(sql: str) -> str:
    """The SQL without comments (/* */ and --), with string literals and dollar-quoted bodies blanked."""
    return _LITERALS_AND_COMMENTS.sub(_blank_one, sql)


def privilege_violations(sql: str) -> list[str]:
    """The reasons a migration would fail for the application role, one per offending rule."""
    body = _blank(sql)
    return [reason for pattern, reason in _RULES if re.search(pattern, body, re.I)]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE EXTENSION IF NOT EXISTS hstore;",
        "GRANT SELECT ON heart.facts TO someone;",
        "ALTER TABLE heart.facts OWNER TO nous;",
        "CREATE ROLE x;",
        "ALTER TABLE scoring.task_scores ADD COLUMN x int;",
        "CREATE INDEX i ON public.t (a);",
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS 'select 1';",
        "CREATE OR REPLACE FUNCTION immutable_array_to_string(a text[], s text) RETURNS text LANGUAGE sql AS 'x';",
        "create view v as select 1;",
        "CREATE TABLE foo (id int);",
        "CREATE TABLE IF NOT EXISTS foo (id int);",
        "CREATE UNLOGGED TABLE foo (id int);",
        "CREATE SEQUENCE foo_seq;",
        "CREATE INDEX idx_foo ON foo (a);",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_foo ON ONLY foo (a);",
        "CREATE INDEX CONCURRENTLY idx_foo ON foo USING hnsw (v vector_cosine_ops);",
        "CREATE TRIGGER t BEFORE UPDATE OF a ON foo FOR EACH ROW EXECUTE FUNCTION update_timestamp();",
        "ALTER TABLE foo ADD COLUMN x int;",
        "DROP TABLE IF EXISTS foo;",
        "SELECT a, b INTO foo FROM heart.facts;",
        "SELECT * INTO UNLOGGED TABLE foo FROM heart.facts;",
        "CREATE RULE r AS ON INSERT TO foo DO INSTEAD NOTHING;",
        "COMMENT ON TABLE foo IS 'x';",
        "COMMENT ON COLUMN foo.a IS 'x';",
        "COMMENT ON TRIGGER t ON foo IS 'x';",
        "ALTER SEQUENCE foo_seq RESTART;",
        "/* header */ CREATE TABLE foo (id int);",
        "CREATE TABLE foo (id int); -- trailing comment",
        "INSERT INTO nous_system.cfg (k, v) VALUES ('note', 'a -- b'), ('k2', 'v2'); CREATE TABLE foo (id int);",
        (
            "INSERT INTO nous_system.cfg (k, v) VALUES ('a /* b');\n"
            "CREATE TABLE foo (id int);\n"
            "INSERT INTO nous_system.cfg (k, v) VALUES ('c */ d');"
        ),
        "COMMENT ON TABLE heart.facts IS 'don''t'; CREATE TABLE foo (id int);",
    ],
)
def test_the_rules_catch_what_the_application_role_cannot_do(sql):
    assert privilege_violations(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE IF NOT EXISTS nous_system.t (id int);",
        "CREATE TABLE heart.t (id int);\nCREATE SEQUENCE brain.s;",
        "-- GRANT in a comment is fine\nALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS x text;",
        "CREATE TRIGGER t BEFORE UPDATE ON nous_system.t FOR EACH ROW EXECUTE FUNCTION update_timestamp();",
        "CREATE OR REPLACE FUNCTION nous_system.f() RETURNS int LANGUAGE sql AS 'select 1';",
        "CREATE INDEX IF NOT EXISTS i ON heart.facts USING hnsw (embedding vector_cosine_ops);",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS i ON ONLY brain.t (a);",
        "CREATE TEMP TABLE scratch (id int);",
        "DROP TABLE IF EXISTS heart.old_thing;",
        "SELECT a INTO TEMP scratch FROM heart.facts;",
        "SELECT a, b INTO heart.copy FROM heart.facts;",
        "COMMENT ON TABLE heart.facts IS 'x';",
        "COMMENT ON COLUMN heart.facts.a IS 'x';",
        "COMMENT ON TRIGGER t ON heart.facts IS 'x';",
        "ALTER SEQUENCE IF EXISTS brain.s RESTART;",
        "CREATE RULE r AS ON INSERT TO heart.facts DO INSTEAD NOTHING;",
        "/* CREATE TABLE foo (id int); */ SELECT 1; -- CREATE EXTENSION x",
        "DO $do$ BEGIN EXECUTE 'CREATE TABLE foo (id int)'; END $do$;",
        "CREATE FUNCTION heart.f() RETURNS int LANGUAGE plpgsql AS $$ DECLARE n int; BEGIN SELECT 1 INTO n; END $$;",
        "COMMENT ON TABLE heart.facts IS 'callers may grant access';",
    ],
)
def test_the_rules_leave_ordinary_migrations_alone(sql):
    assert privilege_violations(sql) == []


def test_the_ci_probes_are_judged_as_they_are_run():
    """CI applies these two as the application role: the first must apply, the second must fail."""
    ok = sorted((PROBES / "ok").glob("*.sql"))
    bad = sorted((PROBES / "unqualified").glob("*.sql"))
    assert ok and bad
    for path in ok:
        assert privilege_violations(path.read_text(encoding="utf-8")) == [], path.name
    for path in bad:
        assert privilege_violations(path.read_text(encoding="utf-8")), path.name


def test_every_migration_after_088_stays_inside_what_the_application_role_may_do():
    bad = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if int(path.stem.split("_", 1)[0]) >= FIRST_CHECKED:
            found = privilege_violations(path.read_text(encoding="utf-8"))
            if found:
                bad[path.name] = found
    assert not bad, bad
