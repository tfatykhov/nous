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
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
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


# ---------------------------------------------------------------------------
# A launch that could not be recorded is launched again; a subtask only if it never ran
# ---------------------------------------------------------------------------


async def _subtask_statuses(p) -> list[str]:
    return sorted(s.status for s in await p.subtasks.list(limit=50))


class _ServerError(Exception):
    """What the driver puts under a SQLAlchemy error: the message and the
    server's SQLSTATE."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _db_error(cls, message: str, sqlstate: str | None = None):
    """A SQLAlchemy error the way a statement raises it."""
    orig = _ServerError(message, sqlstate) if sqlstate else Exception(message)
    return cls("UPDATE nous_system.dag_nodes SET status=$1", {}, orig)


def _lock_timeout() -> DBAPIError:
    return _db_error(DBAPIError, "canceling statement due to lock timeout", "55P03")


def _fail_running_write(
    store, exc: BaseException, *, times: int = 1, after_commit: bool = False, meanwhile=None
) -> None:
    """The launch's `running` write raises `exc` the next `times` times, before
    or after it lands; `meanwhile`, if given, runs while it waits. Every other
    write goes through untouched."""
    real = store.transition_node
    left = [times]

    async def flaky(node_id, **kwargs):
        if kwargs.get("status") == "running" and left[0] > 0:
            left[0] -= 1
            if after_commit:
                await real(node_id, **kwargs)
            if meanwhile is not None:
                await meanwhile()
            raise exc
        return await real(node_id, **kwargs)

    store.transition_node = flaky


async def test_a_subtask_launch_that_could_not_be_recorded_is_launched_again(parts):
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    _fail_running_write(parts.store, _lock_timeout())

    await parts.orch.start_dag(dag.id)

    node = await _node(parts, dag.id)
    assert node.status == "pending"
    assert node.error is None
    assert await _subtask_statuses(parts) == ["cancelled"]  # what the launch created is stopped
    assert (await parts.store.get_dag(dag.id)).status == "running"

    await parts.orch.tick()

    node = await _node(parts, dag.id)
    assert node.status == "running"
    assert await _subtask_statuses(parts) == ["cancelled", "pending"]  # one new subtask, and only one
    assert (await parts.subtasks.get(node.subtask_id)).status == "pending"
    assert parts.orch._defer_counts == {}


@pytest.mark.postgres_only
async def test_a_check_launch_that_could_not_be_recorded_is_launched_again(parts):
    """Postgres only: one check per name is a constraint of the Postgres schema."""
    dag = await _one_node_dag(parts, DAGNodeType.check)
    name = f"dag-{dag.id.hex[:8]}-work"
    _fail_running_write(parts.store, _lock_timeout())

    await parts.orch.start_dag(dag.id)

    node = await _node(parts, dag.id)
    assert node.status == "pending"
    assert [(c["name"], c["enabled"]) for c in await _checks(parts)] == [(name, False)]

    await parts.orch.tick()

    node = await _node(parts, dag.id)
    assert (node.status, node.check_name) == ("running", name)
    assert [(c["name"], c["enabled"]) for c in await _checks(parts)] == [(name, True)]
    assert parts.loader._registry.get_check(name) is not None


@pytest.mark.postgres_only
async def test_a_check_that_could_not_be_disabled_is_replaced_by_the_next_launch(parts):
    """Postgres only: one check per name is a constraint of the Postgres schema."""
    dag = await _one_node_dag(parts, DAGNodeType.check)
    name = f"dag-{dag.id.hex[:8]}-work"
    _fail_running_write(parts.store, _lock_timeout())
    real_manage = parts.loader.manage_check

    async def disable_fails(action, name=None, **kwargs):
        if action == "disable":
            raise _lock_timeout()
        return await real_manage(action, name, **kwargs)

    parts.loader.manage_check = disable_fails

    await parts.orch.start_dag(dag.id)

    assert (await _node(parts, dag.id)).status == "pending"
    left = await _checks(parts)
    assert [(c["name"], c["enabled"]) for c in left] == [(name, True)]  # still on: the disable failed

    await parts.orch.tick()

    assert (await _node(parts, dag.id)).status == "running"
    now = await _checks(parts)
    assert [(c["name"], c["enabled"]) for c in now] == [(name, True)]
    assert now[0]["created_at"] != left[0]["created_at"]  # replaced, so only one of them runs


_CAN_PASS = [
    pytest.param(lambda: _lock_timeout(), id="lock-timeout"),
    pytest.param(lambda: _db_error(DBAPIError, "statement timeout", "57014"), id="statement-timeout"),
    pytest.param(lambda: _db_error(DBAPIError, "deadlock detected", "40P01"), id="deadlock"),
    pytest.param(lambda: _db_error(DBAPIError, "sorry, too many clients already", "53300"), id="too-many-connections"),
    pytest.param(lambda: _db_error(DBAPIError, "terminating connection", "57P01"), id="server-shutting-down"),
    pytest.param(lambda: _db_error(DBAPIError, "connection failure", "08006"), id="connection-failure"),
    pytest.param(lambda: _db_error(InterfaceError, "connection is closed"), id="connection-closed"),
    pytest.param(lambda: PoolTimeoutError("QueuePool limit of size 10 overflow 5 reached"), id="pool-exhausted"),
    pytest.param(lambda: ConnectionRefusedError("connection refused"), id="database-unreachable"),
]

_CANNOT_PASS = [
    pytest.param(lambda: _db_error(IntegrityError, "violates check constraint", "23514"), id="integrity"),
    pytest.param(lambda: _db_error(DBAPIError, "value too long for type character varying(200)", "22001"), id="data"),
    pytest.param(lambda: _db_error(ProgrammingError, "column does not exist", "42703"), id="programming"),
    pytest.param(lambda: _db_error(DBAPIError, "raised by a trigger", "P0001"), id="raised-by-the-server"),
    pytest.param(lambda: RuntimeError("a bug in the launch"), id="not-a-database-error"),
]


@pytest.mark.parametrize("make_error", _CAN_PASS)
async def test_an_error_that_can_pass_leaves_the_node_launchable(parts, make_error):
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    _fail_running_write(parts.store, make_error())

    await parts.orch.start_dag(dag.id)

    assert (await _node(parts, dag.id)).status == "pending"
    assert await _subtask_statuses(parts) == ["cancelled"]


@pytest.mark.parametrize("make_error", _CANNOT_PASS)
async def test_an_error_that_cannot_pass_still_fails_the_node(parts, make_error):
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    error = make_error()
    _fail_running_write(parts.store, error)

    await parts.orch.start_dag(dag.id)

    node = await _node(parts, dag.id)
    assert (node.status, node.error) == ("failed", str(error))
    assert await _subtask_statuses(parts) == ["cancelled"]
    assert parts.orch._defer_counts == {}


@pytest.mark.postgres_only
async def test_a_real_data_error_from_the_server_still_fails_the_node(db, parts):
    """The driver raises a class-22 error as a plain DBAPIError, not a
    DataError: its SQLSTATE is what says it will not pass."""
    async with db.engine.connect() as conn:
        with pytest.raises(DBAPIError) as caught:
            await conn.execute(text("SELECT 1 / 0"))
    assert caught.value.orig.sqlstate == "22012"
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    _fail_running_write(parts.store, caught.value)

    await parts.orch.start_dag(dag.id)

    node = await _node(parts, dag.id)
    assert node.status == "failed"
    assert "division by zero" in node.error


async def test_a_check_launch_error_that_cannot_pass_still_fails_the_node(parts):
    dag = await _one_node_dag(parts, DAGNodeType.check)
    error = _db_error(IntegrityError, "violates check constraint", "23514")
    _fail_running_write(parts.store, error)

    await parts.orch.start_dag(dag.id)

    node = await _node(parts, dag.id)
    assert (node.status, node.error) == ("failed", str(error))
    assert [c["enabled"] for c in await _checks(parts)] == [False]
    assert parts.orch._defer_counts == {}


async def test_an_error_that_never_passes_ends_as_a_visible_failure(parts):
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    parts.orch._MAX_DEFERRALS = 3
    _fail_running_write(parts.store, _lock_timeout(), times=99)

    await parts.orch.start_dag(dag.id)
    await parts.orch.tick()
    assert (await _node(parts, dag.id)).status == "pending"
    await parts.orch.tick()

    node = await _node(parts, dag.id)
    assert node.status == "failed"
    assert "lock timeout" in node.error and "after 3 deferrals" in node.error
    assert await _subtask_statuses(parts) == ["cancelled"] * 3  # nothing it created is left running


async def test_a_write_that_landed_before_raising_keeps_the_node_running(parts):
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    _fail_running_write(parts.store, _lock_timeout(), after_commit=True)

    await parts.orch.start_dag(dag.id)

    assert (await _node(parts, dag.id)).status == "running"
    assert await _subtask_statuses(parts) == ["pending"]
    assert parts.orch._defer_counts == {}


@pytest.mark.parametrize("when", ["while-the-write-waits", "finished-meanwhile", "just-before-the-cancel"])
async def test_a_subtask_a_worker_took_is_not_launched_a_second_time(parts, when):
    """A worker took the subtask before the cancel landed, and ran it or is
    running it: the work has run once, so the node fails as before. The row
    is read after the cancel has committed; that is what sees the last case."""
    dag = await _one_node_dag(parts, DAGNodeType.subtask)

    async def a_worker_takes_it():
        subtask = await parts.subtasks.dequeue("worker-1")
        if when == "finished-meanwhile":
            await parts.subtasks.complete(subtask.id, "the work, done")

    if when == "just-before-the-cancel":
        real_cancel = parts.subtasks.cancel

        async def cancel_after_a_worker_took_it(subtask_id):
            await a_worker_takes_it()
            return await real_cancel(subtask_id)

        parts.subtasks.cancel = cancel_after_a_worker_took_it
        _fail_running_write(parts.store, _lock_timeout())
    else:
        _fail_running_write(parts.store, _lock_timeout(), meanwhile=a_worker_takes_it)

    await parts.orch.start_dag(dag.id)
    await parts.orch.tick()

    assert (await _node(parts, dag.id)).status == "failed"
    assert await _subtask_statuses(parts) == ["completed" if when == "finished-meanwhile" else "cancelled"]


async def test_a_subtask_that_could_not_be_stopped_is_not_launched_a_second_time(parts):
    """The first subtask may still run: a second one next to it would do the work twice."""
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    _fail_running_write(parts.store, _lock_timeout())

    async def cancel_fails(_subtask_id):
        raise _lock_timeout()

    parts.subtasks.cancel = cancel_fails

    await parts.orch.start_dag(dag.id)
    await parts.orch.tick()

    assert (await _node(parts, dag.id)).status == "failed"
    assert await _subtask_statuses(parts) == ["pending"]


@pytest.mark.postgres_only
async def test_a_real_lock_timeout_on_a_subtask_launch_is_launched_again(db):
    """Another session holds the node's row when the launch writes `running`,
    and lets go once that write has timed out."""
    database = _LockTimeoutDatabase(Settings())
    try:
        p = _parts(database)
        dag = await _one_node_dag(p, DAGNodeType.subtask)
        node_id = (await _node(p, dag.id)).id
        async with db.engine.connect() as holder:
            real_create, real_write = p.subtasks.create, p.store.transition_node

            async def create_then_lock(**kwargs):
                subtask = await real_create(**kwargs)
                await holder.execute(_LOCK_NODE, {"id": node_id})
                return subtask

            async def release_when_it_times_out(node_id_, **kwargs):
                try:
                    return await real_write(node_id_, **kwargs)
                except DBAPIError:
                    await holder.rollback()
                    raise

            p.subtasks.create = create_then_lock
            p.store.transition_node = release_when_it_times_out

            await p.orch.start_dag(dag.id)

            assert (await _node(p, dag.id)).status == "pending"
            assert await _subtask_statuses(p) == ["cancelled"]
            p.subtasks.create = real_create

            await p.orch.tick()

        assert (await _node(p, dag.id)).status == "running"
        assert await _subtask_statuses(p) == ["cancelled", "pending"]
    finally:
        await database.disconnect()


# ---------------------------------------------------------------------------
# A subtask that could not be stopped is stopped by the node's next launch
# ---------------------------------------------------------------------------


async def _launch_while_the_database_is_away(p, launch) -> None:
    """The `running` write, the cancel and the failure write all fail, as they
    do while the database is away for a moment, during `launch`."""
    real_write, real_cancel = p.store.transition_node, p.subtasks.cancel

    async def away(node_id, **kwargs):
        if kwargs.get("status") in ("running", "failed"):
            raise _lock_timeout()
        return await real_write(node_id, **kwargs)

    async def cancel_away(_subtask_id):
        raise _lock_timeout()

    p.store.transition_node, p.subtasks.cancel = away, cancel_away
    try:
        await launch()
    finally:
        p.store.transition_node, p.subtasks.cancel = real_write, real_cancel


async def test_a_subtask_whose_stop_failed_is_stopped_by_the_next_launch(parts, monkeypatch):
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    await _launch_while_the_database_is_away(parts, lambda: parts.orch.start_dag(dag.id))
    assert (await _node(parts, dag.id)).status == "ready"  # the failure write failed too
    assert await _subtask_statuses(parts) == ["pending"]  # the first subtask, still queued

    monkeypatch.setattr(orchestrator_module, "_STALE_READY_GRACE_SECONDS", 0)
    await parts.orch.tick()  # the stale-ready sweep hands the node back

    node = await _node(parts, dag.id)
    assert node.status == "running"
    assert await _subtask_statuses(parts) == ["cancelled", "pending"]  # the first stopped, one new
    assert (await parts.subtasks.get(node.subtask_id)).status == "pending"


async def test_a_stop_that_fails_again_is_tried_again_at_the_launch_after(parts, monkeypatch):
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    await _launch_while_the_database_is_away(parts, lambda: parts.orch.start_dag(dag.id))
    monkeypatch.setattr(orchestrator_module, "_STALE_READY_GRACE_SECONDS", 0)

    await _launch_while_the_database_is_away(parts, parts.orch.tick)  # still away at the next launch

    assert (await _node(parts, dag.id)).status == "ready"
    assert await _subtask_statuses(parts) == ["pending"]  # nothing new beside the first

    await parts.orch.tick()

    assert (await _node(parts, dag.id)).status == "running"
    assert await _subtask_statuses(parts) == ["cancelled", "pending"]


async def test_a_leftover_subtask_a_worker_took_ends_the_node(parts, monkeypatch):
    """Between the two launches a worker took the subtask the first one could
    not stop: its work runs, so the next launch fails the node instead of
    starting the work a second time. A retry after that starts it afresh."""
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    await _launch_while_the_database_is_away(parts, lambda: parts.orch.start_dag(dag.id))
    await parts.subtasks.dequeue("worker-1")

    monkeypatch.setattr(orchestrator_module, "_STALE_READY_GRACE_SECONDS", 0)
    await parts.orch.tick()

    node = await _node(parts, dag.id)
    assert node.status == "failed"
    assert "earlier launch" in node.error
    assert await _subtask_statuses(parts) == ["cancelled"]  # the one a worker took, and no other

    await parts.orch.retry_node(dag.id, "work")
    await parts.orch.tick()

    assert (await _node(parts, dag.id)).status == "running"
    assert await _subtask_statuses(parts) == ["cancelled", "pending"]


async def test_a_cancelled_node_stops_the_subtask_its_launch_could_not_stop(parts):
    """A cancel_dag, a budget cancel and a cancel_cascade end a node through
    _cancel_node, which stops a kept subtask too, so it does not run inside a
    cancelled DAG once the database is back."""
    dag = await _one_node_dag(parts, DAGNodeType.subtask)
    await _launch_while_the_database_is_away(parts, lambda: parts.orch.start_dag(dag.id))
    assert await _subtask_statuses(parts) == ["pending"]

    await parts.orch.cancel_dag(dag.id)

    assert (await _node(parts, dag.id)).status == "cancelled"
    assert await _subtask_statuses(parts) == ["cancelled"]
    assert parts.orch._unstopped_subtasks == {}


@pytest.mark.postgres_only
async def test_a_relaunch_whose_leftover_check_cannot_be_deleted_fails_the_node(parts):
    """Postgres only: one check per name is a constraint of the Postgres schema.
    The create step, the delete of a leftover check included, is outside the
    requeue: an error there fails the node, as a failed create always has."""
    dag = await _one_node_dag(parts, DAGNodeType.check)
    _fail_running_write(parts.store, _lock_timeout())
    await parts.orch.start_dag(dag.id)
    assert (await _node(parts, dag.id)).status == "pending"
    real_manage = parts.loader.manage_check

    async def delete_fails(action, name=None, **kwargs):
        if action == "delete":
            raise _lock_timeout()
        return await real_manage(action, name, **kwargs)

    parts.loader.manage_check = delete_fails

    await parts.orch.tick()

    node = await _node(parts, dag.id)
    assert node.status == "failed"
    assert "lock timeout" in node.error
