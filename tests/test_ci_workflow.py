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
