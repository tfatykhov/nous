"""F099 section 4.3 Phase 1: every intention closes as 'legacy' when its source finishes.

Routing is F098 Phase A's, unchanged (test_f099_routing_pins.py). The close
happens before each writer's routing-key check, so a result nobody is routed
(a scheduled fire, a monitor) still closes; in spawn_task's inline path;
and in the reconciler's intentions pass, for a writer that never ran.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_subtask_tools
from nous.brain import intentions
from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.heart.result_inbox import record_dag_result
from nous.storage.models import ResultInbox, Subtask

ON = {"result_inbox_enabled": True, "intentions_enabled": True}
CONT = {**ON, "continuation_enabled": True}
CHAN = "telegram:8080"
RESULT = "Powder: 40cm overnight on the upper mountain."
INTERACTIVE = ExecutionContext(kind="interactive", session_id="S1", channel=CHAN)


class _Turn:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.block = False

    async def run_turn(self, **_):
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        return RESULT, None, {"input_tokens": 1, "output_tokens": 1}

    async def end_conversation(self, *a, **k):
        return True


@pytest.fixture
async def close_env(db, mock_embeddings):
    from nous.handlers.subtask_worker import SubtaskWorkerPool
    from nous.heart import Heart

    hearts = []

    async def build(**over):
        agent = f"f099-close-{uuid.uuid4().hex[:8]}"
        settings = Settings(
            _env_file=None,
            agent_id=agent,
            subtask_payload_schema_enabled=True,
            subtask_max_attempts=1,
            inline_subtask_timeout=30,
            telegram_bot_token="",
            telegram_chat_id="",
            **over,
        )
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        hearts.append(heart)
        turn = _Turn()
        d = ToolDispatcher()
        register_subtask_tools(d, heart, settings, runner=turn)
        pool = SubtaskWorkerPool(MagicMock(), heart, settings)
        return SimpleNamespace(agent=agent, settings=settings, heart=heart, d=d, turn=turn, pool=pool, db=db)

    yield build
    for heart in hearts:
        await heart.close()


async def _subtask(env, *, routed: bool, origin_kind: str = "interactive") -> Subtask:
    return await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1" if routed else None,
        parent_channel=CHAN if routed else None,
        intention=IntentionSpec(intent="Tell the user about the snow", origin_kind=origin_kind),
    )


async def _inbox(env, source_id) -> list[ResultInbox]:
    async with env.db.session() as s:
        return list((await s.execute(select(ResultInbox).where(ResultInbox.source_id == source_id))).scalars().all())


async def _of(env, kind, source_id):
    return await env.heart.intentions.get_for_source(kind, source_id)


async def test_an_unrouted_result_closes_its_intention_and_writes_no_row(close_env):
    env = await close_env(**ON)
    st = await _subtask(env, routed=False, origin_kind="scheduler")
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")
    await env.pool._record_inbox(st)  # the worker's terminal hook
    it = await _of(env, "subtask", st.id)
    assert (it.state, it.close_reason) == ("closed", "legacy") and it.result_at is not None
    assert await _inbox(env, st.id) == []


@pytest.mark.parametrize("finish", ["complete", "fail"])
async def test_a_routed_result_closes_its_intention_and_its_row_names_it(close_env, finish):
    env = await close_env(**ON)
    st = await _subtask(env, routed=True)
    if finish == "complete":
        await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")
    else:
        await env.heart.subtasks.fail(st.id, "boom")
    await env.pool._record_inbox(st)
    it = await _of(env, "subtask", st.id)
    (row,) = await _inbox(env, st.id)
    assert it.state == "closed" and row.intention_id == it.id
    assert (row.channel, row.session_id) == (CHAN, "S1")  # I5: routed as before


@pytest.mark.parametrize("policy", ["report", "continue"])
async def test_a_report_or_continue_intention_closes_as_legacy_with_continuation_off(close_env, policy):  # PIN
    """PIN. Spec section 4.3 Phase 1: with NOUS_CONTINUATION_ENABLED off, F098 still
    consumes every result, so every policy closes as 'legacy'. I4's report close-at-write
    ('delivered', in the insert's transaction) is Phase 2 and needs the flag: that is
    the next test, which is why this one was renamed (its assertions are Phase 1's, unchanged)."""
    env = await close_env(**ON)
    st = await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1",
        parent_channel=CHAN,
        intention=IntentionSpec(intent="Tell the user about the snow", origin_kind="interactive", wake_policy=policy),
    )
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")
    await env.pool._record_inbox(st)
    it = await _of(env, "subtask", st.id)
    assert (it.wake_policy, it.state, it.close_reason) == (policy, "closed", "legacy")
    (row,) = await _inbox(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)  # routed as F098 A


async def test_a_report_intention_closes_as_delivered_with_continuation_on(close_env):
    """F099 Phase 2 (I4): a report closes as 'delivered' in its insert's transaction and still
    routes as F098 A (the chat consumes it); a continue one is no longer routed by F098 at all."""
    env = await close_env(**CONT)
    st = await env.heart.subtasks.create(
        task="Check the snow report",
        parent_session_id="S1",
        parent_channel=CHAN,
        intention=IntentionSpec(intent="Tell the user about the snow", origin_kind="interactive", wake_policy="report"),
    )
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")
    await env.pool._record_inbox(st)
    it = await _of(env, "subtask", st.id)
    assert (it.wake_policy, it.state, it.close_reason) == ("report", "closed", "delivered")
    (row,) = await _inbox(env, st.id)
    assert (row.channel, row.session_id, row.intention_id) == (CHAN, "S1", it.id)


async def _dag(env, *, origin_channel=None):
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
    from nous.dag.store import DAGStore

    store = DAGStore(env.db, env.agent, env.settings)
    dag = await store.create(
        DAGCreateRequest(
            name="d",
            origin_channel=origin_channel,
            nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")],
        ),
        intention=IntentionSpec(intent="Summarise the alerts", origin_kind="work_queue"),
    )
    await store.update_dag_status(dag.id, "completed", result_summary="ok")
    return dag


@pytest.mark.parametrize("id_form", ["uuid", "str", "hex"])
@pytest.mark.parametrize("routed", [False, True])
async def test_a_dag_result_closes_its_intention_whatever_its_routing(close_env, routed, id_form):
    """The listener passes the payload's string id; the close must use the
    parsed UUID (moved above the routing check), or a hex id closes nothing."""
    env = await close_env(**ON)
    dag = await _dag(env, origin_channel=CHAN if routed else None)
    dag_id = {"uuid": dag.id, "str": str(dag.id), "hex": dag.id.hex}[id_form]
    written = await record_dag_result(
        env.heart.result_inbox,
        env.settings,
        dag_id=dag_id,
        name="d",
        status="completed",
        summary="ok",
        blocked=False,
        origin_channel=CHAN if routed else None,
        origin_session_id=None,
    )
    it = await _of(env, "dag", dag.id)
    assert (it.state, it.close_reason) == ("closed", "legacy")
    assert written is routed
    if routed:
        (row,) = await _inbox(env, dag.id)
        assert row.intention_id == it.id


@pytest.mark.parametrize("hardened", [False, True], ids=["legacy path", "hardened path"])
async def test_an_inline_spawn_closes_its_intention_in_the_call(close_env, hardened):
    env = await close_env(**ON, subtask_hardening_enabled=hardened)
    await env.d.dispatch(
        "spawn_task",
        {"task": "t", "await_result": True, "intent": "Tell the user about the snow"},
        session_id="S1",
        context=INTERACTIVE,
    )
    (st,) = await env.heart.subtasks.list(limit=10)
    it = await _of(env, "subtask", st.id)
    assert (it.wake_policy, it.state, it.close_reason) == ("none", "closed", "legacy")


async def test_an_inline_spawn_closes_as_delivered_with_continuation_on(close_env):
    env = await close_env(**CONT)
    await env.d.dispatch(
        "spawn_task",
        {"task": "t", "await_result": True, "intent": "Tell the user about the snow"},
        session_id="S1",
        context=INTERACTIVE,
    )
    (st,) = await env.heart.subtasks.list(limit=10)
    it = await _of(env, "subtask", st.id)
    assert (it.wake_policy, it.state, it.close_reason) == ("none", "closed", "delivered")


async def test_a_cancelled_inline_call_still_closes_its_intention(close_env):
    env = await close_env(**ON)
    env.turn.block = True
    call = asyncio.create_task(
        env.d.dispatch(
            "spawn_task", {"task": "t", "await_result": True, "intent": "x"}, session_id="S1", context=INTERACTIVE
        )
    )
    await asyncio.wait_for(env.turn.started.wait(), timeout=10)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    (st,) = await env.heart.subtasks.list(limit=10)
    assert (await _of(env, "subtask", st.id)).state == "closed"


async def test_a_failed_close_never_costs_the_result_its_inbox_row(close_env, monkeypatch):
    env = await close_env(**ON)
    st = await _subtask(env, routed=True)
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")

    async def boom(*args, **kwargs):
        raise RuntimeError("intentions down")

    monkeypatch.setattr(intentions, "close_for_source", boom)
    await env.pool._record_inbox(st)
    (row,) = await _inbox(env, st.id)
    assert row.intention_id is None


async def test_with_the_flag_off_a_writer_leaves_intentions_alone(close_env):
    env = await close_env(result_inbox_enabled=True)
    st = await _subtask(env, routed=True)  # as if spawned while the flag was on
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")
    await env.pool._record_inbox(st)
    assert (await _of(env, "subtask", st.id)).state == "pending"
    (row,) = await _inbox(env, st.id)
    assert row.intention_id is None


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_the_reconciler_closes_what_no_writer_closed(close_env):
    from nous.heart.result_reconciler import build_reconciler

    env = await close_env(**ON)
    finished = await _subtask(env, routed=False, origin_kind="scheduler")
    await env.heart.subtasks.complete(finished.id, RESULT, final_outcome="completed")  # no hook ran
    running = await _subtask(env, routed=False)
    container = await env.heart.schedules.create(
        task="t",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="x", origin_kind="interactive", container=True),
    )
    results = await build_reconciler(env.db, env.heart.result_inbox, env.settings).run_once()
    assert results["intentions"] == 1
    assert (await _of(env, "subtask", finished.id)).state == "closed"
    assert (await _of(env, "subtask", running.id)).state == "pending"
    assert (await _of(env, "schedule", container.id)).state == "pending"  # its schedule still fires


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_the_reconciler_closes_a_container_whose_close_failed(close_env, monkeypatch):
    """D4's close runs after the deactivation commits and may fail (or never
    run, if the process exits in between). The schedule must stay
    deactivated, and the intentions pass must close the container."""
    from nous.heart.result_reconciler import build_reconciler

    env = await close_env(**ON)
    schedule = await env.heart.schedules.create(
        task="t",
        schedule_type="recurring",
        interval_seconds=1800,
        intention=IntentionSpec(intent="x", origin_kind="interactive", container=True),
    )
    real_close = intentions.close_for_source

    async def failing(*args, **kwargs):
        raise RuntimeError("intentions down")

    monkeypatch.setattr(intentions, "close_for_source", failing)
    await env.heart.schedules.deactivate(schedule.id)  # the close fails, quietly
    monkeypatch.setattr(intentions, "close_for_source", real_close)
    assert (await env.heart.schedules.get(schedule.id)).active is False
    assert (await _of(env, "schedule", schedule.id)).state == "pending"

    results = await build_reconciler(env.db, env.heart.result_inbox, env.settings).run_once()
    container = await _of(env, "schedule", schedule.id)
    assert results["intentions"] == 1
    assert (container.state, container.close_reason, container.result_at) == ("closed", "legacy", None)


