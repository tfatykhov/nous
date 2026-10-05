"""F099 Phase 0a: carry the reason (storage only, no flag).

The turn's Plan decision (TurnContext.decision_id) reaches the subtasks it
spawns, and DAGs record an original_request. Routing is pinned separately in
test_f099_routing_pins.py.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, create_subtask_tools, register_subtask_tools
from nous.config import Settings
from nous.heart.subtasks import SubtaskManager
from nous.storage.models import Subtask

PLAN = str(uuid.uuid4())


class _SpyCognitive:
    """pre_turn returns a TurnContext whose Plan decision is ``decision_id``."""

    def __init__(self, decision_id: str | None) -> None:
        from nous.cognitive.schemas import FrameSelection, TurnContext

        self._ctx = TurnContext(
            system_prompt="You are Nous.",
            frame=FrameSelection(frame_id="task", frame_name="Task", confidence=0.9, match_method="default"),
            decision_id=decision_id,
            active_censors=[],
            context_token_estimate=100,
        )

    async def pre_turn(self, agent_id, session_id, user_input, **kwargs):
        return self._ctx

    async def post_turn(self, agent_id, session_id, turn_result, turn_context, **kwargs):
        from nous.cognitive.schemas import Assessment

        return Assessment(actual=turn_result.response_text[:200])

    async def end_session(self, *args, **kwargs):
        return None

    async def list_frames(self, *args, **kwargs):
        return []


class _Stub:
    async def close(self):
        pass


@pytest.fixture
async def wired(db, mock_embeddings):
    """A real AgentRunner + ToolDispatcher whose fake model calls spawn_task once."""
    from nous.api.anthropic_client import StreamEvent
    from nous.api.runner import AgentRunner, ApiResponse
    from nous.heart import Heart

    built = []

    async def build(decision_id: str | None, **over):
        agent = f"f099-0a-{uuid.uuid4().hex[:8]}"
        settings = Settings(_env_file=None, agent_id=agent, ANTHROPIC_API_KEY="test-key", **over)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        runner = AgentRunner(_SpyCognitive(decision_id), _Stub(), _Stub(), settings)
        dispatcher = ToolDispatcher()
        register_subtask_tools(dispatcher, heart, settings, runner=runner)
        runner.set_dispatcher(dispatcher)
        spawn = {"task": "Check the snow report"}
        calls = {"api": 0, "stream": 0}

        async def fake_call_api(*args, **kwargs):
            calls["api"] += 1
            if calls["api"] == 1:
                return ApiResponse(
                    content=[{"type": "tool_use", "id": "t1", "name": "spawn_task", "input": dict(spawn)}],
                    stop_reason="tool_use",
                )
            return ApiResponse(content=[{"type": "text", "text": "On it."}], stop_reason="end_turn")

        async def fake_stream(*args, **kwargs):
            calls["stream"] += 1
            if calls["stream"] == 1:
                yield StreamEvent(type="tool_start", tool_name="spawn_task", tool_id="t1", block_index=1)
                yield StreamEvent(type="tool_input_delta", text=json.dumps(spawn), block_index=1)
                yield StreamEvent(type="block_stop", block_index=1)
                yield StreamEvent(type="done", stop_reason="tool_use")
            else:
                yield StreamEvent(type="text_delta", text="On it.")
                yield StreamEvent(type="done", stop_reason="end_turn")

        runner._call_api = fake_call_api
        runner._call_api_stream = MagicMock(side_effect=fake_stream)
        env = SimpleNamespace(agent=agent, settings=settings, heart=heart, runner=runner, spawn=spawn, db=db)
        built.append(env)
        return env

    yield build
    for env in built:
        env.runner._api_shared = True
        await env.runner.close()
        await env.heart.close()


async def _spawned(env) -> list[Subtask]:
    async with env.db.session() as s:
        return list((await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all())


async def test_run_turn_records_the_plan_decision_on_the_spawned_subtask(wired):
    env = await wired(PLAN)
    await env.runner.run_turn("S1", "check the snow", context=ExecutionContext(kind="interactive", session_id="S1"))
    (row,) = await _spawned(env)
    assert row.metadata_["plan_decision_id"] == PLAN


async def test_stream_chat_records_the_plan_decision_on_the_spawned_subtask(wired):
    env = await wired(PLAN)
    async for _event in env.runner.stream_chat("S2", "check the snow"):
        pass
    (row,) = await _spawned(env)
    assert row.metadata_["plan_decision_id"] == PLAN


async def test_a_turn_without_a_plan_decision_records_none(wired):
    env = await wired(None)
    await env.runner.run_turn("S3", "check the snow", context=ExecutionContext(kind="interactive", session_id="S3"))
    (row,) = await _spawned(env)
    assert "plan_decision_id" not in row.metadata_


async def test_dispatcher_passes_the_decision_to_spawn_task_and_spawn_sync_only():
    seen: dict[str, dict] = {}

    def _handler(name):
        async def h(**kwargs):
            seen[name] = kwargs
            return {"content": [{"type": "text", "text": "ok"}]}

        return h

    d = ToolDispatcher()
    names = ("spawn_task", "spawn_sync", "schedule_task", "dag_create")
    for name in names:
        d.register(name, _handler(name), {"type": "object", "properties": {}})
    ctx = ExecutionContext(kind="interactive", session_id="S1", decision_id=PLAN)
    for name in names:
        await d.dispatch(name, {"task": "x", "name": "x"}, session_id="S1", context=ctx)
    assert seen["spawn_task"]["_decision_id"] == PLAN
    assert seen["spawn_sync"]["_decision_id"] == PLAN
    assert "_decision_id" not in seen["schedule_task"]
    assert "_decision_id" not in seen["dag_create"]


async def test_a_decision_id_sent_by_the_model_is_dropped():
    seen: dict[str, dict] = {}

    async def h(**kwargs):
        seen["spawn_task"] = kwargs
        return {"content": [{"type": "text", "text": "ok"}]}

    d = ToolDispatcher()
    d.register("spawn_task", h, {"type": "object", "properties": {}})
    forged = {"task": "x", "_decision_id": "forged"}
    await d.dispatch(
        "spawn_task", dict(forged), session_id="S1", context=ExecutionContext(kind="interactive", session_id="S1")
    )
    assert "_decision_id" not in seen["spawn_task"]
    ctx = ExecutionContext(kind="interactive", session_id="S1", decision_id=PLAN)
    await d.dispatch("spawn_task", dict(forged), session_id="S1", context=ctx)
    assert seen["spawn_task"]["_decision_id"] == PLAN


async def test_spawn_sync_forwards_the_decision_to_the_row_it_creates(db):
    class _Turn:
        async def run_turn(self, **_):
            return "done", None, {}

        async def end_conversation(self, *a, **k):
            return True

    manager = SubtaskManager(db, f"f099-0a-{uuid.uuid4().hex[:8]}")
    heart = SimpleNamespace(subtasks=manager, check_censors=AsyncMock(return_value=[]))
    settings = Settings(
        _env_file=None,
        subtask_hardening_enabled=True,
        subtask_payload_schema_enabled=True,
        subtask_max_attempts=1,
        telegram_bot_token="",
        telegram_chat_id="",
    )
    tools = create_subtask_tools(heart, settings, runner=_Turn())
    await tools["spawn_sync"](task="inline work", _decision_id=PLAN)
    (row,) = await manager.list(limit=10)
    assert row.metadata_["plan_decision_id"] == PLAN
    assert row.parent_session_id is None  # I5: spawn_sync still records no session


# ---------------------------------------------------------------------------
# original_request (D1: dag_create stores its description; the work queue
# stores the item title)
# ---------------------------------------------------------------------------


class _Orch:
    clock_wired = True
    approvals_wired = False

    def __init__(self, settings):
        self._settings = settings
        self.start_dag = AsyncMock()


async def _dag_of(db, agent):
    from nous.storage.models import ExecutionDAG

    async with db.session() as s:
        return (await s.execute(select(ExecutionDAG).where(ExecutionDAG.agent_id == agent))).scalar_one()


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("  Summarise overnight alerts for the morning brief ", "Summarise overnight alerts for the morning brief"),
        ("", None),
    ],
)
async def test_dag_create_stores_its_description_as_the_original_request(db, description, expected):
    from nous.api.tools import register_dag_tools
    from nous.dag.store import DAGStore

    agent = f"f099-0a-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent)
    d = ToolDispatcher()
    register_dag_tools(d, DAGStore(db, agent, settings), _Orch(settings), settings=settings)
    text, is_error = await d.dispatch(
        "dag_create",
        {
            "name": "nightly",
            "description": description,
            "nodes": [{"name": "n", "type": "subtask", "instructions": "x"}],
        },
        session_id="S1",
        context=ExecutionContext(kind="interactive", session_id="S1"),
    )
    assert not is_error, text
    assert (await _dag_of(db, agent)).original_request == expected


def _wq(db, agent, path, request_factory=None):
    from nous.dag.orchestrator import DAGOrchestrator
    from nous.dag.store import DAGStore
    from nous.heart.work_queue import WorkQueueItemManager
    from nous.heartbeat.work_queue import FileJsonlAdapter, WorkQueueCheck

    settings = Settings(_env_file=None, agent_id=agent, work_queue_enabled=True, work_queue_source="file_jsonl")
    store = DAGStore(db, agent, settings)
    orch = DAGOrchestrator(store=store, subtask_mgr=AsyncMock(), dynamic_loader=MagicMock(), settings=settings)
    items = WorkQueueItemManager(db, agent)
    check = WorkQueueCheck(
        adapter=FileJsonlAdapter(str(path)),
        items_mgr=items,
        dag_store=store,
        orchestrator=orch,
        settings=settings,
        request_factory=request_factory,
    )
    return check, items


async def test_work_queue_dag_records_the_item_title(tmp_path, db):
    path = tmp_path / "queue.jsonl"
    path.write_text(json.dumps({"external_id": "a1", "title": "Fix the flaky login test", "body": "do it"}))
    agent = f"f099-0a-{uuid.uuid4().hex[:8]}"
    check, _ = _wq(db, agent, path)
    await check.run()
    assert (await _dag_of(db, agent)).original_request == "Fix the flaky login test"


async def test_a_custom_request_factory_still_records_the_item_title(tmp_path, db):
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType

    def factory(item):
        return DAGCreateRequest(
            name="wq-custom", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions=item.body)]
        )

    path = tmp_path / "queue.jsonl"
    path.write_text(json.dumps({"external_id": "a2", "title": "Rotate the API keys", "body": "do it"}))
    agent = f"f099-0a-{uuid.uuid4().hex[:8]}"
    check, _ = _wq(db, agent, path, request_factory=factory)
    await check.run()
    assert (await _dag_of(db, agent)).original_request == "Rotate the API keys"


async def test_a_factory_that_sets_its_own_original_request_keeps_it(tmp_path, db):
    from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType

    def factory(item):
        return DAGCreateRequest(
            name="wq-own",
            original_request="from the factory",
            nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions=item.body)],
        )

    path = tmp_path / "queue.jsonl"
    path.write_text(json.dumps({"external_id": "a3", "title": "ignored", "body": "do it"}))
    agent = f"f099-0a-{uuid.uuid4().hex[:8]}"
    check, _ = _wq(db, agent, path, request_factory=factory)
    await check.run()
    assert (await _dag_of(db, agent)).original_request == "from the factory"


async def test_an_orphan_redispatch_records_the_item_title(tmp_path, db):
    from nous.heartbeat.work_queue import WorkItem

    agent = f"f099-0a-{uuid.uuid4().hex[:8]}"
    check, items = _wq(db, agent, tmp_path / "unused.jsonl")
    row = await items.claim_for_dispatch(source="file_jsonl", external_id="o1", payload={"title": "Renew the TLS cert"})
    item = WorkItem(
        external_id="o1", title="Renew the TLS cert", body="do it", state="open", terminal=False, payload={}
    )
    assert await check._reconcile_orphan(row.id, item, []) is True
    assert (await _dag_of(db, agent)).original_request == "Renew the TLS cert"


@pytest.mark.parametrize("title", ["", "   "])
async def test_a_blank_item_title_leaves_original_request_null(tmp_path, db, title):
    path = tmp_path / "queue.jsonl"
    path.write_text(json.dumps({"external_id": "b1", "title": title, "body": "do it"}))
    agent = f"f099-0a-{uuid.uuid4().hex[:8]}"
    check, _ = _wq(db, agent, path)
    await check.run()
    assert (await _dag_of(db, agent)).original_request is None


# ---------------------------------------------------------------------------
# A plan decision a subtask records is not deleted by post_turn's cleanup
# ---------------------------------------------------------------------------


async def _decision_exists(brain, decision_id: str) -> bool:
    return await brain.get(uuid.UUID(decision_id)) is not None


@pytest.fixture
async def _plan_env(db):
    from nous.brain.brain import Brain
    from nous.cognitive.deliberation import DeliberationEngine

    settings = Settings(_env_file=None, agent_id=f"f099-0a-{uuid.uuid4().hex[:8]}")
    brain = Brain(database=db, settings=settings)
    yield brain, DeliberationEngine(brain, settings), SubtaskManager(db, settings.agent_id), settings.agent_id
    await brain.close()


async def _plan_decision(brain, agent: str) -> str:
    from nous.brain.schemas import ReasonInput, RecordInput

    out = await brain.record(
        RecordInput(
            description="Plan for a turn that spawns work",
            confidence=0.8,
            category="process",
            stakes="medium",
            reasons=[ReasonInput(type="analysis", text="t")],
        ),
    )
    return str(out.id)


@pytest.mark.parametrize("path", ["informational", "quality_gate"])
async def test_a_plan_decision_a_subtask_names_survives_cleanup(_plan_env, path):
    brain, engine, mgr, agent = _plan_env
    decision_id = await _plan_decision(brain, agent)
    await mgr.create(task="spawned", metadata={"plan_decision_id": decision_id})
    if path == "informational":
        await engine.delete(decision_id)
    else:
        assert await engine.finalize(decision_id, description="short", confidence=0.8) is None
    assert await _decision_exists(brain, decision_id)


@pytest.mark.parametrize("path", ["informational", "quality_gate"])
async def test_a_decision_nothing_spawned_from_is_still_deleted(_plan_env, path):
    brain, engine, mgr, agent = _plan_env
    decision_id = await _plan_decision(brain, agent)
    await mgr.create(task="unrelated", metadata={"plan_decision_id": str(uuid.uuid4())})
    if path == "informational":
        await engine.delete(decision_id)
    else:
        assert await engine.finalize(decision_id, description="short", confidence=0.8) is None
    assert not await _decision_exists(brain, decision_id)
