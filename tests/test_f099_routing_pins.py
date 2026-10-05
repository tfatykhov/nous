"""F099 Phase 0: pins on how F098 routes and classifies each kind of spawned row.

F099 records WHY work was spawned (Phase 0a: the Plan decision and
original_request; Phase 1: brain.intentions). Recording the origin must never
change routing (spec I5): F098 Phase A claims inbox rows by parent_channel /
parent_session_id, and Phase C reads the same two columns as "conversation
origin". These tests pin the routing columns every spawn path writes, and what
Phase A and Phase C then do with the row.

The expected values were read from main + #694 when the pins were written. A
change to any of them is a routing change and needs its own review: it is not
something an F099 PR may do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_dag_tools, register_subtask_tools
from nous.config import Settings
from nous.dag.store import DAGStore
from nous.handlers.task_scheduler import TaskScheduler
from nous.heart.result_inbox import record_subtask_result
from nous.storage.models import ExecutionDAG, ResultInbox, Schedule, Subtask

CHAN = "telegram:9099"
SESSION = "S-pin"
# Long enough for Phase C's result_memory_min_chars (200); no secret-like token.
PIN_RESULT = "Snow report: " + "fresh powder on the upper mountain, light wind. " * 8
INTERACTIVE = ExecutionContext(kind="interactive", session_id=SESSION, channel=CHAN)


def _settings(agent: str, **over) -> Settings:
    base = dict(
        _env_file=None,
        agent_id=agent,
        result_inbox_enabled=True,
        # Phase C's classifier reads this (tier 2). result_memory_enabled stays
        # off: the pins call the pure classifier, and the background memory writes must not race teardown.
        result_memory_scheduled=True,
        subtask_payload_schema_enabled=True,
        subtask_hardening_enabled=False,
        subtask_max_attempts=1,
        inline_subtask_timeout=30,
        telegram_bot_token="",
        telegram_chat_id="",
    )
    base.update(over)
    return Settings(**base)


class _Turn:
    """Stands in for AgentRunner: an inline turn finishes at once with PIN_RESULT."""

    async def run_turn(self, **_: object):
        return PIN_RESULT, None, {"input_tokens": 1, "output_tokens": 1}

    async def end_conversation(self, *args: object, **kwargs: object) -> bool:
        return True


class _Orchestrator:
    """What dag_create needs from the orchestrator."""

    clock_wired = True
    approvals_wired = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.start_dag = AsyncMock()


@pytest.fixture
async def make_env(db, mock_embeddings):
    """Build one agent's real Heart + ToolDispatcher with the spawn and DAG tools."""
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-pin-{uuid.uuid4().hex[:8]}"
        settings = _settings(agent, **over)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        dispatcher = ToolDispatcher()
        register_subtask_tools(dispatcher, heart, settings, runner=_Turn())
        register_dag_tools(dispatcher, DAGStore(db, agent, settings), _Orchestrator(settings), settings=settings)
        return SimpleNamespace(agent=agent, settings=settings, heart=heart, dispatcher=dispatcher, db=db)

    yield build
    for heart in hearts:
        await heart.close()


async def _dispatch(env, name: str, args: dict, ctx: ExecutionContext = INTERACTIVE) -> tuple[str, bool]:
    return await env.dispatcher.dispatch(name, args, session_id=ctx.session_id, context=ctx)


