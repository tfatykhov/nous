"""A node reset archives the attempt it discards (migration 089)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from nous.dag.attempts import ATTEMPT_HISTORY_CAP, archived_attempts

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def node(**kw):
    fields = dict(
        subtask_id=uuid.uuid4(),
        status="failed",
        error="Completion check failed: exit code 1",
        check_attempts=3,
        last_check_at=datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        completed_at=datetime(2026, 10, 19, 11, 1, tzinfo=UTC),
        attempt_history=None,
    )
    return SimpleNamespace(**{**fields, **kw})


def test_the_discarded_attempt_is_appended_with_how_it_ended():
    n = node()
    assert archived_attempts(n, "retry", NOW) == [
        {
            "subtask_id": str(n.subtask_id),
            "status": "failed",
            "error": "Completion check failed: exit code 1",
            "check_attempts": 3,
            "last_check_at": "2026-10-19T11:00:00+00:00",
            "completed_at": "2026-10-19T11:01:00+00:00",
            "archived_at": "2026-10-19T12:00:00+00:00",
            "reason": "retry",
        }
    ]


def test_a_node_that_never_launched_keeps_its_history():
    earlier = [{"subtask_id": "x"}]
    assert archived_attempts(node(subtask_id=None, attempt_history=earlier), "unblock", NOW) is earlier
    assert archived_attempts(node(subtask_id=None), "unblock", NOW) is None


def test_an_attempt_is_archived_once():
    # A fix stage's retry keeps subtask_id until the relaunch, so a second reset before it would otherwise
    # archive a launch failure under the earlier attempt's subtask.
    n = node()
    first = archived_attempts(n, "fix_retry", NOW)
    again = node(subtask_id=n.subtask_id, status="failed", error="launch failed", attempt_history=first)
    assert archived_attempts(again, "fix_retry", NOW) is first


def test_entries_are_kept_while_the_scorer_can_still_read_them():
    from datetime import timedelta

    from nous.dag.attempts import ATTEMPT_HISTORY_KEEP_DAYS

    recent = [{"subtask_id": f"r{i}", "archived_at": (NOW - timedelta(days=1)).isoformat()} for i in range(30)]
    old = {"subtask_id": "old", "archived_at": (NOW - timedelta(days=ATTEMPT_HISTORY_KEEP_DAYS + 1)).isoformat()}
    undated = {"subtask_id": "undated"}
    history = [old, undated, *recent]
    n = node(attempt_history=history)
    out = archived_attempts(n, "retry", NOW)
    # More than 20 retries inside the window keep every recent entry (the scorer may still read them); only the entry
    # provably older than its reach goes, and one without a readable time is kept.
    assert [e["subtask_id"] for e in out] == ["undated", *(f"r{i}" for i in range(30)), str(n.subtask_id)]
    assert history[0] is old and len(history) == 32  # not mutated


def test_the_hard_cap_keeps_the_newest():
    recent = [{"subtask_id": f"s{i}", "archived_at": NOW.isoformat()} for i in range(ATTEMPT_HISTORY_CAP)]
    n = node(attempt_history=recent)
    out = archived_attempts(n, "retry", NOW)
    assert len(out) == ATTEMPT_HISTORY_CAP
    assert out[0]["subtask_id"] == "s1" and out[-1]["subtask_id"] == str(n.subtask_id)
