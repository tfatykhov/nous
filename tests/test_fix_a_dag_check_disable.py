"""Post-merge review P2-8: disabling a DAG node's heartbeat check is not compensable.

The DAG loop reads a disabled DAG-managed check as that check node's
completion and launches its successors; re-enabling the check afterwards
takes none of that back. #652 classed every check disable as compensable, so
an undoable node could make the call, and every background context got a
snapshot, a review card and a Revert for it.

Everything here is real except the model and the card publisher: the DAG
check is created by the orchestrator's own launch path, and the disable goes
through the runner's tool loop, the tool dispatcher, the heartbeat tool
handler and the check loader, with the real ledger and snapshot stores.
Auto-review is on and a publisher is wired, so a background call that is
snapshotted also gets a card.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_runner_authorization import (
    AgentRunner,
    _MockBrain,
    _MockCognitive,
    _MockHeart,
    _one_tool_call_then_done_with,
    _run_loop,
    _settings,
)

from nous.api.compensation import SnapshotStore
from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_heartbeat_tools
from nous.cognitive.ledger_store import LedgerStore
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAG_CHECK_NAME_PREFIX, DAGStore
from nous.heartbeat.dynamic import DynamicCheckLoader
from nous.heartbeat.registry import CheckRegistry
from nous.storage.models import CompensationSnapshot, DynamicCheckModel

STANDALONE = "nightly"


async def _scene(db, tmp_path, *, launch: bool = True) -> SimpleNamespace:
    """A running DAG check node with the heartbeat check its launch created,
    one standalone check, and a compensating runner for the same agent. With
    ``launch=False`` the DAG is created and its node is not launched yet."""
    agent = f"test-fixa-chk-{uuid.uuid4().hex[:8]}"
    settings = _settings(compensation_enabled=True, compensation_auto_review_enabled=True)
    loader = DynamicCheckLoader(db=db, registry=CheckRegistry(), agent_id=agent)
    store = DAGStore(db, agent, settings)
    orch = DAGOrchestrator(store=store, dynamic_loader=loader, settings=settings)
    orch.clock_wired = True
    dag = await store.create(
        DAGCreateRequest(
            name="payment",
            nodes=[DAGNodeSpec(name="wait", type=DAGNodeType.check, instructions="wait for the payment")],
        )
    )
    dag_check = None
    if launch:
        await orch.start_dag(dag.id)
        node = (await store.get_dag(dag.id)).nodes[0]
        assert node.status == "running" and node.check_name
        dag_check = node.check_name
    await loader.create_check(name=STANDALONE, description="d", prompt="p")

    runner = AgentRunner(_MockCognitive(), _MockBrain(), _MockHeart(), settings)
    dispatcher = ToolDispatcher()
    register_heartbeat_tools(dispatcher, loader)
    runner.set_dispatcher(dispatcher)
    runner.set_ledger_store(LedgerStore(db, agent))
    runner.set_snapshot_store(SnapshotStore(db, agent), str(tmp_path))
    cards: list[str] = []

    async def publish_card(tool_name, entry_id, session_id):
        cards.append(tool_name)

    runner.set_action_review_pusher(publish_card)
    return SimpleNamespace(
        db=db,
        agent=agent,
        runner=runner,
        dag_check=dag_check,
        cards=cards,
        loader=loader,
        store=store,
        orch=orch,
        dag=dag,
    )


async def _another_agents_dag_owns(db, check_name: str) -> None:
    """A check node in ANOTHER agent's DAG that carries ``check_name``."""
    store = DAGStore(db, f"test-fixa-other-{uuid.uuid4().hex[:8]}", _settings())
    dag = await store.create(
        DAGCreateRequest(name="other", nodes=[DAGNodeSpec(name="wait", type=DAGNodeType.check, instructions="wait")])
    )
    await store.update_node(dag.nodes[0].id, status="running", check_name=check_name)


async def _disable(scene, check: str, ctx: ExecutionContext):
    """One model turn that calls heartbeat_check_manage(disable); returns the tool's result."""
    scene.runner._call_api = _one_tool_call_then_done_with(
        "heartbeat_check_manage", {"action": "disable", "name": check}
    )
    _, tool_results, _, _ = await _run_loop(scene.runner, is_background=True, context=ctx)
    return tool_results[0]


async def _enabled(scene, check: str) -> bool:
    async with scene.db.session() as s:
        return (
            await s.execute(
                select(DynamicCheckModel.enabled)
                .where(DynamicCheckModel.agent_id == scene.agent)
                .where(DynamicCheckModel.name == check)
            )
        ).scalar_one()


async def _snapshots(scene) -> list[dict]:
    async with scene.db.session() as s:
        rows = await s.execute(
            select(CompensationSnapshot.snapshot_data).where(CompensationSnapshot.agent_id == scene.agent)
        )
        return list(rows.scalars())


async def test_an_undoable_node_may_not_disable_a_dag_nodes_check(db, tmp_path):
    scene = await _scene(db, tmp_path)

    result = await _disable(scene, scene.dag_check, ExecutionContext(kind="dag_node", session_id="s1", undoable=True))

    assert result.error is not None and "belongs to a DAG node" in result.error
    assert await _enabled(scene, scene.dag_check) is True
    assert await _snapshots(scene) == []
    assert scene.cards == []


async def test_a_background_disable_of_a_dag_nodes_check_runs_without_a_snapshot(db, tmp_path):
    scene = await _scene(db, tmp_path)

    result = await _disable(scene, scene.dag_check, ExecutionContext(kind="heartbeat_check", session_id="s1"))

    assert result.error is None
    assert await _enabled(scene, scene.dag_check) is False
    assert await _snapshots(scene) == []
    assert scene.cards == []  # no snapshot, so no review card and no Revert


