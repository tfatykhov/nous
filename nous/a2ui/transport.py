"""F092: SSE stream generator for /a2ui/stream.

Frame format: ``id: <seq>\\nevent: a2ui\\ndata: <envelope>`` — the outbox seq
is the SSE event id, so browsers resume natively via ``Last-Event-ID``.

Ordering contract (review finding — do not reorder):
1. subscribe to the in-process hub FIRST,
2. then run the replay query,
3. then drain the hub, dropping anything ``<= last_sent``.
Any push landing between (2) and (3) is waiting in the queue; anything
older is deduped by seq. The client ALSO dedupes by seq — replay and live
legitimately overlap.

The generator races the hub-get against a short timeout used for both the
cross-process catch-up poll (a diag script writing the outbox from another
process never reaches the in-process hub) and the keepalive comment line —
the pending task is never cancelled on timeout (chat_stream pattern,
rest.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 2.0


def _frame(seq: int | None, event: str, data: dict) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream_events(
    service: Any,
    *,
    since: int,
    ping_interval: float,
) -> AsyncIterator[str]:
    """Yield SSE frames: replay from ``since``, then live-tail + poll."""
    queue = service.subscribe()
    last_sent = since
    last_byte = time.monotonic()
    pending: asyncio.Task | None = None
    try:
        replayed = await service.replay(since)
        if replayed is None:
            yield _frame(None, "control", {"type": "resync"})
            return
        for seq, envelope in replayed:
            if seq > last_sent:
                yield _frame(seq, "a2ui", envelope)
                last_sent = seq
                last_byte = time.monotonic()

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
                if seq > last_sent:
                    yield _frame(seq, "a2ui", envelope)
                    last_sent = seq
                    last_byte = time.monotonic()
                continue

            # Timeout: cross-process catch-up, then keepalive if quiet.
            missed = await service.replay(last_sent)
            if missed is None:
                yield _frame(None, "control", {"type": "resync"})
                return
            for seq, envelope in missed:
                if seq > last_sent:
                    yield _frame(seq, "a2ui", envelope)
                    last_sent = seq
                    last_byte = time.monotonic()
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
