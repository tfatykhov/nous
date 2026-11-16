"""F115: the largest time limit of a spawned task binds it when it starts. A task spawn_task created, or a goal
session's first task, that is still waiting when the limit is lowered runs with the lower limit; a running task, a
workflow step, a schedule's run and a companion action keep their own (real Postgres)."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from nous.config import Settings
from nous.handlers.subtask_worker import (
    OWN_TIME_LIMIT_MARKERS,
    SubtaskWorkerPool,
    bound_by_max_timeout,
    start_timeout,
)
from nous.handlers.task_scheduler import create_fire
from nous.heart.heart import Heart
from nous.storage.models import Schedule, Subtask

pytestmark = pytest.mark.postgres_only
ROOT = Path(__file__).resolve().parents[1]

# Every place in the code that creates a subtask, and the marker of a creator that sets its own time limit (None: no
# such marker, the largest time limit binds the task when it starts).
CREATORS = {
    "nous/api/tools.py": None,  # spawn_task
    "nous/goals/roots.py": None,  # a goal session's first task
    "nous/handlers/task_scheduler.py": '"schedule_id"',
    "nous/dag/orchestrator.py": '"dag_id"',
    "nous/a2ui/actions.py": '"a2ui_action_id"',
}
_CREATE = re.compile(r"(?:subtasks|_subtask_mgr)\.create\(")


def _call_text(source: str, start: int) -> str:
    """The text of the call that opens at ``start`` (its brackets balanced)."""
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError("an unbalanced call")


def test_every_creator_of_a_subtask_writes_the_marker_the_worker_reads():
    found: dict[str, list[str]] = {}
    for path in (ROOT / "nous").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in _CREATE.finditer(source):
            found.setdefault(path.relative_to(ROOT).as_posix(), []).append(_call_text(source, match.end() - 1))
    assert set(found) == set(CREATORS)
    for rel, calls in found.items():
        assert len(calls) == 1, rel
        marker = CREATORS[rel]
        if marker is None:
            assert not any(f'"{key}"' in calls[0] for key in OWN_TIME_LIMIT_MARKERS), rel
            assert "dag_node_id" not in calls[0], rel
        else:
            assert marker in calls[0], rel
    assert "dag_node_id=node.id" in found["nous/dag/orchestrator.py"][0]


def _row(timeout: int = 3600, metadata: dict | None = None, dag_node_id: uuid.UUID | None = None) -> Subtask:
    return Subtask(task="look", timeout_seconds=timeout, metadata_=metadata or {}, dag_node_id=dag_node_id)


def _settings(**over) -> Settings:
    values = {
        "subtask_default_timeout": 600,
        "subtask_max_timeout": 3600,
        "subtask_hardening_enabled": False,
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        **over,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("metadata", "dag_node_id", "bound"),
    [
        ({}, None, True),  # spawn_task
        ({"plan_decision_id": "d", "intention": {"id": "i"}}, None, True),  # spawn_task with its own keys
        ({"goal_id": "g", "goal_session": 1}, None, True),  # a goal session's first task
        ({"schedule_id": "s"}, None, False),  # a schedule's run
        ({"schedule_id": "s", "fix": "f"}, None, False),  # a schedule's fix run
        ({"dag_id": "d", "node_name": "n"}, uuid.uuid4(), False),  # a workflow step
        ({"dag_id": "d", "node_name": "n"}, None, False),  # a workflow step whose node row is gone
        ({}, uuid.uuid4(), False),  # a row tied to a workflow step
        ({"a2ui_surface_id": "s", "a2ui_action_id": "a", "max_attempts": 1}, None, False),  # a companion action
    ],
)
def test_which_tasks_the_largest_time_limit_binds_at_start(metadata, dag_node_id, bound):
    row = _row(metadata=metadata, dag_node_id=dag_node_id)
    assert bound_by_max_timeout(row) is bound
    settings = _settings()
    object.__setattr__(settings, "subtask_max_timeout", 600)  # lowered after the task was queued at 3,600 s
    assert start_timeout(row, settings) == (600 if bound else 3600)


def test_a_raised_limit_never_lengthens_a_task_and_no_limit_falls_back_to_the_default():
    settings = _settings()
    object.__setattr__(settings, "subtask_max_timeout", 9000)
    assert start_timeout(_row(timeout=600), settings) == 600
    assert start_timeout(_row(timeout=0), settings) == 600  # the default
    object.__setattr__(settings, "subtask_max_timeout", 300)
    assert start_timeout(_row(timeout=0), settings) == 300


# ---- through the worker, on real rows --------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def rig(db):
    settings = _settings(agent_id=f"test-time-limit-{uuid.uuid4().hex[:8]}")
    heart = Heart(db, settings)
    pool = SubtaskWorkerPool(runner=AsyncMock(), heart=heart, settings=settings, bus=None)
    yield heart, settings, pool
    async with db.session() as session:
        await session.execute(delete(Subtask).where(Subtask.agent_id == settings.agent_id))
        await session.execute(delete(Schedule).where(Schedule.agent_id == settings.agent_id))
        await session.commit()
    await heart.close()


async def test_a_waiting_task_starts_with_the_lowered_limit(rig):
    heart, settings, pool = rig
    queued = await heart.subtasks.create(task="look", timeout=3600)  # as spawn_task writes it: no marker
    object.__setattr__(settings, "subtask_max_timeout", 600)
    started = await heart.subtasks.dequeue("w1")
    assert started.id == queued.id and started.timeout_seconds == 3600
    assert start_timeout(started, settings) == 600
    # Through the worker: lowered to one second, the task that asked for an hour times out after one.
    object.__setattr__(settings, "subtask_max_timeout", 1)
    again = await heart.subtasks.create(task="look", timeout=3600)

    async def slow(subtask):
        await asyncio.sleep(30)

    pool._execute_subtask = slow
    await pool._process_subtask(await heart.subtasks.dequeue("w1"))
    row = await heart.subtasks.get(again.id)
    assert row.status == "failed" and row.error == "Subtask timed out after 1s"


async def test_a_schedule_run_keeps_its_own_limit(rig):
    heart, settings, _pool = rig
    schedule = await heart.schedules.create(
        task="look", schedule_type="recurring", interval_seconds=3600, notify=False, timeout=3600
    )
    run = await create_fire(heart, settings, schedule)
    object.__setattr__(settings, "subtask_max_timeout", 600)
    assert run.metadata_["schedule_id"] == schedule.id.hex
    assert start_timeout(run, settings) == 3600


async def test_a_running_task_keeps_the_limit_it_started_with(rig):
    heart, settings, pool = rig
    queued = await heart.subtasks.create(task="look", timeout=3600)
    running, release = asyncio.Event(), asyncio.Event()

    async def execute(subtask):
        running.set()
        await release.wait()

    pool._execute_subtask = execute
    work = asyncio.create_task(pool._process_subtask(await heart.subtasks.dequeue("w1")))
    await asyncio.wait_for(running.wait(), 10)
    object.__setattr__(settings, "subtask_max_timeout", 1)  # lowered while it runs
    await asyncio.sleep(1.5)
    assert not work.done()
    release.set()
    await asyncio.wait_for(work, 10)
    assert (await heart.subtasks.get(queued.id)).status != "failed"