async def _only_subtask(env) -> Subtask:
    async with env.db.session() as s:
        rows = (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all()
    assert len(rows) == 1, rows
    return rows[0]


async def _spawn(env) -> Subtask:
    text, is_error = await _dispatch(env, "spawn_task", {"task": "Check the snow report"})
    assert not is_error, text
    row = await _only_subtask(env)
    await env.heart.subtasks.complete(row.id, PIN_RESULT, final_outcome="completed")
    return await env.heart.subtasks.get(row.id)


async def _inline(env) -> Subtask:
    text, is_error = await _dispatch(env, "spawn_task", {"task": "Check the snow report", "await_result": True})
    assert not is_error, text
    return await _only_subtask(env)


async def _spawn_sync(env) -> Subtask:
    # The stub never calls submit_final_report, so the hardened run may end
    # completed or failed; the routing columns are what is pinned.
    await _dispatch(env, "spawn_sync", {"task": "Check the snow report"})
    return await _only_subtask(env)


async def _fire(env, *, notify: bool) -> Subtask:
    text, is_error = await _dispatch(
        env, "schedule_task", {"task": "Check the snow report", "every": "30 minutes", "notify": notify}
    )
    assert not is_error, text
    async with env.db.session() as s:
        schedule = (await s.execute(select(Schedule).where(Schedule.agent_id == env.agent))).scalar_one()
        # I5: schedule_task records no creating session, so a fire has no routing key.
        assert schedule.created_by_session is None
        await s.execute(
            update(Schedule)
            .where(Schedule.id == schedule.id)
            .values(next_fire_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()
    assert await TaskScheduler(env.heart, env.settings)._fire_due_tasks() == 1
    row = await _only_subtask(env)
    await env.heart.subtasks.complete(row.id, PIN_RESULT, final_outcome="completed")
    return await env.heart.subtasks.get(row.id)


async def _fire_notify(env) -> Subtask:
    return await _fire(env, notify=True)


async def _fire_silent(env) -> Subtask:
    return await _fire(env, notify=False)


BUILD = {
    "spawn": _spawn,
    "inline": _inline,
    "spawn_sync": _spawn_sync,
    "fire_notify": _fire_notify,
    "fire_silent": _fire_silent,
}
# spawn_sync is registered only with the hardened executor.
PATH_SETTINGS = {"spawn_sync": {"subtask_hardening_enabled": True}}


def _routing(row: Subtask) -> dict:
    return {
        "parent_session_id": row.parent_session_id,
        "parent_channel": row.parent_channel,
        "worker_id": row.worker_id,
        "notify": row.notify,
    }


async def _inbox_keys(env, source_id) -> list[tuple]:
    async with env.db.session() as s:
        rows = (
            await s.execute(
                select(ResultInbox.channel, ResultInbox.session_id).where(ResultInbox.source_id == source_id)
            )
        ).all()
    return [tuple(r) for r in rows]


@pytest.mark.parametrize(
    ("path", "routing", "inbox"),
    [
        (
            "spawn",
            {"parent_session_id": SESSION, "parent_channel": CHAN, "worker_id": None, "notify": False},
            [(CHAN, SESSION)],
        ),
        ("inline", {"parent_session_id": SESSION, "parent_channel": CHAN, "worker_id": "inline", "notify": False}, []),
        ("spawn_sync", {"parent_session_id": None, "parent_channel": None, "worker_id": "inline", "notify": False}, []),
        ("fire_notify", {"parent_session_id": None, "parent_channel": None, "worker_id": None, "notify": True}, []),
        ("fire_silent", {"parent_session_id": None, "parent_channel": None, "worker_id": None, "notify": False}, []),
    ],
)
async def test_phase_a_routing_of_each_spawned_row(make_env, path, routing, inbox):
    env = await make_env(**PATH_SETTINGS.get(path, {}))
    row = await BUILD[path](env)
    assert _routing(row) == routing
    if row.worker_id != "inline":
        # The worker's terminal hook, called as the worker calls it. Inline
        # rows never reach it: their result returns in the calling turn.
        await record_subtask_result(env.heart.result_inbox, row, env.settings)
    assert await _inbox_keys(env, row.id) == inbox


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("spawn", ("write", "tier1")),
        ("inline", ("write", "tier1")),
        ("spawn_sync", ("skip", "inline")),
        ("fire_notify", ("write", "tier2")),
        ("fire_silent", ("skip", "background")),
    ],
)
async def test_phase_c_classification_of_each_spawned_row(make_env, path, expected):
    from nous.heart.result_memory import classify_for_memory

    env = await make_env(**PATH_SETTINGS.get(path, {}))
    row = await BUILD[path](env)
    decision = classify_for_memory(row, env.settings)
    assert (decision.decision, decision.reason) == expected


@pytest.mark.parametrize(
    ("ctx", "origin"),
    [
        (INTERACTIVE, (CHAN, SESSION)),
        (ExecutionContext(kind="mcp", session_id="mcp-pin"), (None, "mcp-pin")),
        (ExecutionContext(kind="subtask", session_id="subtask-pin"), (None, None)),
    ],
    ids=["interactive", "mcp", "background"],
)
async def test_dag_origin_written_by_dag_create(make_env, ctx, origin):
    env = await make_env()
    text, is_error = await _dispatch(
        env,
        "dag_create",
        {
            "name": "pin-dag",
            "description": "Pin the origin",
            "nodes": [{"name": "n", "type": "subtask", "instructions": "x"}],
        },
        ctx,
    )
    assert not is_error, text
    async with env.db.session() as s:
        dag = (await s.execute(select(ExecutionDAG).where(ExecutionDAG.agent_id == env.agent))).scalar_one()
    assert (dag.origin_channel, dag.origin_session_id) == origin
