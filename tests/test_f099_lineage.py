"""F099 I3: the lineage reaches every turn descended from an intention.

Phase 1 carries it (ExecutionContext, the dispatcher's origin arguments, DAG
node launches). Nothing narrows a tool set on it yet: that is Phase 2.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from nous.api.execution_context import ExecutionContext, lineage_from_stamp
from nous.api.tools import ToolDispatcher

IID, RID = uuid.uuid4(), uuid.uuid4()
STAMP = {"id": str(IID), "root_id": str(RID), "authority": "internal_only"}
PLAN = str(uuid.uuid4())


def _row(**meta) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), parent_session_id=None, dag_node_id=None, metadata_=meta)


def test_for_subtask_reads_the_lineage_stamp():
    ctx = ExecutionContext.for_subtask(_row(intention=STAMP), "subtask-1")
    assert (ctx.kind, ctx.intention_id, ctx.root_intention_id, ctx.authority) == ("subtask", IID, RID, "internal_only")


def test_a_dag_node_keeps_its_kind_and_gains_the_lineage():
    ctx = ExecutionContext.for_subtask(_row(dag_id=str(uuid.uuid4()), node_name="n", intention=STAMP), "s")
    assert (ctx.kind, ctx.intention_id, ctx.authority) == ("dag_node", IID, "internal_only")


def test_a_scheduled_fire_gains_its_own_lineage():
    stamp = {"id": str(IID), "root_id": str(IID), "authority": "owner"}
    ctx = ExecutionContext.for_subtask(_row(schedule_id="abc", intention=stamp), "s")
    assert (ctx.kind, ctx.intention_id, ctx.root_intention_id, ctx.authority) == ("scheduled", IID, IID, "owner")


def test_a_row_without_a_stamp_is_owner():
    ctx = ExecutionContext.for_subtask(_row(), "s")
    assert (ctx.intention_id, ctx.root_intention_id, ctx.authority) == (None, None, "owner")


@pytest.mark.parametrize(
    "stamp",
    [
        "garbage",
        {"id": "not-a-uuid", "root_id": str(RID), "authority": "owner"},
        {"id": str(IID), "root_id": str(RID), "authority": "root"},
        {"id": str(IID)},
    ],
)
def test_an_unreadable_stamp_fails_closed(stamp):
    assert ExecutionContext.for_subtask(_row(intention=stamp), "s").authority == "internal_only"
    assert lineage_from_stamp(stamp)[2] == "internal_only"


def test_an_unknown_authority_is_rejected():
    with pytest.raises(ValueError):
        ExecutionContext(kind="subtask", authority="root")


def _recording_dispatcher(origin_aware: dict[str, bool]) -> tuple[ToolDispatcher, dict]:
    seen: dict[str, dict] = {}

    def _handler(name):
        async def h(**kwargs):
            seen[name] = kwargs
            return {"content": [{"type": "text", "text": "ok"}]}

        return h

    d = ToolDispatcher()
    for name, aware in origin_aware.items():
        d.register(name, _handler(name), {"type": "object", "properties": {}}, origin_aware=aware)
    return d, seen


async def test_origin_arguments_reach_origin_aware_tools_only():
    d, seen = _recording_dispatcher({"dag_create": True, "list_tasks": False})
    ctx = ExecutionContext(
        kind="subtask", session_id="subtask-1", decision_id=PLAN, intention_id=IID, root_intention_id=RID
    )
    await d.dispatch("dag_create", {"name": "d"}, session_id="subtask-1", context=ctx)
    await d.dispatch("list_tasks", {}, session_id="subtask-1", context=ctx)
    assert {k: v for k, v in seen["dag_create"].items() if k.startswith("_")} == {
        "_origin_kind": "subtask",
        "_origin_session_id": "subtask-1",
        "_decision_id": PLAN,
        "_intention_id": str(IID),
    }
    assert not [k for k in seen["list_tasks"] if k.startswith("_origin") or k == "_intention_id"]


async def test_an_interactive_turn_passes_its_channel_and_no_intention():
    d, seen = _recording_dispatcher({"schedule_task": True})
    ctx = ExecutionContext(kind="interactive", session_id="S1", channel="telegram:1")
    await d.dispatch("schedule_task", {"task": "t"}, session_id="S1", context=ctx)
    assert seen["schedule_task"]["_origin_channel"] == "telegram:1"
    assert seen["schedule_task"]["_origin_kind"] == "interactive"
    assert "_intention_id" not in seen["schedule_task"]


async def test_registering_again_without_origin_aware_stops_injection():
    d, seen = _recording_dispatcher({"spawn_sync": True})
    d.register("spawn_sync", d._handlers["spawn_sync"], {"type": "object", "properties": {}})
    await d.dispatch("spawn_sync", {"task": "t"}, context=ExecutionContext(kind="subtask", session_id="s"))
    assert "_origin_kind" not in seen["spawn_sync"]


async def test_origin_arguments_the_model_sent_are_dropped():
    """Security (blanket strip): a forged _intention_id would join a foreign lineage."""
    d, seen = _recording_dispatcher({"dag_create": True})
    forged = {
        "name": "d",
        "_intention_id": str(uuid.uuid4()),
        "_origin_kind": "continuation",
        "_origin_channel": "telegram:666",
        "_decision_id": "forged",
    }
    await d.dispatch(
        "dag_create", dict(forged), session_id="S1", context=ExecutionContext(kind="interactive", session_id="S1")
    )
    hidden = {
        k: v for k, v in seen["dag_create"].items() if k.startswith("_origin") or k in ("_intention_id", "_decision_id")
    }
    assert hidden == {"_origin_kind": "interactive", "_origin_session_id": "S1"}
    ctx = ExecutionContext(kind="subtask", session_id="s", intention_id=IID, root_intention_id=RID)
    await d.dispatch("dag_create", dict(forged), session_id="s", context=ctx)
    assert seen["dag_create"]["_intention_id"] == str(IID)


# ---------------------------------------------------------------------------
# Security: the blanket strip of model-sent "_" arguments (every tool)
# ---------------------------------------------------------------------------


async def test_a_forged_channel_never_reaches_the_subtask_row(db, mock_embeddings):
    """An MCP turn has no channel. A _channel the model sent must not route the
    result (F098): parent_channel stays NULL. A forged _session_id is replaced
    by the real one."""
    from nous.api.tools import register_subtask_tools
    from nous.config import Settings
    from nous.heart import Heart

    settings = Settings(_env_file=None, agent_id=f"f099-strip-{uuid.uuid4().hex[:8]}")
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    try:
        d = ToolDispatcher()
        register_subtask_tools(d, heart, settings)
        text, is_error = await d.dispatch(
            "spawn_task",
            {"task": "x", "_channel": "telegram:666", "_session_id": "someone-elses-session"},
            session_id="mcp-1",
            context=ExecutionContext(kind="mcp", session_id="mcp-1"),
        )
        assert not is_error, text
        (row,) = await heart.subtasks.list(limit=10)
        assert (row.parent_channel, row.parent_session_id) == (None, "mcp-1")
    finally:
        await heart.close()


async def test_a_forged_session_id_on_spawn_sync_is_dropped():
    """The dispatcher never injects _session_id into spawn_sync (I5), so a
    model-sent one would have become the row's parent_session_id."""
    d, seen = _recording_dispatcher({"spawn_sync": False})
    await d.dispatch(
        "spawn_sync",
        {"task": "t", "_session_id": "someone-elses-session", "_lookup_token": "spawn_sync-forged"},
        session_id="S1",
        context=ExecutionContext(kind="interactive", session_id="S1"),
    )
    assert "_session_id" not in seen["spawn_sync"] and "_lookup_token" not in seen["spawn_sync"]


