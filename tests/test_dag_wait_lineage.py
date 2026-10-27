"""Typed waits for lineage DAGs: a DAG created by a background turn may wait only through typed conditions.

A lineage (internal_only) DAG may carry no shell completion_check except the exact built-in PR check, which the
orchestrator runs without a shell. Refused at create (store and tool) and, for rows written another way, at launch and
at poll.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.api.execution_context import lineage_from_stamp
from nous.brain.intentions import AUTHORITY_INTERNAL, IntentionSpec, lineage_stamp, stamp_is_internal
from nous.config import Settings
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGNodeSpec,
    DAGNodeType,
    LineageShellCheckRefused,
    lineage_refuses_check,
    lineage_shell_check_message,
)
from nous.dag.store import DAGStore
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
