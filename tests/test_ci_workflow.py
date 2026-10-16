"""CI workflow sanity: shell-level mistakes in ``run:`` steps."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


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
    text = (WORKFLOWS / "ci.yml").read_text()
    step = text.split("- name: Run the tests as the application role", 1)[1].split("- name:", 1)[0]
    run = re.search(r"^\s+run: (pytest .*)$", step, re.M)
    assert run, step
    assert '-m "not scoring_writer"' in run.group(1), run.group(1)
