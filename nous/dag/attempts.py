"""The attempt a node reset discards, archived on the node (migration 089).

retry_node, a fix stage's retry and the unblock of a cancelled node all overwrite the node's
subtask_id, status, error and check columns. Those columns are the only record of how the attempt
ended, so each reset appends an entry here first, in the same conditional write. The task scorer
reads the entry of a superseded attempt (``observability/task_score_gatherer.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

ATTEMPT_HISTORY_CAP = 20  # entries kept per node, newest last


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
    return [*history, entry][-ATTEMPT_HISTORY_CAP:]
