"""Grep linter: nous/eval/ must not use the deprecated datetime.utcnow().

Fails CI if any file under ``nous/eval/`` references ``utcnow``. The
replacement is ``datetime.now(tz=timezone.utc)`` (or ``datetime.now(UTC)``
in Python 3.12+).

This is a regression guard, not a content test, so it runs without a DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.eval


REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "nous" / "eval"


def test_no_utcnow_in_eval_module() -> None:
    """No ``utcnow`` usage anywhere under ``nous/eval/``."""
    if not EVAL_DIR.exists():
        pytest.skip("nous/eval/ does not yet exist (Core agent not landed)")
    offenders: list[tuple[Path, int, str]] = []
    for py in EVAL_DIR.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "utcnow" in line and not line.lstrip().startswith("#"):
                offenders.append((py.relative_to(REPO_ROOT), lineno, line.strip()))
    assert not offenders, (
        "nous/eval/ uses deprecated datetime.utcnow(); "
        "replace with datetime.now(tz=timezone.utc) or datetime.now(UTC). "
        f"Offenders:\n{chr(10).join(f'{p}:{ln}: {src}' for p, ln, src in offenders)}"
    )
