"""The attempt a node reset discards, archived on the node (migration 089).

retry_node, a fix stage's retry and the unblock of a cancelled node all overwrite the node's
subtask_id, status, error and check columns. Those columns are the only record of how the attempt
ended, so each reset appends an entry here first, in the write that resets the node. The task scorer
reads the entry of a superseded attempt (``observability/task_score_gatherer.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# An entry is kept while the scorer may still read it: the scoring job re-scores a task until it settles, at most 21
# days after it finished, and never reaches back more than 30 (MAX_REACHBACK_DAYS in observability/task_score_job.py).
# An attempt is archived after it finished, so 35 days from the archive covers both. The cap only bounds a node retried
# hundreds of times inside that span; nothing in the harness retries that often.
ATTEMPT_HISTORY_KEEP_DAYS = 35
ATTEMPT_HISTORY_CAP = 200


def _archived_at(entry: Any) -> datetime | None:
    value = entry.get("archived_at") if isinstance(entry, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def archived_attempts(node: Any, reason: str, now: datetime) -> list[dict[str, Any]] | None:
    """``node.attempt_history`` with the attempt a reset is about to discard appended.

    Unchanged when the node never launched a subtask, or when that subtask is archived already: a fix
    stage's retry leaves ``subtask_id`` in place until the relaunch, so a second reset before the
    relaunch would otherwise archive a launch failure under the earlier attempt's subtask.
    """
    history = list(node.attempt_history or [])
    if node.subtask_id is None:
        return node.attempt_history
    subtask_id = str(node.subtask_id)
    if any(entry.get("subtask_id") == subtask_id for entry in history):
        return node.attempt_history
    entry = {
        "subtask_id": subtask_id,
        "status": node.status,
        "error": node.error,
        "check_attempts": node.check_attempts or 0,
        "last_check_at": node.last_check_at.isoformat() if node.last_check_at else None,
        "completed_at": node.completed_at.isoformat() if node.completed_at else None,
        "archived_at": now.isoformat(),
        "reason": reason,
    }
    horizon = now - timedelta(days=ATTEMPT_HISTORY_KEEP_DAYS)
    # Drop only what is provably older than the scorer can still read; an entry without a readable time is kept.
    kept = [e for e in history if (at := _archived_at(e)) is None or at.tzinfo is None or at >= horizon]
    return [*kept, entry][-ATTEMPT_HISTORY_CAP:]
