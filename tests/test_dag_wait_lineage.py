"""Typed waits for lineage DAGs: a DAG created by a background turn may wait only through typed conditions.

A lineage (internal_only) DAG may carry no shell completion_check except the exact built-in PR check, which the
orchestrator runs without a shell. Refused at create (store and tool) and, for rows written another way, at launch and
at poll.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from nous.api.execution_context import lineage_from_stamp
from nous.brain.intentions import AUTHORITY_INTERNAL, IntentionSpec, lineage_stamp, stamp_is_internal
from nous.config import Settings
from nous.dag import orchestrator as orchestrator_module
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGNodeSpec,
    DAGNodeType,
    LineageShellCheckRefused,
    lineage_refuses_check,
    lineage_shell_check_message,
)
from nous.dag.store import DAGStore
from nous.observability.task_scoring import node_check_state, parse_pr_check
from nous.storage.models import ExecutionDAG, Intention

TYPED = "python -m nous.dag.typed_checks pr o/r --pr 5"
_A, _B = uuid.uuid4(), uuid.uuid4()


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        (None, False),
        ("junk", True),
        ({}, True),
        ({"id": "x", "root_id": "y", "authority": "internal_only"}, True),
        (lineage_stamp(_A, _B, "owner"), False),
        (lineage_stamp(_A, _B, "internal_only"), True),
        ({"id": str(_A), "root_id": str(_B), "authority": "root"}, True),
        ({"id": str(_A), "authority": "owner"}, True),
    ],
)
def test_stamp_is_internal_agrees_with_lineage_from_stamp(stamp, expected):
    assert stamp_is_internal(stamp) is expected
    assert stamp_is_internal(stamp) == (lineage_from_stamp(stamp)[2] == AUTHORITY_INTERNAL)


@pytest.mark.parametrize(
    ("cmd", "refused"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("true", True),
        ("test -f out.md", True),
        (TYPED, False),
        (f"  {TYPED}  ", False),
        ("python -m nous.dag.typed_checks pr o/r --head feat/x --job-file /tmp/nous-workspace/j.id", False),
        (f"{TYPED} || true", True),
        (f"{TYPED}; rm -rf x", True),
        (f"{TYPED} --extra", True),
        ("python -m nous.dag.typed_checks pr ../x --pr 5", True),
        ("python -m nous.dag.typed_checks pr o/r --pr 5\nrm x", True),
    ],
)
def test_only_the_built_in_pr_check_is_a_check_a_lineage_may_run(cmd, refused):
    assert lineage_refuses_check(cmd) is refused


def test_the_refusal_names_the_nodes_and_the_typed_waits():
    text = lineage_shell_check_message(["a", "b"])
    assert text.startswith("Refused:")  # never "Completion check ...": the scorer reads that prefix as a failed check
    for word in ("'a'", "'b'", "wait", "job", "pr", "file", "python -m nous.dag.typed_checks pr"):
        assert word in text
    assert "\n" not in text


# ---------------------------------------------------------------------------
# Create time (Postgres)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def env(db):
    agent = f"test-wait-lin-{uuid.uuid4().hex[:8]}"
    settings = Settings(_env_file=None, agent_id=agent)
    store = DAGStore(db, agent, settings)
    yield SimpleNamespace(agent=agent, db=db, store=store, settings=settings)
    async with db.session() as s:
        await s.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
        await s.execute(delete(Intention).where(Intention.agent_id == agent))
        await s.commit()


def _request(*nodes: DAGNodeSpec, name: str = "lin") -> DAGCreateRequest:
    return DAGCreateRequest(name=name, nodes=list(nodes))


def _checked(node_type: DAGNodeType, cmd: str, name: str = "c") -> DAGNodeSpec:
    return DAGNodeSpec(name=name, type=node_type, instructions="work", completion_check=cmd)


def _internal(**over) -> IntentionSpec:
    return IntentionSpec(intent="x", origin_kind="continuation", origin_authority="internal_only", **over)


async def _dag_names(env) -> list[str]:
    async with env.db.session() as s:
        return list((await s.execute(select(ExecutionDAG.name).where(ExecutionDAG.agent_id == env.agent))).scalars())


@pytest.mark.postgres_only
@pytest.mark.parametrize("node_type", [DAGNodeType.check, DAGNodeType.subtask])
async def test_create_refuses_a_shell_check_in_an_internal_only_dag(env, node_type):
    with pytest.raises(LineageShellCheckRefused) as exc:
        await env.store.create(_request(_checked(node_type, "true")), intention=_internal())
    text = str(exc.value)
    assert text.startswith("Refused:") and "'c'" in text
    for word in ("wait", "job", "pr", "file"):
        assert word in text
    assert await _dag_names(env) == []


@pytest.mark.postgres_only
async def test_create_keeps_the_built_in_pr_check_in_an_internal_only_dag(env):
    dag = await env.store.create(_request(_checked(DAGNodeType.subtask, TYPED)), intention=_internal())
    assert dag.nodes[0].completion_check == TYPED


@pytest.mark.postgres_only
async def test_a_blank_check_is_no_shell_check(env):
    dag = await env.store.create(_request(_checked(DAGNodeType.subtask, "  ")), intention=_internal())
    assert dag.name == "lin"


@pytest.mark.postgres_only
async def test_an_internal_only_dag_of_waits_is_created(env):
    dag = await env.store.create(
        _request(
            DAGNodeSpec(name="w", type=DAGNodeType.wait, wait_for={"kind": "file", "path": "x"}),
            DAGNodeSpec(name="s", type=DAGNodeType.subtask, instructions="go"),
        ),
        intention=_internal(),
    )
    assert {n.name for n in dag.nodes} == {"w", "s"}


@pytest.mark.postgres_only
async def test_a_child_is_refused_only_when_its_turn_is_internal(env):
    root = await env.store.create(
        _request(DAGNodeSpec(name="r", type=DAGNodeType.subtask, instructions="go"), name="root"),
        intention=IntentionSpec(intent="root", origin_kind="interactive"),
    )
    root_id = uuid.UUID((await env.store.intention_lineage(root.id))["id"])
    owner_child = await env.store.create(
        _request(_checked(DAGNodeType.check, "true"), name="owner-child"),
        intention=IntentionSpec(intent="child", origin_kind="interactive", parent_id=root_id),
    )
    assert owner_child.nodes[0].completion_check == "true"
    with pytest.raises(LineageShellCheckRefused):
        await env.store.create(
            _request(_checked(DAGNodeType.check, "true"), name="internal-child"),
            intention=_internal(parent_id=root_id),
        )
    assert sorted(await _dag_names(env)) == ["owner-child", "root"]


@pytest.mark.postgres_only
async def test_an_owner_dag_keeps_its_shell_check(env):
    dag = await env.store.create(
        _request(_checked(DAGNodeType.check, "true")), intention=IntentionSpec(intent="x", origin_kind="interactive")
    )
    assert dag.nodes[0].completion_check == "true"


@pytest.mark.postgres_only
async def test_a_dag_without_an_intention_keeps_its_shell_check(env):
    dag = await env.store.create(_request(_checked(DAGNodeType.check, "true")))
    assert dag.nodes[0].completion_check == "true"


# ---------------------------------------------------------------------------
# Run time (Postgres): rows written before this rule, or another way
# ---------------------------------------------------------------------------

# What the built-in PR check may see of the orchestrator's environment.
TYPED_CHECK_ENV = {
    "PATH",
    "GITHUB_TOKEN",
    "NOUS_WORKSPACE_DIR",
    "NOUS_DAG_CREATED_AT",
    "PYTHONPATH",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
}


class _Proc:
    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self.pid = 4242
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


class Spawns:
    """Spies on both ways the orchestrator could start a process."""

    def __init__(self, monkeypatch, *, returncode: int = 0, shell_allowed: bool = False):
        self.shell: list[tuple] = []
        self.exec: list[tuple] = []
        self.returncode = returncode
        self.shell_allowed = shell_allowed
        monkeypatch.setattr(orchestrator_module.asyncio, "create_subprocess_shell", self._shell)
        monkeypatch.setattr(orchestrator_module.asyncio, "create_subprocess_exec", self._exec)

    async def _shell(self, *args, **kwargs):
        self.shell.append((args, kwargs))
        if not self.shell_allowed:
            raise AssertionError("shell spawned")
        return _Proc(self.returncode)

    async def _exec(self, *args, **kwargs):
        self.exec.append((args, kwargs))
        return _Proc(self.returncode, b"PR #5 was closed without a merge")


@pytest_asyncio.fixture
async def run_env(db, tmp_path):
    agent = f"test-wait-run-{uuid.uuid4().hex[:8]}"
    settings = Settings(
        _env_file=None,
        agent_id=agent,
        workspace_dir=str(tmp_path / "ws"),
        dag_workspace_root=tmp_path / "dagws",
    ).model_copy(update={"github_token": "t0ken"})  # read from GITHUB_TOKEN, not a NOUS_ name
    (tmp_path / "ws").mkdir()
    store = DAGStore(db, agent, settings)
    subtask_mgr = AsyncMock()
    subtask_mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    loader = AsyncMock()
    loader._registry = None
    orch = DAGOrchestrator(store=store, subtask_mgr=subtask_mgr, dynamic_loader=loader, settings=settings)
    orch.clock_wired = True
    yield SimpleNamespace(
        agent=agent, db=db, store=store, orch=orch, subtask_mgr=subtask_mgr, loader=loader, workspace=tmp_path / "ws"
    )
    async with db.session() as s:
        await s.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
        await s.execute(delete(Intention).where(Intention.agent_id == agent))
        await s.commit()


async def _owner_dag(env, *nodes: DAGNodeSpec, internal_now: bool):
    """A DAG created in an owner turn; ``internal_now`` turns its intention internal_only afterwards, as a row from
    before this rule (or a direct write) would be."""
    dag = await env.store.create(_request(*nodes), intention=IntentionSpec(intent="x", origin_kind="interactive"))
    if internal_now:
        stamp = await env.store.intention_lineage(dag.id)
        async with env.db.session() as s:
            await s.execute(
                update(Intention).where(Intention.id == uuid.UUID(stamp["id"])).values(authority="internal_only")
            )
            await s.commit()
    return dag


async def _parked_check(env, node_type: DAGNodeType, cmd: str, *, internal_now: bool):
    dag = await _owner_dag(env, _checked(node_type, cmd), internal_now=internal_now)
    now = datetime.now(UTC)
    await env.store.update_dag_status(dag.id, "running")
    await env.store.update_node(dag.nodes[0].id, status="awaiting_check", started_at=now, awaiting_check_at=now)
    return dag.id


async def _node(env, dag_id):
    return (await env.store.get_dag(dag_id)).nodes[0]


def _scorer_state(row) -> str | None:
    return node_check_state(row.completion_check, row.check_attempts, row.last_check_at, row.status, row.error)


@pytest.mark.postgres_only
@pytest.mark.parametrize("node_type", [DAGNodeType.check, DAGNodeType.subtask])
@pytest.mark.parametrize("cmd", ["true", f"{TYPED} || true"])
async def test_a_lineage_dag_from_before_the_rule_fails_its_shell_check_and_spawns_nothing(
    run_env, monkeypatch, node_type, cmd
):
    spawns = Spawns(monkeypatch)
    dag_id = await _parked_check(run_env, node_type, cmd, internal_now=True)
    await run_env.orch.tick()
    row = await _node(run_env, dag_id)
    assert row.status == "failed"
    assert row.error == lineage_shell_check_message(["c"])
    assert (spawns.shell, spawns.exec) == ([], [])
    assert row.check_attempts == 0
    assert _scorer_state(row) is None  # a refusal is no check that ran and failed


@pytest.mark.postgres_only
async def test_the_built_in_pr_check_runs_without_a_shell_in_a_lineage_dag(run_env, monkeypatch):
    spawns = Spawns(monkeypatch, returncode=0)
    dag = await run_env.store.create(_request(_checked(DAGNodeType.subtask, TYPED)), intention=_internal())
    now = datetime.now(UTC)
    await run_env.store.update_dag_status(dag.id, "running")
    await run_env.store.update_node(dag.nodes[0].id, status="awaiting_check", started_at=now, awaiting_check_at=now)
    await run_env.orch.tick()

    assert spawns.shell == []
    [(argv, kwargs)] = spawns.exec
    assert list(argv) == [sys.executable, "-P", "-m", "nous.dag.typed_checks", "pr", "o/r", "--pr", "5"]
    env = kwargs["env"]
    assert set(env) <= TYPED_CHECK_ENV
    assert env["GITHUB_TOKEN"] == "t0ken"
    assert env["NOUS_WORKSPACE_DIR"] == str(run_env.workspace)
    assert "NOUS_DAG_CREATED_AT" in env and "PATH" in env
    assert kwargs["start_new_session"] is True
    row = await _node(run_env, dag.id)
    assert row.status == "completed"
    # What the task scorer reads: the same columns as before, so it credits the node as it did.
    assert _scorer_state(row) == "passed"
    assert parse_pr_check(row.completion_check) is not None


@pytest.mark.postgres_only
async def test_a_failing_built_in_pr_check_fails_as_a_check(run_env, monkeypatch):
    Spawns(monkeypatch, returncode=1)
    dag = await run_env.store.create(_request(_checked(DAGNodeType.subtask, TYPED)), intention=_internal())
    now = datetime.now(UTC)
    await run_env.store.update_dag_status(dag.id, "running")
    await run_env.store.update_node(dag.nodes[0].id, status="awaiting_check", started_at=now, awaiting_check_at=now)
    await run_env.orch.tick()
    row = await _node(run_env, dag.id)
    assert row.status == "failed"
    assert row.error.startswith("Completion check failed")
    assert _scorer_state(row) == "failed"


@pytest.mark.postgres_only
async def test_an_owner_dag_keeps_its_built_in_pr_check_in_a_shell(run_env, monkeypatch):
    spawns = Spawns(monkeypatch, returncode=0, shell_allowed=True)
    dag_id = await _parked_check(run_env, DAGNodeType.subtask, TYPED, internal_now=False)
    await run_env.orch.tick()
    assert (len(spawns.shell), spawns.exec) == (1, [])
    assert (await _node(run_env, dag_id)).status == "completed"


@pytest.mark.postgres_only
@pytest.mark.parametrize("node_type", [DAGNodeType.check, DAGNodeType.subtask])
async def test_launch_refuses_before_a_worker_exists(run_env, monkeypatch, node_type):
    spawns = Spawns(monkeypatch)
    dag = await _owner_dag(run_env, _checked(node_type, "true"), internal_now=True)
    await run_env.orch.start_dag(dag.id)
    row = await _node(run_env, dag.id)
    assert row.status == "failed"
    assert row.error == lineage_shell_check_message(["c"])
    run_env.loader.create_check.assert_not_called()
    run_env.subtask_mgr.create.assert_not_called()
    assert (spawns.shell, spawns.exec) == ([], [])


@pytest.mark.postgres_only
async def test_launch_keeps_the_built_in_pr_check_in_a_lineage_dag(run_env, monkeypatch):
    Spawns(monkeypatch)
    dag = await run_env.store.create(_request(_checked(DAGNodeType.subtask, TYPED)), intention=_internal())
    await run_env.orch.start_dag(dag.id)
    assert (await _node(run_env, dag.id)).status == "running"
    run_env.subtask_mgr.create.assert_called_once()


@pytest.mark.postgres_only
async def test_an_unreadable_lineage_does_not_run_the_shell(run_env, monkeypatch):
    spawns = Spawns(monkeypatch, returncode=0, shell_allowed=True)
    dag_id = await _parked_check(run_env, DAGNodeType.check, "true", internal_now=False)
    real = run_env.store.intention_lineage
    run_env.store.intention_lineage = AsyncMock(side_effect=RuntimeError("db down"))
    await run_env.orch.tick()
    row = await _node(run_env, dag_id)
    assert (row.status, row.check_attempts) == ("awaiting_check", 0)
    assert spawns.shell == []
    run_env.store.intention_lineage = real
    await run_env.orch.tick()
    assert len(spawns.shell) == 1
    assert (await _node(run_env, dag_id)).status == "completed"


@pytest.mark.postgres_only
async def test_the_lineage_is_read_once_per_dag_per_poll(run_env, monkeypatch):
    Spawns(monkeypatch, returncode=2, shell_allowed=True)  # exit 2: still running
    dag = await _owner_dag(
        run_env,
        _checked(DAGNodeType.check, "true", name="a"),
        _checked(DAGNodeType.check, "true", name="b"),
        internal_now=False,
    )
    now = datetime.now(UTC)
    await run_env.store.update_dag_status(dag.id, "running")
    for node in dag.nodes:
        await run_env.store.update_node(node.id, status="awaiting_check", started_at=now, awaiting_check_at=now)
    lookups = AsyncMock(side_effect=RuntimeError("db down"))
    run_env.store.intention_lineage = lookups
    await run_env.orch._poll_awaiting_checks(await run_env.store.get_dag(dag.id))
    assert lookups.await_count == 1  # a failed lookup is not retried for the second node


@pytest.mark.postgres_only
async def test_a_damaged_stamp_counts_as_lineage(run_env, monkeypatch):
    spawns = Spawns(monkeypatch)
    dag_id = await _parked_check(run_env, DAGNodeType.check, "true", internal_now=False)
    run_env.store.intention_lineage = AsyncMock(return_value="junk")
    await run_env.orch.tick()
    row = await _node(run_env, dag_id)
    assert row.status == "failed" and row.error.startswith("Refused:")
    assert spawns.shell == []


@pytest.mark.postgres_only
async def test_an_owner_dag_still_runs_its_shell_check(run_env, monkeypatch):
    spawns = Spawns(monkeypatch, returncode=0, shell_allowed=True)
    dag_id = await _parked_check(run_env, DAGNodeType.check, "true", internal_now=False)
    await run_env.orch.tick()
    assert [args[0] for args, _ in spawns.shell] == ["true"]
    assert (await _node(run_env, dag_id)).status == "completed"


@pytest.mark.postgres_only
async def test_a_wait_node_in_a_lineage_dag_runs(run_env, monkeypatch):
    spawns = Spawns(monkeypatch)
    (run_env.workspace / "done.md").write_text("x")
    dag = await run_env.store.create(
        _request(DAGNodeSpec(name="w", type=DAGNodeType.wait, wait_for={"kind": "file", "path": "done.md"})),
        intention=_internal(),
    )
    await run_env.orch.start_dag(dag.id)
    await run_env.orch.tick()
    row = await _node(run_env, dag.id)
    assert (row.status, row.result) == ("completed", "file done.md exists")
    assert (spawns.shell, spawns.exec) == ([], [])


@pytest.mark.postgres_only
async def test_the_check_runner_itself_never_runs_a_lineage_shell_command(run_env, monkeypatch):
    spawns = Spawns(monkeypatch)
    dag_id = await _parked_check(run_env, DAGNodeType.check, "true", internal_now=False)
    with pytest.raises(LineageShellCheckRefused):
        await run_env.orch._run_completion_check(await _node(run_env, dag_id), lineage_internal=True)
    assert (spawns.shell, spawns.exec) == ([], [])
