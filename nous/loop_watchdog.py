"""Event-loop watchdog: dump every thread's stack and exit if the loop stalls.

2026-09-26: the event loop sat blocked for 2.5 h on a lock held by a thread
that no longer existed. `/health` timed out and Docker marked the container
`unhealthy`, but nothing acts on that, and diagnosing it took py-spy from a
second container.

An asyncio task keeps re-arming `faulthandler.dump_traceback_later`. While
the loop turns, the timer never expires. When it stops turning — blocked on a
lock, stuck in a sync call, anything — faulthandler's own C thread, which
needs neither the loop nor the GIL nor any Python lock, writes every thread's
stack to stderr and exits the process with status 1, and the container's
restart policy brings Nous back with the evidence already in the log.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from nous.config import Settings

logger = logging.getLogger(__name__)

# How often the loop proves it is alive. Far below the minimum timeout (30 s),
# so a turning loop can never let the timer run out.
REARM_SECONDS = 10.0


async def run_event_loop_watchdog(
    timeout: float, *, rearm: float = REARM_SECONDS, file: TextIO | None = None
) -> None:
    """Re-arm the stall timer every `rearm` seconds until cancelled."""
    out = file if file is not None else sys.stderr
    try:
        while True:
            faulthandler.dump_traceback_later(timeout, exit=True, file=out)
            await asyncio.sleep(rearm)
    finally:
        faulthandler.cancel_dump_traceback_later()


def start_event_loop_watchdog(settings: Settings) -> asyncio.Task | None:
    """Start the watchdog if enabled. Call once startup has finished."""
    if not settings.event_loop_watchdog_enabled:
        return None
    timeout = settings.event_loop_watchdog_timeout_seconds
    logger.info(
        "Event-loop watchdog armed: a %ds stall dumps all thread stacks and exits",
        timeout,
    )
    return asyncio.create_task(run_event_loop_watchdog(timeout), name="event-loop-watchdog")


async def stop_event_loop_watchdog(task: asyncio.Task | None) -> None:
    """Disarm before shutdown, so a slow shutdown is never mistaken for a stall."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
