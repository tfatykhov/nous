"""F091: ring buffer + fire-and-forget persistence for retrieval traces.

Mirrors ``ContextLogger`` deliberately, including its ``_pending_tasks``
strong-reference set: asyncio holds only a weak reference to a task, so a
create-and-discard write can be garbage-collected mid-flight and silently
vanish. That bug was already found and fixed once in ``context_logger.py``;
re-deriving the pattern here rather than reusing it would reintroduce it.
"""

from __future__ import annotations

import logging
import random
from collections import deque
from typing import Any

from nous.observability.retrieval_trace import NULL_TRACE, NullTrace, RetrievalTrace

logger = logging.getLogger(__name__)


class RetrievalLogger:
    """Creates traces, keeps a live ring for the dashboard, persists in background."""

    def __init__(
        self,
        db_writer=None,
        enabled: bool = True,
        candidate_sample_rate: float = 0.1,
        snippet_chars: int = 200,
        max_candidates: int = 300,
        ring_size: int = 100,
        agent_id: str = "",
        query_chars: int = 500,
    ):
        self._db_writer = db_writer
        self._enabled = enabled
        self._sample_rate = max(0.0, min(1.0, candidate_sample_rate))
        self._snippet_chars = snippet_chars
        self._query_chars = query_chars
        self._max_candidates = max_candidates
        self._agent_id = agent_id
        self._entries: deque[dict] = deque(maxlen=ring_size)
        self._by_id: dict[str, dict] = {}
        self._pending_tasks: set = set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(
        self,
        query: str,
        path: str,
        session_id: str | None = None,
        turn_number: int | None = None,
        trace_id: str | None = None,
    ) -> RetrievalTrace | NullTrace:
        """Open a trace, or hand back the shared no-op when disabled."""
        if not self._enabled:
            return NULL_TRACE
        return RetrievalTrace(
            query=query,
            path=path,
            agent_id=self._agent_id,
            session_id=session_id,
            turn_number=turn_number,
            trace_id=trace_id,
            snippet_chars=self._snippet_chars,
            max_candidates=self._max_candidates,
            query_chars=self._query_chars,
            # Sampled per-retrieval: header, legs and expansions are cheap and
            # always captured; the per-candidate array is the expensive part.
            capture_candidates=random.random() < self._sample_rate,
        )

    def commit(self, trace: RetrievalTrace | NullTrace) -> None:
        """Snapshot the trace into the ring and schedule the DB write.

        ``to_dict`` is called HERE, synchronously, so the background writer
        never holds a reference to an object the caller could still mutate.
        """
        if not self._enabled or isinstance(trace, NullTrace):
            return
        try:
            payload = trace.to_dict()
        except Exception:
            logger.debug("F091: trace serialization failed", exc_info=True)
            return

        self._entries.append(payload)
        self._by_id[payload["id"]] = payload
        if len(self._by_id) > len(self._entries) + 10:
            self._prune_index()

        if self._db_writer is not None:
            self._schedule_bg(self._db_writer(payload))

    def _prune_index(self) -> None:
        valid = {e["id"] for e in self._entries}
        for stale in [k for k in self._by_id if k not in valid]:
            del self._by_id[stale]

    def _schedule_bg(self, coro) -> None:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()  # no loop (sync context) — avoid "never awaited" warning
            return
        task = loop.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def drain(self, timeout: float = 5.0) -> None:
        """Await in-flight DB writes. Call before closing the DB pool.

        Writes are fire-and-forget, so at shutdown a slow final one is either
        cancelled by loop teardown or wakes up against a disconnected pool —
        and `_write_retrieval_log` swallows the resulting error, so the loss is
        silent. Bounded so a wedged write cannot hold shutdown open.
        """
        import asyncio

        pending = [t for t in self._pending_tasks if not t.done()]
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=timeout)
        except Exception:
            logger.debug("F091: drain failed", exc_info=True)

    # -- reads for the dashboard -------------------------------------------

    def get_recent(self, limit: int = 50, path: str | None = None) -> list[dict]:
        entries = list(reversed(self._entries))
        if path:
            entries = [e for e in entries if e.get("path") == path]
        return entries[:limit]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return self._by_id.get(entry_id)


# Process-wide active logger, set once at startup by nous/main.py.
#
# A singleton registry rather than constructor injection because the two
# retrieval paths are reached from very different places — a tool closure
# (recall_deep) and a cognitive-layer component (ContextEngine) — and
# threading one object down both would touch a lot of unrelated signatures
# for a sink that is process-global anyway. Tests call ``set_active`` directly.
_ACTIVE: RetrievalLogger | None = None


def set_active(logger_instance: RetrievalLogger | None) -> None:
    global _ACTIVE
    _ACTIVE = logger_instance


def get_active() -> RetrievalLogger | None:
    return _ACTIVE


__all__ = ["RetrievalLogger", "get_active", "set_active"]
