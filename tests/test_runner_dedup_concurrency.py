"""F071: ContextVar isolation across concurrent run_turn-style calls.

The spec's primary correctness invariant is ``contextvar leaks across turns``
must be Low likelihood. These tests pin that invariant by exercising the
ContextVar set/reset pattern under:

- Two parallel coroutines (per-asyncio-Task isolation)
- A synchronous exception inside the turn body (`.reset()` still fires)
- ``asyncio.CancelledError`` mid-turn (`.reset()` still fires)
"""

from __future__ import annotations

import asyncio

import pytest

from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS


@pytest.mark.asyncio
async def test_contextvar_per_task_isolation():
    """Two concurrent Tasks each setting CURRENT_TURN_EXCLUDE_IDS observe
    only their own value. Uses ``asyncio.Event`` for deterministic interleave
    rather than fragile sleep granularity on slow CI."""
    observed: dict[str, dict] = {}
    set_count = {"n": 0}
    both_set = asyncio.Event()

    async def turn(session_id: str, fact_ids: set[str]) -> None:
        token = CURRENT_TURN_EXCLUDE_IDS.set({"fact": fact_ids})
        try:
            set_count["n"] += 1
            if set_count["n"] == 2:
                both_set.set()
            await both_set.wait()
            observed[session_id] = CURRENT_TURN_EXCLUDE_IDS.get()
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

    await asyncio.gather(
        turn("s1", {"a", "b"}),
        turn("s2", {"x", "y", "z"}),
    )

    assert observed["s1"] == {"fact": {"a", "b"}}
    assert observed["s2"] == {"fact": {"x", "y", "z"}}
    # Outside both turns, the var is restored to its default
    assert CURRENT_TURN_EXCLUDE_IDS.get() is None


@pytest.mark.asyncio
async def test_contextvar_reset_on_sync_exception():
    """A RuntimeError raised inside the body still fires `.reset(token)`
    via `finally`. Restores default."""

    async def failing_turn() -> None:
        token = CURRENT_TURN_EXCLUDE_IDS.set({"fact": {"x"}})
        try:
            raise RuntimeError("boom")
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

    with pytest.raises(RuntimeError):
        await failing_turn()
    assert CURRENT_TURN_EXCLUDE_IDS.get() is None


@pytest.mark.asyncio
async def test_contextvar_reset_on_cancellation():
    """asyncio.CancelledError mid-turn (e.g. client disconnect during
    stream_chat, subtask cancellation) still triggers `.reset()` so the
    contextvar leaves the Task in a clean state."""
    started = asyncio.Event()

    async def long_turn() -> None:
        token = CURRENT_TURN_EXCLUDE_IDS.set({"fact": {"x"}})
        try:
            started.set()
            await asyncio.sleep(10)  # cancellation point
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

    task = asyncio.create_task(long_turn())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert CURRENT_TURN_EXCLUDE_IDS.get() is None