async def test_a_standalone_check_disable_is_still_snapshotted(db, tmp_path):
    """Control: the same call on a check no DAG node of this agent owns stays
    compensable -- another agent's DAG node carrying the same name does not count."""
    scene = await _scene(db, tmp_path)
    await _another_agents_dag_owns(db, STANDALONE)

    result = await _disable(scene, STANDALONE, ExecutionContext(kind="dag_node", session_id="s1", undoable=True))

    assert result.error is None
    assert await _enabled(scene, STANDALONE) is False
    assert [s["check_name"] for s in await _snapshots(scene)] == [STANDALONE]
    assert scene.cards == ["heartbeat_check_manage"]


@pytest.mark.parametrize("undoable", [True, False], ids=["undoable", "background"])
async def test_a_failed_dag_lookup_is_named_and_leaves_nothing_to_revert(db, tmp_path, monkeypatch, caplog, undoable):
    """The lookup that tells a DAG node's check from a standalone one can
    fail, and a failure is not a "no". The disable is then one that cannot be
    reverted: refused on an undoable node, with the lookup named as the
    reason; anywhere else it runs with no snapshot, so no card and no Revert
    on what may be a DAG node's check."""
    scene = await _scene(db, tmp_path)

    async def unreadable(self, name):
        raise RuntimeError("db down")

    # raising=False: on 236c110 the store has no such lookup to replace.
    monkeypatch.setattr(SnapshotStore, "is_dag_managed_check", unreadable, raising=False)
    kind = "dag_node" if undoable else "heartbeat_check"
    reason = f"whether check {STANDALONE!r} belongs to a DAG node could not be read"

    with caplog.at_level(logging.WARNING, logger="nous.api.runner"):
        result = await _disable(scene, STANDALONE, ExecutionContext(kind=kind, session_id="s1", undoable=undoable))

    if undoable:
        assert result.error is not None and reason in result.error
        assert await _enabled(scene, STANDALONE) is True  # refused: nothing changed
    else:
        assert result.error is None
        assert await _enabled(scene, STANDALONE) is False  # it ran
        assert any(reason in rec.getMessage() for rec in caplog.records)
    assert await _snapshots(scene) == []
    assert scene.cards == []


async def test_an_undoable_disable_in_the_launch_window_is_refused(db, tmp_path):
    """The launch creates the check first and writes its name on the node
    afterwards. In between no node owns the check, but its name already says
    whose it is, so the disable is refused there as well. The real launch is
    paused in that window; the name it is about to record is the one tried."""
    scene = await _scene(db, tmp_path, launch=False)
    in_window, resume = asyncio.Event(), asyncio.Event()
    real_transition = scene.store.transition_node
    recording: dict[str, str] = {}

    async def paused_transition(*args, **kwargs):
        if kwargs.get("check_name"):
            recording["check"] = kwargs["check_name"]
            in_window.set()
            await resume.wait()
        return await real_transition(*args, **kwargs)

    scene.store.transition_node = paused_transition
    launch = asyncio.create_task(scene.orch.start_dag(scene.dag.id))
    try:
        await asyncio.wait_for(in_window.wait(), timeout=20)
        check = recording["check"]
        assert check.startswith(DAG_CHECK_NAME_PREFIX)  # the launch builds the name from the gate's prefix
        # the window: the check exists and no node has recorded it yet
        assert await _enabled(scene, check) is True
        assert (await scene.store.get_dag(scene.dag.id)).nodes[0].check_name is None

        result = await _disable(scene, check, ExecutionContext(kind="dag_node", session_id="s1", undoable=True))

        assert result.error is not None and "belongs to a DAG node" in result.error
        assert await _enabled(scene, check) is True
        assert await _snapshots(scene) == []
        assert scene.cards == []
    finally:
        resume.set()
        await asyncio.wait_for(launch, timeout=20)
    node = (await scene.store.get_dag(scene.dag.id)).nodes[0]
    assert node.status == "running" and node.check_name == check


async def test_a_standalone_check_named_like_a_dag_nodes_counts_as_one(db, tmp_path):
    """The cost of deciding by name: a standalone check somebody named
    ``dag-...`` is treated as a DAG node's -- refused on an undoable node, so
    no snapshot and no Revert -- although no node owns it."""
    scene = await _scene(db, tmp_path)
    await scene.loader.create_check(name="dag-nightly", description="d", prompt="p")

    result = await _disable(scene, "dag-nightly", ExecutionContext(kind="dag_node", session_id="s1", undoable=True))

    assert result.error is not None and "belongs to a DAG node" in result.error
    assert await _enabled(scene, "dag-nightly") is True
    assert await _snapshots(scene) == []
    assert scene.cards == []


async def test_a_check_a_node_recorded_under_another_name_is_found_by_the_query(db, tmp_path):
    """Guard for the other half of the lookup: a name without the
    orchestrator's prefix is decided by the query, so a check that one of this
    agent's DAG nodes has recorded is that node's, whatever it is called. (The
    orchestrator's own checks never get that far: their name decides.)"""
    scene = await _scene(db, tmp_path)
    dag = await scene.store.create(
        DAGCreateRequest(name="older", nodes=[DAGNodeSpec(name="wait", type=DAGNodeType.check, instructions="wait")])
    )
    await scene.store.update_node(dag.nodes[0].id, status="running", check_name=STANDALONE)

    result = await _disable(scene, STANDALONE, ExecutionContext(kind="dag_node", session_id="s1", undoable=True))

    assert result.error is not None and "belongs to a DAG node" in result.error
    assert await _enabled(scene, STANDALONE) is True
    assert await _snapshots(scene) == []
    assert scene.cards == []