def test_the_intentions_pass_runs_only_with_the_flag_on():
    from nous.heart.result_reconciler import build_reconciler

    on = build_reconciler(MagicMock(), MagicMock(), Settings(_env_file=None, **ON))
    off = build_reconciler(MagicMock(), MagicMock(), Settings(_env_file=None, result_inbox_enabled=True))
    assert "intentions" in [p.name for p in on._passes]
    assert "intentions" not in [p.name for p in off._passes]


async def test_the_inbox_repair_names_and_closes_the_intention(close_env):
    from nous.heart.result_reconciler import InboxSubtaskPass

    env = await close_env(**ON)
    await env.heart.result_inbox.ensure_enabled_at()  # repairs cover results finished after this
    st = await _subtask(env, routed=True)
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")  # the hook's write was lost
    await InboxSubtaskPass(env.db, env.heart.result_inbox, env.settings).run(limit=10)
    it = await _of(env, "subtask", st.id)
    (row,) = await _inbox(env, st.id)
    assert row.intention_id == it.id and it.state == "closed"


async def test_end_to_end_a_chat_spawn_is_recorded_then_closed(db, mock_embeddings):
    """The real runner, dispatcher, spawn_task, worker and inbox writer. A
    chat turn's spawn records a continue intention carrying the turn's Plan
    decision; the worker finishing it closes it as legacy, and the inbox row
    names it and is routed exactly as F098 routes it."""
    from nous.api.runner import AgentRunner, ApiResponse
    from nous.cognitive.schemas import Assessment, FrameSelection, TurnContext
    from nous.handlers.subtask_worker import SubtaskWorkerPool
    from nous.heart import Heart

    plan = str(uuid.uuid4())

    class _Cognitive:
        async def pre_turn(self, *a, **k):
            return TurnContext(
                system_prompt="You are Nous.",
                frame=FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="default"),
                decision_id=plan,
                active_censors=[],
                context_token_estimate=100,
            )

        async def post_turn(self, agent_id, session_id, turn_result, turn_context, **k):
            return Assessment(actual=turn_result.response_text[:200])

        async def end_session(self, *a, **k):
            return None

        async def list_frames(self, *a, **k):
            return []

    class _Stub:
        async def close(self):
            pass

    agent = f"f099-e2e-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent, ANTHROPIC_API_KEY="test-key", **ON)
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    runner = AgentRunner(_Cognitive(), _Stub(), _Stub(), settings)
    d = ToolDispatcher()
    register_subtask_tools(d, heart, settings, runner=runner)
    runner.set_dispatcher(d)
    calls = {"n": 0}

    async def fake_call_api(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            spawn = {"task": "Check the snow report", "intent": "Tell the user whether to drive up"}
            return ApiResponse(
                content=[{"type": "tool_use", "id": "t1", "name": "spawn_task", "input": spawn}], stop_reason="tool_use"
            )
        return ApiResponse(content=[{"type": "text", "text": "On it."}], stop_reason="end_turn")

    runner._call_api = fake_call_api
    try:
        await runner.run_turn(
            "S1", "check the snow", context=ExecutionContext(kind="interactive", session_id="S1", channel=CHAN)
        )
        (st,) = await heart.subtasks.list(limit=10)
        it = await heart.intentions.get_for_source("subtask", st.id)
        assert (it.wake_policy, it.origin_kind, it.origin_channel, str(it.origin_decision_id)) == (
            "continue",
            "interactive",
            CHAN,
            plan,
        )
        assert st.metadata_["plan_decision_id"] == plan

        class _WorkerTurn:
            async def run_turn(self, **kwargs):
                return RESULT, None, {"input_tokens": 1, "output_tokens": 1}

            async def end_conversation(self, *a, **k):
                return None

        pool = SubtaskWorkerPool(_WorkerTurn(), heart, settings)
        await pool._process_subtask(await heart.subtasks.dequeue("worker-0"))
        it = await heart.intentions.get_for_source("subtask", st.id)
        assert (it.state, it.close_reason) == ("closed", "legacy")
        async with db.session() as s:
            (row,) = (await s.execute(select(ResultInbox).where(ResultInbox.source_id == st.id))).scalars().all()
        assert row.intention_id == it.id and (row.channel, row.session_id) == (CHAN, "S1")
    finally:
        runner._api_shared = True
        await runner.close()
        await heart.close()


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_a_subtask_cancelled_while_pending_closes_without_a_result_at(close_env):
    from nous.heart.result_reconciler import build_reconciler

    env = await close_env(**ON)
    st = await _subtask(env, routed=False)
    assert await env.heart.subtasks.cancel(st.id)
    await build_reconciler(env.db, env.heart.result_inbox, env.settings).run_once()
    it = await _of(env, "subtask", st.id)
    assert (it.state, it.close_reason) == ("closed", "legacy")
    assert it.closed_at is not None
    assert it.result_at is None


@pytest.mark.postgres_only  # CAST(text AS uuid) join
async def test_a_completed_subtask_closed_by_the_pass_has_a_result_at(close_env):
    from nous.heart.result_reconciler import build_reconciler

    env = await close_env(**ON)
    st = await _subtask(env, routed=False)
    await env.heart.subtasks.complete(st.id, RESULT, final_outcome="completed")  # no hook ran
    await build_reconciler(env.db, env.heart.result_inbox, env.settings).run_once()
    it = await _of(env, "subtask", st.id)
    assert it.state == "closed"
    assert it.result_at is not None


@pytest.mark.postgres_only  # CAST(text AS uuid) in the close
async def test_a_cancelled_inline_call_closes_without_a_result_at(close_env):
    env = await close_env(**ON)
    env.turn.block = True
    call = asyncio.create_task(
        env.d.dispatch(
            "spawn_task", {"task": "t", "await_result": True, "intent": "x"}, session_id="S1", context=INTERACTIVE
        )
    )
    await asyncio.wait_for(env.turn.started.wait(), timeout=10)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    (st,) = await env.heart.subtasks.list(limit=10)
    it = await _of(env, "subtask", st.id)
    assert (it.state, it.close_reason) == ("closed", "legacy")
    assert it.result_at is None
