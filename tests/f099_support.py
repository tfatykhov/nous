"""Shared builders for the F099 Phase 2b tests.

The fixture is imported by name into a test module (``# noqa: F401``); the
builders are plain functions. Every environment gets its own agent id, so
tests never see each other's rows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update

from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.storage.models import Intention, ResultInbox

ON = {"result_inbox_enabled": True, "intentions_enabled": True}
CONT = {**ON, "continuation_enabled": True}
CHAN = "telegram:8080"
RESULT = "Powder: 40cm overnight on the upper mountain."


class RecordingBus:
    """A bus that keeps what it was asked to emit."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
async def env_factory(db, mock_embeddings):
    """``await env_factory(**settings_overrides)``: heart, a worker pool with a mock
    HTTP client, and a recording bus, all on one fresh agent."""
    from nous.handlers.subtask_worker import SubtaskWorkerPool
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-2b-{uuid.uuid4().hex[:8]}"
        values = {"telegram_bot_token": "", "telegram_chat_id": "", **over}
        settings = Settings(_env_file=None, agent_id=agent, **values)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        http = MagicMock()
        http.post = AsyncMock(return_value=SimpleNamespace(status_code=200))
        pool = SubtaskWorkerPool(MagicMock(), heart, settings, http_client=http)
        return SimpleNamespace(
            agent=agent, settings=settings, heart=heart, pool=pool, http=http, db=db, bus=RecordingBus()
        )

    yield build
    for heart in hearts:
        await heart.close()


async def make_subtask(env, *, policy: str = "continue", routed: bool = True, notify: bool = False):
    """A pending subtask with its intention. ``routed`` gives it F098's routing keys."""
    return await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1" if routed else None,
        parent_channel=CHAN if routed else None,
        notify=notify,
        intention=IntentionSpec(
            intent="Tell the user about the snow",
            origin_kind="interactive",
            wake_policy=policy,
            origin_channel=CHAN if routed else None,
        ),
    )


async def finish(env, subtask, how: str = "complete"):
    """Finish a subtask ('complete', 'empty' or 'fail') and return the fresh row."""
    if how == "complete":
        await env.heart.subtasks.complete(subtask.id, RESULT, final_outcome="completed")
    elif how == "empty":
        await env.heart.subtasks.complete(subtask.id, "", final_outcome="completed")
    else:
        await env.heart.subtasks.fail(subtask.id, "boom")
    return await env.heart.subtasks.get(subtask.id)


async def make_dag(env, *, policy: str = "continue", origin_channel: str | None = None, status: str = "completed"):
    """A terminal DAG with its intention. Returns ``(dag, store)``."""
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
    from nous.dag.store import DAGStore

    store = DAGStore(env.db, env.agent, env.settings)
    dag = await store.create(
        DAGCreateRequest(
            name="snow-dag",
            origin_channel=origin_channel,
            nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")],
        ),
        intention=IntentionSpec(
            intent="Summarise the alerts", origin_kind="interactive", wake_policy=policy, origin_channel=origin_channel
        ),
    )
    await store.update_dag_status(dag.id, status, result_summary="ok")
    return await store.get_dag(dag.id), store


def dag_kwargs(dag, *, origin_channel=None, origin_session_id=None, status="completed") -> dict:
    """The keyword arguments ``record_dag_result`` takes, for ``dag``."""
    return dict(
        dag_id=dag.id,
        name=dag.name,
        status=status,
        summary="ok",
        blocked=False,
        origin_channel=origin_channel,
        origin_session_id=origin_session_id,
        generation=dag.delivery_generation,
    )


async def inbox_rows(env, source_id=None) -> list[ResultInbox]:
    async with env.db.session() as s:
        query = select(ResultInbox).where(ResultInbox.agent_id == env.agent)
        if source_id is not None:
            query = query.where(ResultInbox.source_id == source_id)
        return list((await s.execute(query.order_by(ResultInbox.created_at))).scalars().all())


async def intention_of(env, source_kind: str, source_id) -> Intention | None:
    return await env.heart.intentions.get_for_source(source_kind, source_id)


async def set_intention(env, intention_id, **values) -> None:
    """Move an intention by hand, as a later PR's runner would."""
    async with env.db.session() as s:
        await s.execute(update(Intention).where(Intention.id == intention_id).values(**values))
        await s.commit()


async def make_root(env, *, policy: str = "continue", routed: bool = True) -> Intention:
    """The root intention of a pending subtask (``policy``, origin channel ``CHAN`` when ``routed``)."""
    st = await make_subtask(env, policy=policy, routed=routed)
    return await intention_of(env, "subtask", st.id)


async def make_child(env, parent: Intention, *, authority: str = "internal_only") -> Intention:
    """A child intention under ``parent``, as a continuation turn's spawn writes it (a pending subtask)."""
    st = await env.heart.subtasks.create(
        task="follow-up work",
        intention=IntentionSpec(
            intent="next step", origin_kind="continuation", parent_id=parent.id, origin_authority=authority
        ),
    )
    return await intention_of(env, "subtask", st.id)
