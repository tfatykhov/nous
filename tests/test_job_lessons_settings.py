"""Job lessons: NOUS_JOB_LESSONS_ENABLED and NOUS_JOB_LESSONS_PER_SLEEP, the compose lines and the doc rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_are_off_and_two_per_sleep():
    s = Settings(_env_file=None)
    assert (s.job_lessons_enabled, s.job_lessons_per_sleep) == (False, 2)


def test_the_environment_names_are_the_contract_names(monkeypatch):
    monkeypatch.setenv("NOUS_JOB_LESSONS_ENABLED", "true")
    monkeypatch.setenv("NOUS_JOB_LESSONS_PER_SLEEP", "5")
    s = Settings(_env_file=None)
    assert (s.job_lessons_enabled, s.job_lessons_per_sleep) == (True, 5)


def test_a_negative_cap_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, job_lessons_per_sleep=-1)


def test_compose_passes_both_flags_with_the_settings_defaults(monkeypatch):
    for k in ("NOUS_JOB_LESSONS_ENABLED", "NOUS_JOB_LESSONS_PER_SLEEP"):
        monkeypatch.delenv(k, raising=False)
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- NOUS_JOB_LESSONS_ENABLED=${NOUS_JOB_LESSONS_ENABLED:-false}" in compose
    assert "- NOUS_JOB_LESSONS_PER_SLEEP=${NOUS_JOB_LESSONS_PER_SLEEP:-2}" in compose


def test_the_docs_have_the_rows():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    assert "| `NOUS_JOB_LESSONS_ENABLED` | `false` |" in doc
    assert "| `NOUS_JOB_LESSONS_PER_SLEEP` | `2` |" in doc
    structure = (ROOT / "docs" / "reference" / "project-structure.md").read_text(encoding="utf-8")
    assert "nous/handlers/job_lessons.py" in structure
    assert "Job lessons" in (ROOT / "docs" / "features" / "F012-kline-learning.md").read_text(encoding="utf-8")
