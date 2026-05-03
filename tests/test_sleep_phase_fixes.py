"""Tests for the 3 silently-broken sleep phases fixed after PR #404
discovered them (stale_scan / cluster_consolidation / procedures).

These tests focus on the *specific failure modes* the prod-data
analysis identified:

  - stale_scan filtered ``active=true AND superseded_by IS NOT NULL``
    which is empty by design (the supersede flow deactivates).
  - cluster_consolidation picked top-5 by size, always landing on
    accumulating subjects (lesson_learned 164, Tim 36) the LLM
    correctly refused to merge into one fact.
  - procedure_learner._check_recency hardcoded a 7-day cutoff —
    only 3 of 200 eligible decisions fell in that window in prod.

All tests mock the DB / LLM layer; the assertions are about the
filter / threshold logic, not Postgres semantics.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from nous.config import Settings
from nous.handlers.procedure_learner import ProcedureLearner


# ---------------------------------------------------------------------------
# Settings — verify the new knobs exist with the documented defaults
# ---------------------------------------------------------------------------


def test_stale_scan_settings_have_safe_defaults():
    s = Settings()
    assert s.stale_scan_age_days == 60
    assert "rule" in s.stale_scan_excluded_categories, (
        "rule category MUST be excluded by default — rules are user "
        "directives that may be infrequently exercised but still in "
        "force; deactivating them on recall stats alone is unsafe."
    )


def test_cluster_consolidation_settings_skip_accumulating_subjects():
    s = Settings()
    assert s.cluster_consolidation_min_facts == 3
    assert s.cluster_consolidation_max_facts == 10, (
        "max_facts MUST cap at a small number — prod has subjects with "
        "164 / 36 facts that are accumulating logs (lesson_learned, "
        "Tim) which the LLM correctly refuses to merge into one."
    )


def test_procedure_recency_window_is_meaningful():
    s = Settings()
    # 30 days is the minimum that gives a non-trivial candidate pool
    # given prod's ~8 successful-reviewed-with-bridge decisions per week.
    assert s.procedure_recency_days >= 14, (
        f"procedure_recency_days={s.procedure_recency_days} too tight; "
        f"prod data shows 7-day window yielded 3 of 200 eligible "
        f"decisions, blocking the recency gate."
    )


# ---------------------------------------------------------------------------
# ProcedureLearner._check_recency — actual behavior with the new knob
# ---------------------------------------------------------------------------


def _make_learner(recency_days: int) -> ProcedureLearner:
    """Build a ProcedureLearner with just enough wiring to exercise
    _check_recency (which only reads self._settings)."""
    settings = Settings(procedure_recency_days=recency_days)
    learner = ProcedureLearner.__new__(ProcedureLearner)
    learner._settings = settings
    return learner


def test_recency_gate_passes_when_member_within_window():
    learner = _make_learner(recency_days=30)
    now = datetime.now(UTC)
    items = [
        SimpleNamespace(created_at=now - timedelta(days=200)),  # ancient
        SimpleNamespace(created_at=now - timedelta(days=15)),   # within 30d
    ]
    assert learner._check_recency(items) is True


def test_recency_gate_rejects_when_all_outside_window():
    learner = _make_learner(recency_days=30)
    now = datetime.now(UTC)
    items = [
        SimpleNamespace(created_at=now - timedelta(days=60)),
        SimpleNamespace(created_at=now - timedelta(days=45)),
    ]
    assert learner._check_recency(items) is False


def test_recency_gate_respects_settings_window():
    """The hardcoded 7-day window was the bug. Verify the gate now
    respects the setting — passing 7 days should still reject items
    that are 15 days old, while 30 days should accept them."""
    now = datetime.now(UTC)
    items = [SimpleNamespace(created_at=now - timedelta(days=15))]
    # 7-day window → reject (the old behavior)
    assert _make_learner(7)._check_recency(items) is False
    # 30-day window → accept (the new default)
    assert _make_learner(30)._check_recency(items) is True


def test_recency_gate_handles_missing_created_at():
    """A cluster member with no timestamp must not crash the gate."""
    learner = _make_learner(recency_days=30)
    items = [
        SimpleNamespace(created_at=None),
        SimpleNamespace(),  # no attribute at all
        SimpleNamespace(created_at=datetime.now(UTC) - timedelta(days=2)),
    ]
    assert learner._check_recency(items) is True


def test_recency_gate_returns_false_on_empty_cluster():
    learner = _make_learner(recency_days=30)
    assert learner._check_recency([]) is False


# ---------------------------------------------------------------------------
# stale_scan filter design — verify the OR-on-recall semantics
# ---------------------------------------------------------------------------


def test_stale_scan_filter_uses_or_on_recall():
    """The new filter must use OR on the recall side
    (``last_recalled_at IS NULL OR last_recalled_at < cutoff``). A
    ``recall_count == 0``-only filter would permanently exempt facts
    recalled once years ago and never since — same silent-failure
    pattern the original code had in reverse. Reviewer flagged this
    on PR #405; verify the source contains the OR semantics rather
    than the exclude-on-recall_count semantics.
    """
    import inspect

    from nous.handlers.sleep_handler import SleepHandler

    src = inspect.getsource(SleepHandler._phase_stale_scan)

    # Filter must check both NULL and <cutoff (the OR branches).
    assert "last_recalled_at.is_(None)" in src, (
        "stale_scan filter must check last_recalled_at IS NULL"
    )
    assert "last_recalled_at < cutoff" in src, (
        "stale_scan filter must also check last_recalled_at < cutoff "
        "so facts recalled long-ago-and-never-again get caught"
    )
    # The bug-flagged version used recall_count == 0 as a hard gate.
    # New version must not gate on recall_count in a SQLAlchemy where().
    # (The string `recall_count` may legitimately appear in docstring
    # text describing the bug, so check the SQL clause shape instead.)
    assert "Fact.recall_count == 0" not in src, (
        "stale_scan filter must NOT gate on recall_count == 0 — "
        "facts recalled once long ago would be permanently exempted"
    )
