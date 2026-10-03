"""Telling a task's own cancellation from one that came out of something it awaited."""

from __future__ import annotations

import asyncio


def cancel_requested() -> bool:
    """Whether the running task itself is being cancelled (stop(), event-loop
    teardown). False when a CancelledError only came out of something the
    task awaited: that is the awaited thing's failure, not a request to stop.
    """
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0
