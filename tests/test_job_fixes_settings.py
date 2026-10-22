"""Job fixes: NOUS_JOB_FIXES_ENABLED, the compose line and the doc row."""

from __future__ import annotations

from pathlib import Path

from nous.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_default_is_off():
    assert Settings(_env_file=None).job_fixes_enabled is False


def test_the_environment_name_is_the_contract_name(monkeypatch):
    monkeypatch.setenv("NOUS_JOB_FIXES_ENABLED", "true")
    assert Settings(_env_file=None).job_fixes_enabled is True


def test_compose_passes_the_flag_with_the_settings_default():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- NOUS_JOB_FIXES_ENABLED=${NOUS_JOB_FIXES_ENABLED:-false}" in compose


def test_the_docs_have_the_row():
    doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text(encoding="utf-8")
    row = next(line for line in doc.splitlines() if line.startswith("| `NOUS_JOB_FIXES_ENABLED` | `false` |"))
    needles = ("NOUS_RESULT_INBOX_ENABLED", "NOUS_INTENTIONS_ENABLED", "NOUS_CONTINUATION_ENABLED", "Telegram chat")
    for needle in needles:
        assert needle in row
