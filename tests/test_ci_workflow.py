"""CI workflow sanity: shell-level mistakes in ``run:`` steps."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _step(name: str) -> str:
    """The text of one step of ci.yml, from its name to the next step."""
    text = (WORKFLOWS / "ci.yml").read_text()
    return text.split(f"- name: {name}\n", 1)[1].split("- name:", 1)[0]


def test_pip_version_specifiers_are_quoted() -> None:
    # An unquoted `pip install ruff>=0.8` is `pip install ruff` with stdout
    # redirected to a file named `=0.8`: the constraint is silently dropped.
    bad = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for lineno, line in enumerate(workflow.read_text().splitlines(), 1):
            if "pip install" not in line:
                continue
            unquoted = re.sub(r"\"[^\"]*\"|'[^']*'", "", line)
            if "<" in unquoted or ">" in unquoted:
                bad.append(f"{workflow.name}:{lineno}: {line.strip()}")
    assert not bad, bad


def test_the_application_role_job_deselects_the_modules_that_write_scores() -> None:
    # The DB roles job connects as nous_app, which may not write scoring.*; the modules that do carry the marker.
    step = _step("Run the tests as the application role")
    assert re.search(r"^\s+DB_USER: nous_app$", step, re.M), step
    run = re.search(r"^\s+run: (pytest .*)$", step, re.M)
    assert run, step
    assert '-m "not scoring_writer"' in run.group(1), run.group(1)


def test_the_qualified_probe_step_fails_unless_the_probe_was_applied() -> None:
    # apply_migrations.py exits 0 and prints "applied []" when the directory holds nothing new.
    step = _step("Probe, qualified DDL applies under the application role")
    assert "9998_qualified_ddl" in step, step


def test_the_roles_step_runs_the_runbook_form_through_the_database_containers_socket() -> None:
    # db_roles.sql logs in as nous_admin (no password) before it locks the bootstrap role; over TCP from the runner
    # that login meets scram-sha-256 and fails, so both scripts go through the container's local socket.
    step = _step("Create the database roles and check them")
    container = "${{ job.services.postgres.id }}"
    roles = re.search(rf"docker exec -i [^\n]*{re.escape(container)} psql -U nous -d nous [^\n]*-f - < (\S+)", step)
    check = re.search(rf"docker exec -i {re.escape(container)} psql -U nous_admin -d nous [^\n]*-f - < (\S+)", step)
    assert roles and roles.group(1) == "scripts/ops/db_roles.sql", step
    assert check and check.group(1) == "scripts/ops/verify_db_roles.sql", step
    assert roles.start() < check.start(), step
    assert "-e NOUS_APP_DB_PASSWORD -e NOUS_SCORER_DB_PASSWORD" in roles.group(0), roles.group(0)
    assert "ON_ERROR_STOP=1" in roles.group(0) and "ON_ERROR_STOP=1" in check.group(0), step


def test_the_roles_are_checked_again_after_the_later_migrations() -> None:
    # A migration applied as nous_app can undo what the checks rely on (row security on a scorer input, say).
    text = (WORKFLOWS / "ci.yml").read_text()
    later = text.index("- name: Apply every later migration as the application role\n")
    again = text.index("- name: Check the database roles again after the later migrations\n")
    assert later < again, "the second check must run after the later migrations"
    step = _step("Check the database roles again after the later migrations")
    assert "psql -U nous_admin -d nous" in step and "-f - < scripts/ops/verify_db_roles.sql" in step, step
