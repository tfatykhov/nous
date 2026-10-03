"""A subtask row is run by exactly one executor, whichever path created it.

spawn_task(await_result=True) and spawn_sync run their subtask inline, in the
calling turn. No worker may take that row: not while the turn runs it, not
after the call was cancelled, and not after the process running it was killed.

Every test uses the real SubtaskManager on the test database and the real
tool closures or worker pool. Only the agent turn (the runner) and the censor
check are replaced.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nous.api.tools import create_subtask_tools
from nous.config import Settings
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.heart.subtasks import SubtaskManager

# How long a test waits for something that should happen at once. Only
# reached when the behaviour under test is broken.
WAIT = 5.0


def _settings(*, hardened: bool = False) -> Settings:
    return Settings(
        subtask_hardening_enabled=hardened,
        subtask_max_attempts=1,
        inline_subtask_timeout=30,
        subtask_workers=1,
        subtask_poll_interval=0.01,
        subtask_cleanup_timeout_seconds=5,
        telegram_bot_token="",
        telegram_chat_id="",
    )


def _heart(db) -> SimpleNamespace:
    """The heart the tools see: a real subtask manager under an agent id of
    this test's own, and a censor check that lets every task through."""
    manager = SubtaskManager(db, f"test-fix-w-{uuid.uuid4().hex[:8]}")
    return SimpleNamespace(subtasks=manager, check_censors=AsyncMock(return_value=[]))


class _Turn:
    """Stands in for AgentRunner. While a turn runs, it records what a worker
    polling the queue at that moment would get, and how the row looks."""

    def __init__(self, subtasks: SubtaskManager, *, until: asyncio.Event | None = None) -> None:
        self._subtasks = subtasks
        self._until = until
        self.started = asyncio.Event()
        self.dequeued: list = []
        self.rows: list[tuple[str, str | None, bool]] = []

    async def run_turn(self, *, session_id: str, user_message: str, **_: object) -> tuple[str, None, dict]:
        self.dequeued.append(await self._subtasks.dequeue("worker-0"))
        for row in await self._subtasks.list(limit=10):
            self.rows.append((row.status, row.worker_id, row.started_at is not None))
        # Set only once no query is in flight: a test may cancel the call now.
        self.started.set()
        if self._until is not None:
            await self._until.wait()
        return "done", None, {}

    async def end_conversation(self, session_id: str, **_: object) -> bool:
        return True


BOTH_PATHS = pytest.mark.parametrize("hardened", [False, True], ids=["legacy path", "hardened path"])


async def _spawn(tools: dict, entry: str):
    if entry == "spawn_task":
        return await tools["spawn_task"](task="inline work", await_result=True, _session_id="parent")
    return await tools["spawn_sync"](task="inline work", _session_id="parent")


# ---------------------------------------------------------------------------
# While the calling turn runs it, a worker cannot claim the row
# ---------------------------------------------------------------------------


@BOTH_PATHS
@pytest.mark.parametrize("entry", ["spawn_task", "spawn_sync"])
async def test_a_worker_cannot_claim_a_subtask_that_runs_inline(db, hardened, entry):
    heart = _heart(db)
    turn = _Turn(heart.subtasks)
    tools = create_subtask_tools(heart, _settings(hardened=hardened), runner=turn)

    before = datetime.now(UTC)
    await _spawn(tools, entry)
    after = datetime.now(UTC)

    assert turn.dequeued == [None], "a worker could claim the subtask the calling turn was running"
    assert turn.rows == [("running", "inline", True)], "the inline row was not claimed while it ran"
    (row,) = await heart.subtasks.list(limit=10)
    assert row.status in ("completed", "failed") and row.worker_id == "inline"
    # The SQLite test database returns naive datetimes; they are UTC.
    started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=UTC)
    assert before <= started <= after, "the claim did not record when the inline call started"


@pytest.mark.postgres_only  # the SQLite test database cannot serve a worker and the inline call at once
@BOTH_PATHS
async def test_the_worker_pool_does_not_run_an_inline_subtask_a_second_time(db, hardened):
    heart = _heart(db)
    polls = 0
    dequeue = heart.subtasks.dequeue

    async def counted_dequeue(worker_id: str):
        nonlocal polls
        polls += 1
        return await dequeue(worker_id)

    heart.subtasks.dequeue = counted_dequeue
    settings = _settings(hardened=hardened)
    in_background = _Turn(heart.subtasks)
    pool = SubtaskWorkerPool(runner=in_background, heart=heart, settings=settings)

    class _InlineTurn(_Turn):
        async def run_turn(self, **kwargs: object) -> tuple[str, None, dict]:
            # Let the idle worker poll the queue a few times while this runs.
            seen = polls
            loop = asyncio.get_running_loop()
            deadline = loop.time() + WAIT
            while polls < seen + 3 and loop.time() < deadline:
                await asyncio.sleep(0.01)
            return "done", None, {}

    tools = create_subtask_tools(heart, settings, runner=_InlineTurn(heart.subtasks))
    await pool.start()
    try:
        await tools["spawn_task"](task="inline work", await_result=True, _session_id="parent")
    finally:
        await asyncio.wait_for(pool.stop(), WAIT)

    assert polls >= 3, "the worker never polled while the inline turn ran"
    assert not in_background.started.is_set(), "a worker ran the inline subtask a second time"


# ---------------------------------------------------------------------------
# What stays as it was: rows for the queue are queued
# ---------------------------------------------------------------------------


async def test_a_fire_and_forget_subtask_is_still_queued_for_a_worker(db):
    """Parity pin: green before this change too."""
    heart = _heart(db)
    tools = create_subtask_tools(heart, _settings(), runner=_Turn(heart.subtasks))

    await tools["spawn_task"](task="background work", _session_id="parent")

    claimed = await heart.subtasks.dequeue("worker-0")
    assert claimed is not None and claimed.task == "background work"


async def test_an_inline_request_without_a_runner_is_still_left_for_a_worker(db):
    """Parity pin: green before this change too. Without a runner nothing runs
    the row inline, so it must stay where a worker can find it."""
    heart = _heart(db)
    tools = create_subtask_tools(heart, _settings(), runner=None)

    result = await tools["spawn_task"](task="inline work", await_result=True, _session_id="parent")

    assert result.get("is_error") is True
    claimed = await heart.subtasks.dequeue("worker-0")
    assert claimed is not None and claimed.task == "inline work"


async def test_a_subtask_created_without_a_worker_id_is_left_for_a_worker(db):
    """Parity pin: green before this change too. app.act, the DAG orchestrator
    and the task scheduler create their rows this way, for the worker pool."""
    heart = _heart(db)

    await heart.subtasks.create(task="queued work")

    claimed = await heart.subtasks.dequeue("worker-0")
    assert claimed is not None and claimed.task == "queued work"
