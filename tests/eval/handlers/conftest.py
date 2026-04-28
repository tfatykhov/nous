"""F056 Phase 2 handler test fixtures (mini-fixture literals).

Per F056 spec §"Test coverage": each handler's 1 integration test runs
end-to-end against the eval test DB on a 5-row mini-fixture defined as
a literal here (no external JSONL file — keeps tests hermetic).
"""
from __future__ import annotations

import pytest


# F056 PR #1: 5-row mini-fixture for admission integration tests.
# 3 admit + 2 reject — enough to validate end-to-end flow without the
# full 50-row corpus. Stable row_ids for deterministic comparison.
_MINI_ADMISSION_ROWS = [
    {
        "row_id": "mini_a01",
        "content": "Mini test fact one is concrete and grounded with source text.",
        "subject": "test",
        "category": "technical",
        "source_text": "Mini test fact one is concrete and grounded with source text.",
        "label": "admit",
        "reviewed_by": "tim",
    },
    {
        "row_id": "mini_a02",
        "content": "User prefers JSON over YAML for configuration files.",
        "subject": "user",
        "category": "preference",
        "source_text": "User said: 'use JSON, not YAML, for the new config.'",
        "label": "admit",
        "reviewed_by": "tim",
    },
    {
        "row_id": "mini_a03",
        "content": "Project deadline is March 15 2026.",
        "subject": "project",
        "category": "technical",
        "source_text": "Tim: 'remember the deadline is March 15 2026'",
        "label": "admit",
        "reviewed_by": "tim",
    },
    {
        "row_id": "mini_r01",
        "content": "OK.",
        "subject": None,
        "category": None,
        "source_text": None,
        "label": "reject",
        "reviewed_by": "tim",
    },
    {
        "row_id": "mini_r02",
        "content": "Stuff happened.",
        "subject": None,
        "category": None,
        "source_text": None,
        "label": "reject",
        "reviewed_by": "tim",
    },
]


@pytest.fixture
def mini_admission_rows() -> list[dict]:
    """5-row admission mini-fixture as Python dicts (for unit tests)."""
    return _MINI_ADMISSION_ROWS.copy()
