"""Shared builders for the F099 Phase 2b tests.

The fixture is imported by name into a test module (``# noqa: F401``); the
builders are plain functions. Every environment gets its own agent id, so
tests never see each other's rows.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text, update

from nous.brain import continuation
from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.storage.models import Intention, IntentionArrival, ResultInbox

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


async def make_dag(
    env,
    *,
    policy: str = "continue",
    origin_channel: str | None = None,
    status: str = "completed",
    parent: Intention | None = None,
):
    """A terminal DAG with its intention (a child of ``parent`` when given). Returns ``(dag, store)``."""
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
            intent="Summarise the alerts",
            origin_kind="continuation" if parent is not None else "interactive",
            wake_policy=policy,
            origin_channel=origin_channel,
            parent_id=parent.id if parent is not None else None,
            origin_authority="internal_only" if parent is not None else None,
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


async def record(env, intention: Intention, *, body: str = RESULT, generation: int = 0):
    """A continue result for ``intention``'s source, written as the worker hook writes it (T4/T6)."""
    async with env.db.session() as s:
        recorded = await continuation.record_result(
            s,
            env.agent,
            intention_id=intention.id,
            source_kind=intention.source_kind,
            source_id=uuid.UUID(intention.source_id),
            msg_type="INFORM",
            title="Snow report",
            body=body,
            source_generation=generation,
            settings=env.settings,
        )
        await s.commit()
    return recorded


async def age(env, *intention_ids, seconds: float = 60) -> None:
    """Make the results of these intentions ``seconds`` old (the debounce reads ``result_at``)."""
    when = datetime.now(UTC) - timedelta(seconds=seconds)
    for intention_id in intention_ids:
        await set_intention(env, intention_id, result_at=when)


async def claim(env, root_id, *, debounce: float = 0, max_wait: float = 0):
    """``continuation.claim_root`` in its own session, committed when it claimed."""
    async with env.db.session() as s:
        got = await continuation.claim_root(
            s, env.agent, root_id, token=uuid.uuid4(), debounce_s=debounce, max_wait_s=max_wait
        )
        if got is not None:
            await s.commit()
    return got


async def eligible(env, *, debounce: float = 20, max_wait: float = 120, limit: int = 50):
    async with env.db.session() as s:
        return await continuation.eligible_roots(s, env.agent, debounce_s=debounce, max_wait_s=max_wait, limit=limit)


async def until_a_backend_waits_on_a_lock(env, *, at_least: int = 1) -> None:
    """Return once ``at_least`` backends of this database wait on a lock. Callers bound it with
    ``asyncio.wait_for``: a lock that is never waited on fails the test, it does not hang it."""
    while True:
        async with env.db.session() as s:  # a fresh transaction each time: the stats view is snapshotted
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query ILIKE '%brain.intentions%'"
                    )
                )
            ).scalar_one()
        if waiting >= at_least:
            return
        await asyncio.sleep(0.05)


async def add_arrival(
    env,
    root_id,
    n: int,
    *,
    progress: bool | None = False,
    gate_reason: str | None = None,
    tokens: tuple[int, int] = (0, 0),
    decision: str = "continue",
    outcome: str = "resolved",
) -> None:
    """An arrival row as a committed arrival leaves it (the budgets read these)."""
    async with env.db.session() as s:
        s.add(
            IntentionArrival(
                agent_id=env.agent,
                root_id=root_id,
                n=n,
                intention_ids=[root_id],
                claim_token=uuid.uuid4(),
                decision=decision,
                progress_claimed=bool(progress),
                progress=progress,
                gate_reason=gate_reason,
                tokens_in=tokens[0],
                tokens_out=tokens[1],
                outcome=outcome,
            )
        )
        await s.commit()
