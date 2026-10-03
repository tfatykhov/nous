"""A DAG node that is launched a second time, and a launch that could not be recorded.

Real rows throughout: the store, the subtask queue and the check loader are the
production ones. Only the failing write, or the lock that makes it fail, is staged.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nous.config import Settings
from nous.dag import orchestrator as orchestrator_module
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.heart.subtasks import SubtaskManager
from nous.heartbeat.dynamic import DynamicCheckLoader
from nous.heartbeat.registry import CheckRegistry
from nous.storage.database import Database


def _settings(**overrides) -> Settings:
    """Hermetic settings: never inherit the developer's .env."""
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


def _parts(database) -> SimpleNamespace:
    agent = f"test-fixq-{uuid.uuid4().hex[:8]}"
    settings = _settings()
    store = DAGStore(database, agent, settings)
    subtasks = SubtaskManager(database, agent)
    loader = DynamicCheckLoader(db=database, registry=CheckRegistry(), agent_id=agent)
    orch = DAGOrchestrator(store=store, subtask_mgr=subtasks, dynamic_loader=loader, settings=settings)
    orch.clock_wired = True
    return SimpleNamespace(store=store, subtasks=subtasks, loader=loader, orch=orch)


@pytest_asyncio.fixture
async def parts(db):
    return _parts(db)


async def _one_node_dag(p, node_type: DAGNodeType):
    spec = DAGNodeSpec(name="work", type=node_type, instructions="do the work")
    return await p.store.create(DAGCreateRequest(name=f"launch-{node_type.value}", nodes=[spec]))


async def _node(p, dag_id):
    return (await p.store.get_dag(dag_id)).nodes[0]


async def _checks(p) -> list[dict]:
    return (await p.loader.manage_check(action="list"))["checks"]


_LOCK_NODE = text("SELECT id FROM nous_system.dag_nodes WHERE id = :id FOR UPDATE")


class _LockTimeoutDatabase(Database):
    """The application's database with a 300 ms lock timeout on every connection."""

    def __init__(self, settings: Settings) -> None:
        self.engine = create_async_engine(settings.db_url, connect_args={"server_settings": {"lock_timeout": "300"}})
        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# A check node launched a second time
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_retried_check_node_replaces_the_check_of_its_earlier_attempt(parts):
    """Postgres only: one check per name is a constraint of the Postgres schema."""
    dag = await _one_node_dag(parts, DAGNodeType.check)
    name = f"dag-{dag.id.hex[:8]}-work"
    await parts.orch.start_dag(dag.id)
    node = await _node(parts, dag.id)
    assert (node.status, node.check_name) == ("running", name)
    first_created = (await _checks(parts))[0]["created_at"]
    # The node fails the way a check node ends: its check disabled, the node failed.
    await parts.loader.manage_check(action="disable", name=name)
    await parts.store.update_node(node.id, status="failed", error="the check gave up")
    await parts.orch.tick()
    assert (await parts.store.get_dag(dag.id)).status == "failed"

    await parts.orch.retry_node(dag.id, "work")
    await parts.orch.tick()

    node = await _node(parts, dag.id)
    assert (node.status, node.check_name, node.error) == ("running", name, None)
    checks = await _checks(parts)
    assert [(c["name"], c["enabled"]) for c in checks] == [(name, True)]
    assert checks[0]["created_at"] != first_created  # a new check, not the old one switched back on


@pytest.mark.postgres_only
async def test_a_real_lock_held_through_a_check_launch_is_launched_again(db, monkeypatch):
    """Another session holds the node's row from the moment the check exists.
    The lock outlasts every write of that launch, so the node is left `ready`;
    once the lock is gone the stale-ready sweep hands the node back, and the
    new launch meets the check the first one created."""
    monkeypatch.setattr(orchestrator_module, "_STALE_READY_GRACE_SECONDS", 0)
    database = _LockTimeoutDatabase(Settings())
    try:
        p = _parts(database)
        dag = await _one_node_dag(p, DAGNodeType.check)
        name = f"dag-{dag.id.hex[:8]}-work"
        node_id = (await _node(p, dag.id)).id
        async with db.engine.connect() as holder:
            real_create = p.loader.create_check

            async def create_then_lock(**kwargs):
                created = await real_create(**kwargs)
                await holder.execute(_LOCK_NODE, {"id": node_id})
                return created

            p.loader.create_check = create_then_lock

            await p.orch.start_dag(dag.id)

            assert (await _node(p, dag.id)).status == "ready"
            assert [(c["name"], c["enabled"]) for c in await _checks(p)] == [(name, False)]
            await holder.rollback()
            p.loader.create_check = real_create

            await p.orch.tick()

        node = await _node(p, dag.id)
        assert (node.status, node.check_name) == ("running", name)
        assert [(c["name"], c["enabled"]) for c in await _checks(p)] == [(name, True)]
    finally:
        await database.disconnect()
