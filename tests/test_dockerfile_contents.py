"""F110 PR 6: the image ships the feature and reference docs, and the package ships the builtin skill files.

Text checks on the Dockerfile, .dockerignore and pyproject.toml: no build is needed.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_COPIES = ("COPY docs/features/ docs/features/", "COPY docs/reference/ docs/reference/")


def _lines(name: str) -> list[str]:
    return [line.strip() for line in (ROOT / name).read_text(encoding="utf-8").splitlines()]


def test_dockerfile_copies_feature_and_reference_docs():
    lines = _lines("Dockerfile")
    final_stage = max(i for i, line in enumerate(lines) if line.startswith("FROM "))
    sql = lines.index("COPY sql/ sql/")
    assert sql > final_stage
    for copy in DOCS_COPIES:
        assert copy in lines, copy
        assert lines.index(copy) > sql, f"{copy} comes before COPY sql/"


def test_dockerignore_does_not_exclude_docs_features_reference():
    patterns = [
        line.lstrip("/").rstrip("/")
        for line in _lines(".dockerignore")
        if line and not line.startswith("#") and not line.startswith("!")
    ]
    # A pattern that matches a parent directory excludes everything under it.
    paths = ("docs", "docs/features", "docs/features/INDEX.md", "docs/reference", "docs/reference/rest-api.md")
    excluded = [(p, path) for p in patterns for path in paths if fnmatch.fnmatch(path, p)]
    assert excluded == []


def test_package_data_declares_builtin_skill():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    assert "builtin/*/SKILL.md" in package_data.get("nous.skills", [])