async def test_a_forged_intention_id_is_dropped_even_with_the_flag_off():
    """No origin injection (origin_aware off), and still no forged _intention_id."""
    d, seen = _recording_dispatcher({"dag_create": False})
    await d.dispatch(
        "dag_create",
        {"name": "d", "_intention_id": str(uuid.uuid4())},
        session_id="s",
        context=ExecutionContext(kind="subtask", session_id="s"),
    )
    assert "_intention_id" not in seen["dag_create"]


async def test_a_tool_with_no_hidden_arguments_never_receives_one():
    d, seen = _recording_dispatcher({"list_tasks": False})
    await d.dispatch("list_tasks", {"_foo": 1, "status": "pending"}, context=ExecutionContext(kind="interactive"))
    assert seen["list_tasks"] == {"status": "pending"}


async def test_a_damaged_stamp_sends_an_unreadable_lineage_not_a_root():
    from nous.brain.intentions import UNREADABLE_LINEAGE

    d, seen = _recording_dispatcher({"dag_create": True})
    ctx = ExecutionContext.for_subtask(_row(intention="garbage"), "s")
    assert (ctx.intention_id, ctx.authority) == (None, "internal_only")
    await d.dispatch("dag_create", {"name": "d"}, session_id="s", context=ctx)
    assert seen["dag_create"]["_intention_id"] == UNREADABLE_LINEAGE


