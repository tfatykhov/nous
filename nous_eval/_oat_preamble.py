"""Shared helpers for eval scripts:

1. ``with_oat_preamble`` — build a multi-block system field with the
   Claude Code preamble at block 0. Required for OAT subscription not
   to 429 aggressively (mirrors `nous/api/runner.py:509`).
2. ``RateLimiter`` — minimal async token-bucket so eval scripts don't
   hammer the API even with the preamble in place. Ensures a minimum
   gap between requests.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

_CLAUDE_CODE_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."


class RateLimiter:
    """Async-friendly minimum-interval gate.

    Ensures at most one request per ``min_interval_s``. Concurrent
    callers serialize via the internal lock.

    Default 2.0s ≈ 30 RPM, well under typical OAT limits and gentle
    enough that competing prod traffic shouldn't push us over.
    """

    def __init__(self, min_interval_s: float = 2.0) -> None:
        self.min_interval_s = min_interval_s
        self._lock = asyncio.Lock()
        self._last_t = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval_s - (now - self._last_t)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_t = time.monotonic()


async def call_with_retries(
    api_client, payload: dict, *,
    rate_limiter: RateLimiter | None = None,
    max_retries: int = 6,
):
    """Call api_client.call with rate-limit gating + exponential backoff on 429.

    Backoff schedule: 4s, 8s, 16s, 32s, 64s, 128s. Total ~4 minutes if
    every attempt 429s. Other errors propagate immediately.
    """
    if rate_limiter is None:
        rate_limiter = RateLimiter(min_interval_s=2.0)
    for attempt in range(max_retries):
        await rate_limiter.acquire()
        try:
            return await api_client.call(payload)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "429" in msg and attempt < max_retries - 1:
                wait = 2 ** (attempt + 2)  # 4, 8, 16, 32, 64
                await asyncio.sleep(wait)
                continue
            raise


def with_oat_preamble(system_text: str = "") -> list[dict[str, Any]]:
    """Wrap a plain system prompt as multi-block with the OAT preamble at block 0.

    Pass the result as ``payload["system"]``. If ``system_text`` is empty,
    returns just the preamble block; otherwise returns [preamble, system_text].
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": _CLAUDE_CODE_PREAMBLE,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if system_text:
        blocks.append({
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks
