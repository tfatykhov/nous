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
    # A model call's HTTP attempt reported after the call's row was written: counted, not recorded.
    dropped_late: int = 0

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
        # The writes pending per kind: each kind is held to its own cap, so one kind never crowds out another.
        self._pending_by_kind: dict[str, int] = {}
        self._saturated: set[str] = set()  # the kinds dropping now: warn once per stretch of drops, count every one

    def _count(self, field: str, stats: RecorderStats | None) -> None:
        """Bump one counter in the writer's totals and, when given, in the caller's own stats."""
        setattr(self.stats, field, getattr(self.stats, field) + 1)
        if stats is not None and stats is not self.stats:
            setattr(stats, field, getattr(stats, field) + 1)

    def submit(
        self,
        make_write: Callable[[], Awaitable[None]],
        stats: RecorderStats | None = None,
        *,
        kind: str = "default",
        max_pending: int | None = None,
    ) -> bool:
        """Schedule one write. Returns False, and counts it, when it is dropped.

        Takes a factory, not a coroutine: the write's coroutine is created only once a slot is held, so a dropped
        or never-started write leaves nothing un-awaited. The write's outcome is counted in the writer's ``stats``
        and also in ``stats`` when given (the submitting recorder's own counters). A write is dropped
        (``dropped_backpressure``) when ``max_pending`` writes of its ``kind`` are already pending (the writer's own
        cap when not given): the model-call text is a kind of its own with a smaller cap, and its writes never count
        against the other writes' cap. All kinds share the concurrency cap. Never raises.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._count("dropped_error", stats)
            logger.warning("F108: %s write dropped: no running event loop", self.name)
            return False
        cap = self._max_pending if max_pending is None else max_pending
        if self._pending_by_kind.get(kind, 0) >= cap:
            self._count("dropped_backpressure", stats)
            if kind not in self._saturated:
                self._saturated.add(kind)
                logger.warning(
                    "F108: %s writer has %d %s writes pending; dropping new ones until it catches up",
                    self.name,
                    cap,
                    kind,
                )
            return False
        self._saturated.discard(kind)
        task = loop.create_task(self._bounded(make_write, stats))
        self._pending.add(task)
        self._pending_by_kind[kind] = self._pending_by_kind.get(kind, 0) + 1
        task.add_done_callback(lambda done: self._finished(done, kind))
        return True

    def _finished(self, task: asyncio.Task, kind: str) -> None:
        """A write is done: it no longer counts as pending."""
        self._pending.discard(task)
        self._pending_by_kind[kind] -= 1

    async def _bounded(self, make_write: Callable[[], Awaitable[None]], stats: RecorderStats | None) -> None:
        """One write, within the concurrency cap and the timeout. Never raises (cancellation still propagates)."""
        try:
            async with self._slots:
                await asyncio.wait_for(make_write(), timeout=self._timeout_s)
        except TimeoutError:
            self._count("dropped_timeout", stats)
            logger.warning("F108: %s write timed out after %ss", self.name, self._timeout_s)
        except Exception as exc:
            self._count("dropped_error", stats)
            logger.warning("F108: %s write failed: %r", self.name, exc)
        else:
            self._count("written", stats)

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
