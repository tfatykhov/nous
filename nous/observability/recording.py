"""F108: the one bounded fire-and-forget writer every work-view recorder shares.

Recording must never fail or slow a turn. A write is handed over as a factory and run in the background: at most
``concurrency`` writes hold a database connection at once, each is cut off after ``timeout_s``, and past
``max_pending`` queued or running writes a new one is dropped. Every outcome is counted and every failure logged at
WARNING. The semantics follow ``ContextLogger._schedule_bg`` / ``_bounded`` (context_logger.py), plus the pending cap
and the counters.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)


@dataclass
class RecorderStats:
    """What became of the writes handed to a recorder since the process started."""

    written: int = 0
    dropped_timeout: int = 0
    dropped_error: int = 0
    dropped_backpressure: int = 0
    unlabelled: int = 0

    def as_dict(self) -> dict[str, int]:
        """The counters as a plain dict (the observability endpoint's shape)."""
        return asdict(self)


class BoundedWriter:
    """Runs database writes fire-and-forget, bounded in concurrency, time and backlog."""

    def __init__(self, name: str, concurrency: int = 4, timeout_s: float = 30.0, max_pending: int = 500) -> None:
        self.name = name
        self.stats = RecorderStats()
        self._timeout_s = timeout_s
        self._max_pending = max_pending
        self._slots = asyncio.Semaphore(concurrency)
        # asyncio's loop keeps only a WEAK reference to a task: hold a strong one until it is done.
        self._pending: set[asyncio.Task] = set()
        self._saturated = False  # warn once per stretch of backpressure drops, count every one

    def submit(self, make_write: Callable[[], Awaitable[None]]) -> bool:
        """Schedule one write. Returns False, and counts it, when it is dropped.

        Takes a factory, not a coroutine: the write's coroutine is created only once a slot is held, so a dropped
        or never-started write leaves nothing un-awaited. Never raises.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.stats.dropped_error += 1
            logger.warning("F108: %s write dropped: no running event loop", self.name)
            return False
        if len(self._pending) >= self._max_pending:
            self.stats.dropped_backpressure += 1
            if not self._saturated:
                self._saturated = True
                logger.warning(
                    "F108: %s writer has %d writes pending; dropping new writes until it catches up",
                    self.name,
                    self._max_pending,
                )
            return False
        self._saturated = False
        task = loop.create_task(self._bounded(make_write))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return True

    async def _bounded(self, make_write: Callable[[], Awaitable[None]]) -> None:
        """One write, within the concurrency cap and the timeout. Never raises (cancellation still propagates)."""
        try:
            async with self._slots:
                await asyncio.wait_for(make_write(), timeout=self._timeout_s)
        except TimeoutError:
            self.stats.dropped_timeout += 1
            logger.warning("F108: %s write timed out after %ss", self.name, self._timeout_s)
        except Exception as exc:
            self.stats.dropped_error += 1
            logger.warning("F108: %s write failed: %r", self.name, exc)
        else:
            self.stats.written += 1

    async def drain(self, timeout_s: float) -> None:
        """Wait up to ``timeout_s`` for the pending writes (shutdown, before the pool closes). Never raises.

        Writes still running at the deadline are left to their own timeout and logged, not cancelled.
        """
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            while self._pending:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.wait(set(self._pending), timeout=remaining)
            if self._pending:
                logger.warning(
                    "F108: %s writer drain left %d writes unfinished after %ss",
                    self.name,
                    len(self._pending),
                    timeout_s,
                )
        except Exception as exc:
            logger.warning("F108: %s writer drain failed: %r", self.name, exc)
