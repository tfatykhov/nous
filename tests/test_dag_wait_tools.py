"""Typed waits for lineage DAGs through dag_create: the wait node in the schema and the handler, and the refusal of
shell completion_checks in a DAG a background turn creates."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.brain.intentions import IntentionSpec
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.storage.models import ExecutionDAG, Intention

TYPED = "python -m nous.dag.typed_checks pr o/r --pr 5"
FILE_WAIT = {"name": "w", "type": "wait", "instructions": "", "wait_for": {"kind": "file", "path": "x.md"}}
GO = {"name": "n", "type": "subtask", "instructions": "go"}
THEN = [{"from_node": "w", "to_node": "n"}]


def _settings(**over) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(over)
    return Settings(**base)


@pytest_asyncio.fixture
async def agent(db):
    agent = f"test-wait-tools-{uuid.uuid4().hex[:8]}"
    yield agent
    async with db.session() as s:
        await s.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
        await s.execute(delete(Intention).where(Intention.agent_id == agent))
        await s.commit()


def _tools(db, agent, settings) -> SimpleNamespace:
    store = DAGStore(db, agent, settings)
    subtasks = AsyncMock()
    subtasks.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    orch = DAGOrchestrator(store=store, subtask_mgr=subtasks, dynamic_loader=AsyncMock(), settings=settings)
    orch.clock_wired = True
    dispatcher = ToolDispatcher()
    register_dag_tools(dispatcher, store, orch, settings=settings)
    return SimpleNamespace(store=store, create=dispatcher._handlers["dag_create"], schemas=dispatcher._schemas)


def _text(result) -> str:
    return result["content"][0]["text"]


def _is_error(result) -> bool:
    return bool(result.get("is_error"))


def _strings(value) -> str:
    """Every string in a schema, joined: what the model reads."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_strings(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_strings(v) for v in value)
    return ""


async def _dag_count(db, agent) -> int:
    async with db.session() as s:
        return await s.scalar(select(func.count()).select_from(ExecutionDAG).where(ExecutionDAG.agent_id == agent))


def test_the_schema_teaches_the_wait_node():
    t = _tools(None, "a", _settings())
    item = t.schemas["dag_create"]["properties"]["nodes"]["items"]
    assert "wait" in item["properties"]["type"]["enum"]
    assert "wait_for" in item["properties"]
    assert item["required"] == ["name", "type", "instructions"]
    text = _strings(t.schemas["dag_create"])
    assert "not a 'check' node" in text
    for example in ('"kind": "job"', '"kind": "pr"', '"kind": "file"'):
        assert example in text
    for until in ("merged", "closed", "head_changed", "checks_done", "review_on_head"):
        assert until in text
    # A repository without CI never lists a check run: checks_done does not pass vacuously.
    assert "pending while GitHub lists no check run on the head" in text


@pytest.mark.postgres_only
async def test_a_wait_node_is_created_through_the_tool(db, agent):
    t = _tools(db, agent, _settings())
    result = await t.create(name="w", nodes=[FILE_WAIT, GO], edges=THEN)
    assert not _is_error(result), _text(result)
    dag = (await t.store.get_active_dags())[0]
    w = next(n for n in dag.nodes if n.name == "w")
    assert (w.node_type, w.wait_for) == ("wait", {"kind": "file", "path": "x.md", "nonempty": False})


@pytest.mark.postgres_only
async def test_wait_for_is_not_silently_dropped(db, agent):
    t = _tools(db, agent, _settings())
    missing = await t.create(name="w", nodes=[{**FILE_WAIT, "wait_for": None}])
    assert _is_error(missing) and "wait_for" in _text(missing)
    on_a_subtask = await t.create(name="s", nodes=[{**GO, "wait_for": {"kind": "file", "path": "x"}}])
    assert _is_error(on_a_subtask) and "wait_for is allowed only on wait nodes" in _text(on_a_subtask)
    assert await _dag_count(db, agent) == 0


@pytest.mark.postgres_only
async def test_the_poll_floor_and_timeout_notes_reach_the_result(db, agent):
    t = _tools(db, agent, _settings())
    wait = {
        "name": "w",
        "type": "wait",
        "instructions": "",
        "wait_for": {"kind": "pr", "number": 7, "until": "merged", "repo": "o/r"},
        "completion_check_interval": 10,
        "max_check_attempts": 5,
    }
    result = await t.create(name="pr", nodes=[wait])
    assert not _is_error(result), _text(result)
    notes = [line for line in _text(result).splitlines() if line.startswith("Note:")]
    assert any("poll interval raised from 10 to 60" in n for n in notes)
    assert any("timeout_seconds raised from 120 to 360" in n for n in notes)


