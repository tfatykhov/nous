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
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from nous.api.tools import create_subtask_tools
from nous.config import Settings
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.heart.subtasks import SubtaskManager
from nous.storage.models import Subtask

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


# ---------------------------------------------------------------------------
# A cancelled inline call leaves its row to nobody
# ---------------------------------------------------------------------------


async def _expect(event: asyncio.Event, what: str) -> None:
    try:
        await asyncio.wait_for(event.wait(), WAIT)
    except TimeoutError:
        pytest.fail(f"{what} (waited {WAIT:.0f}s)")


async def _cancelled_inline_call(tools: dict, turn: _Turn) -> asyncio.Task:
    """Start an inline spawn whose turn never ends, and cancel it once the turn runs."""
    call = asyncio.create_task(tools["spawn_task"](task="inline work", await_result=True, _session_id="parent"))
    await _expect(turn.started, "the inline turn never started")
    call.cancel()
    return call


@BOTH_PATHS
async def test_a_cancelled_inline_call_cancels_its_subtask(db, hardened):
    heart = _heart(db)
    turn = _Turn(heart.subtasks, until=asyncio.Event())
    tools = create_subtask_tools(heart, _settings(hardened=hardened), runner=turn)

    call = await _cancelled_inline_call(tools, turn)
    with pytest.raises(asyncio.CancelledError):
        await call

    (row,) = await heart.subtasks.list(limit=10)
    assert (row.status, row.final_outcome) == ("cancelled", "cancelled"), (
        "a cancelled inline call left its subtask open"
    )
    assert await heart.subtasks.dequeue("worker-0") is None


async def test_a_second_cancellation_does_not_keep_the_subtask_open(db):
    heart = _heart(db)
    closing, may_close, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancel = heart.subtasks.cancel

    async def slow_cancel(subtask_id):
        closing.set()
        try:
            await may_close.wait()
            return await cancel(subtask_id)
        finally:
            closed.set()

    heart.subtasks.cancel = slow_cancel
    turn = _Turn(heart.subtasks, until=asyncio.Event())
    tools = create_subtask_tools(heart, _settings(), runner=turn)

    call = await _cancelled_inline_call(tools, turn)
    await _expect(closing, "a cancelled inline call never closed its subtask")
    call.cancel()  # the caller is cancelled again while its subtask is being closed
    with pytest.raises(asyncio.CancelledError):
        await call
    may_close.set()
    await _expect(closed, "the closing of the subtask never ended")

    (row,) = await heart.subtasks.list(limit=10)
    assert row.status == "cancelled", "the second cancellation interrupted the closing of the subtask"


async def test_a_failed_close_still_lets_the_cancellation_through(db, caplog):
    heart = _heart(db)

    async def broken_cancel(subtask_id):
        raise RuntimeError("database gone")

    heart.subtasks.cancel = broken_cancel
    turn = _Turn(heart.subtasks, until=asyncio.Event())
    tools = create_subtask_tools(heart, _settings(), runner=turn)

    call = await _cancelled_inline_call(tools, turn)
    with pytest.raises(asyncio.CancelledError):
        await call

    said = [r.getMessage() for r in caplog.records if r.name == "nous.api.tools"]
    assert any("Could not mark inline subtask" in line for line in said), f"the failed close was not logged: {said}"


# ---------------------------------------------------------------------------
# What a killed process left running is closed when the next one starts
# ---------------------------------------------------------------------------


async def test_a_start_cancels_the_inline_subtasks_a_killed_process_left_running(db, caplog):
    """A redeploy kills the process, so a cut-off call never closed its row.

    The rows as a killed process leaves them: two inline calls cut off, one
    just now and one long past its timeout, an inline call that had finished,
    and a worker's row long past its timeout. Another agent's inline call is
    running in the same database.
    """
    heart = _heart(db)
    subtasks = heart.subtasks
    await subtasks.create(task="inline, cut off just now", worker_id="inline")
    cut_off_long_ago = await subtasks.create(task="inline, cut off long ago", timeout=60, worker_id="inline")
    finished = await subtasks.create(task="inline, finished", worker_id="inline")
    await subtasks.complete(finished.id, "done", final_outcome="completed")
    stale = await subtasks.create(task="a worker's, cut off long ago", timeout=60)
    assert (await subtasks.dequeue("worker-0")).id == stale.id
    async with db.session() as session:
        await session.execute(
            update(Subtask)
            .where(Subtask.id.in_([cut_off_long_ago.id, stale.id]))
            .values(started_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()
    other_agent = SubtaskManager(db, f"test-fix-w-{uuid.uuid4().hex[:8]}")
    elsewhere = await other_agent.create(task="another agent's inline call", worker_id="inline")

    settings = _settings()
    settings.subtask_workers = 0  # start() only closes what was left; nothing runs
    pool = SubtaskWorkerPool(runner=_Turn(subtasks), heart=heart, settings=settings)
    for _ in range(2):  # the second start finds nothing left
        await pool.start()
        await pool.stop()

    rows = {row.task: row for row in await subtasks.list(limit=10)}
    for task in ("inline, cut off just now", "inline, cut off long ago"):
        assert (rows[task].status, rows[task].final_outcome) == ("cancelled", "cancelled"), f"left open: {task}"
        assert rows[task].completed_at is not None
    assert rows["inline, finished"].status == "completed", "a start rewrote a finished inline subtask"
    claimed = await subtasks.dequeue("worker-0")
    assert claimed is not None and claimed.task == "a worker's, cut off long ago", "the stale row was not re-queued"
    assert await subtasks.dequeue("worker-0") is None, "a worker could take an inline subtask a killed process left"
    assert (await other_agent.get(elsewhere.id)).status == "running", "a start closed another agent's inline call"
    said = [r.getMessage() for r in caplog.records if r.name == "nous.heart.subtasks"]
    assert [line for line in said if "a previous process left running" in line] == [
        "Cancelled 2 inline subtasks a previous process left running"
    ], said