# ---------------------------------------------------------------------------
# Task 1.7: DAG node launches and DAG checks carry the DAG's lineage
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from sqlalchemy import update  # noqa: E402

from nous.brain.intentions import IntentionSpec  # noqa: E402
from nous.config import Settings  # noqa: E402
from nous.dag.orchestrator import DAGOrchestrator  # noqa: E402
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType  # noqa: E402
from nous.dag.store import DAGStore  # noqa: E402
from nous.heartbeat.dynamic import DynamicCheck, DynamicCheckLoader  # noqa: E402
from nous.heartbeat.registry import CheckRegistry  # noqa: E402
from nous.storage.models import Intention  # noqa: E402


@pytest.fixture
def dag_env(db):
    agent = f"f099-lin-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent, dag_callback_execution_enabled=True)
    store = DAGStore(db, agent, settings)
    subtask_mgr = AsyncMock()
    subtask_mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    loader = AsyncMock()
    loader.create_check = AsyncMock(return_value={"name": "c"})
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    orch = DAGOrchestrator(store=store, subtask_mgr=subtask_mgr, dynamic_loader=loader, settings=settings)
    return SimpleNamespace(agent=agent, db=db, store=store, subtask_mgr=subtask_mgr, loader=loader, orch=orch)


async def _dag(env, node_type: DAGNodeType, *, with_intention: bool = True, authority: str = "owner"):
    request = DAGCreateRequest(name="d", nodes=[DAGNodeSpec(name="n", type=node_type, instructions="x")])
    spec = IntentionSpec(intent="Summarise the alerts", origin_kind="interactive") if with_intention else None
    dag = await env.store.create(request, **({"intention": spec} if spec else {}))
    stamp = await env.store.intention_lineage(dag.id)
    if stamp and authority != "owner":
        async with env.db.session() as s:
            await s.execute(update(Intention).where(Intention.id == uuid.UUID(stamp["id"])).values(authority=authority))
            await s.commit()
        stamp = {**stamp, "authority": authority}
    dag = await env.store.get_dag(dag.id)
    return dag, dag.nodes[0], stamp


@pytest.mark.parametrize("node_type", [DAGNodeType.subtask, DAGNodeType.callback])
async def test_a_subtask_backed_node_carries_its_dags_lineage(dag_env, node_type):
    dag, node, stamp = await _dag(dag_env, node_type, authority="internal_only")
    await dag_env.orch._launch_subtask_node(node, dag)
    metadata = dag_env.subtask_mgr.create.call_args.kwargs["metadata"]
    assert metadata["intention"] == stamp and stamp["authority"] == "internal_only"
    assert metadata["dag_id"] == str(dag.id)
    # The node's turn reads it like any subtask row.
    ctx = ExecutionContext.for_subtask(_row(**metadata), "s")
    assert (ctx.kind, ctx.authority, str(ctx.intention_id)) == ("dag_node", "internal_only", stamp["id"])


async def test_a_check_node_carries_its_dags_lineage(dag_env):
    dag, node, stamp = await _dag(dag_env, DAGNodeType.check)
    await dag_env.orch._launch_check_node(node, dag)
    assert dag_env.loader.create_check.call_args.kwargs["metadata"]["intention"] == stamp