@pytest.mark.postgres_only
async def test_a_too_long_poll_budget_is_refused(db, agent):
    t = _tools(db, agent, _settings())
    wait = {**FILE_WAIT, "completion_check_interval": 600, "max_check_attempts": 10}
    result = await t.create(name="long", nodes=[wait])
    assert _is_error(result)
    assert "6600" in _text(result) and "3600" in _text(result)
    assert await _dag_count(db, agent) == 0


def _check(cmd: str) -> dict:
    return {"name": "c", "type": "check", "instructions": "watch", "completion_check": cmd}


async def _root(t) -> str:
    """An owner chat turn's root intention, for a continuation turn to spawn under."""
    root = await t.store.create(
        DAGCreateRequest(name="root", nodes=[DAGNodeSpec(name="r", type=DAGNodeType.subtask, instructions="go")]),
        intention=IntentionSpec(intent="root", origin_kind="interactive"),
    )
    return (await t.store.intention_lineage(root.id))["id"]


def _continuation(root_id: str) -> dict:
    return {
        "_origin_kind": "continuation",
        "_origin_authority": "internal_only",
        "_intention_id": root_id,
        "intent": "wait for the job, then report",
    }


@pytest.mark.postgres_only
async def test_an_internal_only_turn_cannot_create_a_dag_with_a_shell_check(db, agent, caplog):
    t = _tools(db, agent, _settings(intentions_enabled=True, result_inbox_enabled=True))
    root_id = await _root(t)
    with caplog.at_level(logging.ERROR, logger="nous.api.tools"):
        result = await t.create(name="lin", nodes=[_check("true")], **_continuation(root_id))
    assert _is_error(result)
    for word in ("'c'", "wait", "job", "pr", "file"):
        assert word in _text(result)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert await _dag_count(db, agent) == 1  # the root only

    waits = await t.create(name="lin", nodes=[FILE_WAIT, GO], edges=THEN, **_continuation(root_id))
    assert not _is_error(waits), _text(waits)
    typed = await t.create(name="lin-pr", nodes=[{**GO, "completion_check": TYPED}], **_continuation(root_id))
    assert not _is_error(typed), _text(typed)
    async with db.session() as s:
        lineage = await s.scalar(
            select(func.count())
            .select_from(Intention)
            .where(Intention.agent_id == agent, Intention.authority == "internal_only")
        )
    assert lineage == 2  # both DAGs are lineage DAGs, so the store's check saw them too
    assert await _dag_count(db, agent) == 3


@pytest.mark.postgres_only
async def test_the_tool_refuses_even_with_intentions_off(db, agent):
    t = _tools(db, agent, _settings(intentions_enabled=False))
    result = await t.create(name="lin", nodes=[_check("true")], _origin_authority="internal_only")
    assert _is_error(result) and _text(result).startswith("Error creating DAG: Refused:")
    assert await _dag_count(db, agent) == 0


@pytest.mark.postgres_only
async def test_a_dag_narrowed_by_its_parent_is_refused_cleanly(db, agent, caplog):
    """The turn says owner, but its parent intention is internal_only: the store narrows and refuses."""
    t = _tools(db, agent, _settings(intentions_enabled=True, result_inbox_enabled=True))
    parent = await t.store.create(
        DAGCreateRequest(name="p", nodes=[DAGNodeSpec(name="r", type=DAGNodeType.subtask, instructions="go")]),
        intention=IntentionSpec(intent="p", origin_kind="continuation", origin_authority="internal_only"),
    )
    parent_id = (await t.store.intention_lineage(parent.id))["id"]
    with caplog.at_level(logging.ERROR, logger="nous.api.tools"):
        result = await t.create(
            name="child", nodes=[_check("true")], _origin_kind="interactive", _intention_id=parent_id, intent="x"
        )
    assert _is_error(result) and "Refused:" in _text(result)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert await _dag_count(db, agent) == 1


@pytest.mark.postgres_only
@pytest.mark.parametrize("authority", ["owner", None])
async def test_an_owner_chat_turn_keeps_its_shell_check(db, agent, authority):
    t = _tools(db, agent, _settings(intentions_enabled=True, result_inbox_enabled=True))
    origin = {"_origin_kind": "interactive", "intent": "watch it"}
    if authority is not None:
        origin["_origin_authority"] = authority
    result = await t.create(name="own", nodes=[_check("true")], **origin)
    assert not _is_error(result), _text(result)
