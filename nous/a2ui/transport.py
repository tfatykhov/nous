"""F092: SSE stream generator for /a2ui/stream.

Frame format: ``id: <seq>\\nevent: a2ui\\ndata: <envelope>`` — the outbox seq
is the SSE event id, so browsers resume natively via ``Last-Event-ID``.

Ordering contract (review findings, do not simplify away):

- Subscribe to the in-process hub FIRST, then run the replay query, then
  drain the hub — anything pushed in between waits in the queue.
- Seqs can arrive OUT OF ORDER (codex P1): with two overlapping
  transactions PostgreSQL can hand the lower BIGSERIAL to the one that
  commits second, so the hub legitimately delivers 12 then 11. Delivery is
  therefore deduped by MEMBERSHIP (a bounded seen-set), never by a
  monotonic watermark — a watermark would drop 11 forever. The client
  mirrors this (store dedupes by set, resumes from max).
- The cross-process catch-up poll advances its own watermark ONLY from
  poll results, which are lag-windowed by ``service.replay`` — a
  hub-delivered high seq must not advance it past a straggler still
  invisible inside the lag window.

The generator races the hub-get against a short timeout used for both the
poll and the keepalive comment line — the pending task is never cancelled
on timeout (chat_stream pattern, rest.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0
_SEEN_MAX = 4096


def _frame(seq: int | None, event: str, data: dict) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class _Delivered:
    """Bounded membership set of delivered seqs (out-of-order safe)."""

    def __init__(self, floor: int) -> None:
        # Everything at or below `floor` counts as delivered (the client
        # resumed from there).
        self.floor = floor
        self._seen: set[int] = set()

    def mark(self, seq: int) -> bool:
        """True if this seq is new (and marks it delivered)."""
        if seq <= self.floor or seq in self._seen:
            return False
        self._seen.add(seq)
        if len(self._seen) > _SEEN_MAX:
            # Compact: raise the floor to the lowest contiguous run.
            low = min(self._seen)
            self.floor = max(self.floor, low)
            self._seen = {s for s in self._seen if s > self.floor}
        return True


async def stream_events(
    service: Any,
    *,
    since: int,
    ping_interval: float,
) -> AsyncIterator[str]:
    """Yield SSE frames: replay from ``since``, then live-tail + poll."""
    queue = service.subscribe()
    delivered = _Delivered(since)
    # Poll watermark: advanced ONLY by lag-windowed replay results, so a
    # straggler seq below a hub-delivered high seq is still polled once it
    # ages past the lag window.
    poll_since = since
    last_byte = time.monotonic()
    pending: asyncio.Task | None = None
    try:
        replayed = await service.replay(since)
        if replayed is None:
            yield _frame(None, "control", {"type": "resync"})
            return
        for seq, envelope in replayed:
            if delivered.mark(seq):
                yield _frame(seq, "a2ui", envelope)
                last_byte = time.monotonic()
            poll_since = max(poll_since, seq)

        while True:
            if pending is None:
                pending = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({pending}, timeout=_POLL_INTERVAL_SECONDS)
            if done:
                item = pending.result()
                pending = None
                if item is None:
                    # Dropped by the hub (queue overflow) — force resync.
                    yield _frame(None, "control", {"type": "resync"})
                    return
                seq, envelope = item
                if delivered.mark(seq):
                    yield _frame(seq, "a2ui", envelope)
                    last_byte = time.monotonic()
                continue

            # Timeout: cross-process catch-up, then keepalive if quiet.
            missed = await service.replay(poll_since)
            if missed is None:
                yield _frame(None, "control", {"type": "resync"})
                return
            for seq, envelope in missed:
                if delivered.mark(seq):
                    yield _frame(seq, "a2ui", envelope)
                    last_byte = time.monotonic()
                poll_since = max(poll_since, seq)
            if time.monotonic() - last_byte >= ping_interval:
                yield ": keepalive\n\n"
                last_byte = time.monotonic()
    finally:
        service.unsubscribe(queue)
        if pending is not None:
            pending.cancel()
            try:
                await pending
            except BaseException:
                pass