async def test_a_dag_without_an_intention_stamps_nothing(dag_env):
    dag, node, _ = await _dag(dag_env, DAGNodeType.subtask, with_intention=False)
    await dag_env.orch._launch_subtask_node(node, dag)
    assert "intention" not in dag_env.subtask_mgr.create.call_args.kwargs["metadata"]


@pytest.mark.parametrize("node_type", [DAGNodeType.subtask, DAGNodeType.check])
async def test_a_failed_lookup_defers_the_launch_and_creates_nothing(dag_env, node_type):
    dag, node, _ = await _dag(dag_env, node_type)
    dag_env.store.intention_lineage = AsyncMock(side_effect=RuntimeError("db down"))
    dag_env.orch._defer_node = AsyncMock()
    if node_type == DAGNodeType.check:
        await dag_env.orch._launch_check_node(node, dag)
        dag_env.loader.create_check.assert_not_called()
    else:
        await dag_env.orch._launch_subtask_node(node, dag)
        dag_env.subtask_mgr.create.assert_not_called()
    dag_env.orch._defer_node.assert_awaited_once()
    assert dag_env.orch._defer_node.await_args.args[2] == "intention lookup failed: RuntimeError"
    assert dag_env.orch._defer_node.await_args.kwargs["backstop"] == "lineage still unreadable"


async def test_a_lookup_that_keeps_failing_fails_the_node_naming_the_cause(dag_env):
    """Review S7: the real _defer_node path, capped. The node fails with the
    cause (the exception type, never SQL text) and no claim of saturation."""
    dag, node, _ = await _dag(dag_env, DAGNodeType.subtask)
    dag_env.store.intention_lineage = AsyncMock(side_effect=RuntimeError("SELECT secret statement text"))
    dag_env.orch._MAX_DEFERRALS = 2
    await dag_env.orch._launch_subtask_node(node, dag)
    await dag_env.orch._launch_subtask_node(node, dag)
    (row,) = (await dag_env.store.get_dag(dag.id)).nodes
    assert row.status == "failed"
    assert row.error == "intention lookup failed: RuntimeError — lineage still unreadable after 2 deferrals"
    dag_env.subtask_mgr.create.assert_not_called()


async def test_a_dag_check_runs_under_its_dags_lineage():
    agent = MagicMock()
    agent.run_turn = AsyncMock(return_value=('{"has_findings": false, "findings": []}', MagicMock(), {}))
    agent.end_conversation = AsyncMock()
    check = DynamicCheck(check_id="c", name="dag-x-chk", prompt="p", tools=[], runner=agent, intention=STAMP)
    await check.run()
    ctx = agent.run_turn.call_args.kwargs["context"]
    assert (ctx.kind, ctx.intention_id, ctx.root_intention_id, ctx.authority) == (
        "heartbeat_check",
        IID,
        RID,
        "internal_only",
    )


async def test_a_check_with_no_stamp_runs_as_owner():
    agent = MagicMock()
    agent.run_turn = AsyncMock(return_value=('{"has_findings": false, "findings": []}', MagicMock(), {}))
    agent.end_conversation = AsyncMock()
    await DynamicCheck(check_id="c", name="plain", prompt="p", tools=[], runner=agent).run()
    assert agent.run_turn.call_args.kwargs["context"].authority == "owner"


async def test_the_loader_carries_the_stamp_on_create_and_after_a_restart(db):
    agent_id = f"f099-lin-{uuid.uuid4().hex[:8]}"
    name = f"dag-{uuid.uuid4().hex[:6]}-chk"
    loader = DynamicCheckLoader(db, CheckRegistry(), runner=MagicMock(), agent_id=agent_id)
    await loader.create_check(
        name=name, description="d", prompt="p", interval_seconds=300, metadata={"intention": STAMP}
    )
    assert loader._registry.get_check(name)._intention == STAMP
    restarted = DynamicCheckLoader(db, CheckRegistry(), runner=MagicMock(), agent_id=agent_id)
    await restarted.sync()
    assert restarted._registry.get_check(name)._intention == STAMP
