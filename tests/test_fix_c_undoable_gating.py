"""Post-merge review P2-7: `undoable` exists only where it can be honored.

#652 advertised `undoable` in the dag_create schema and stored it with every
flag off. A node that set it could then not write at all: the runner refuses
its non-compensable calls by policy and its compensable ones because nothing
is wired to snapshot them.

Each test drives dag_create on a real DAGStore, a real DAGOrchestrator and the
real subtask queue, and reads the flag back where the runner reads it: the
ExecutionContext built from the launched subtask row.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from nous.api.execution_context import ExecutionContext
from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.store import DAGStore
from nous.heart.subtasks import SubtaskManager


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


class _Wired:
    def __init__(self, db, settings: Settings) -> None:
        agent = f"test-fixc-undo-{uuid.uuid4().hex[:8]}"
        self.store = DAGStore(db, agent, settings)
        self.subtasks = SubtaskManager(db, agent)
        orch = DAGOrchestrator(
            store=self.store, subtask_mgr=self.subtasks, dynamic_loader=AsyncMock(), settings=settings
        )
        orch.clock_wired = True
        self.dispatcher = ToolDispatcher()
        register_dag_tools(self.dispatcher, self.store, orch, settings=settings)

    def node_schema(self) -> dict:
        return self.dispatcher._schemas["dag_create"]["properties"]["nodes"]["items"]["properties"]

    async def create_and_launch(self, **node_fields):
        """dag_create one subtask node; returns its row and the context its turn runs under."""
        node = {"name": "write", "type": "subtask", "instructions": "write the report", **node_fields}
        result = await self.dispatcher._handlers["dag_create"](name="report", nodes=[node])
        assert "Created DAG" in result["content"][0]["text"], result
        row = (await self.store.get_active_dags())[0].nodes[0]
        assert row.status == "running"
        subtask = await self.subtasks.get(row.subtask_id)
        return row, ExecutionContext.for_subtask(subtask, "session-1")


async def test_compensation_off_neither_advertises_nor_stores_undoable(db):
    wired = _Wired(db, _settings())

    assert "undoable" not in wired.node_schema()
    row, ctx = await wired.create_and_launch(undoable=True)
    assert row.undoable is False
    assert (ctx.kind, ctx.undoable) == ("dag_node", False)


async def test_compensation_on_carries_undoable_from_dag_create_to_the_turn(db):
    wired = _Wired(db, _settings(compensation_enabled=True))

    assert wired.node_schema()["undoable"]["type"] == "boolean"
    row, ctx = await wired.create_and_launch(undoable=True)
    assert row.undoable is True
    assert (ctx.kind, ctx.undoable) == ("dag_node", True)


@pytest.mark.parametrize("compensation", [False, True])
@pytest.mark.parametrize("empty", [None, "", 0, False, []])
async def test_an_empty_undoable_never_fails_the_request(db, compensation, empty):
    wired = _Wired(db, _settings(compensation_enabled=compensation))

    row, ctx = await wired.create_and_launch(undoable=empty)
    assert row.undoable is False
    assert ctx.undoable is False


async def test_a_string_false_is_not_read_as_true(db):
    """Guard: the value reaches pydantic as given -- bool("false") is True in Python."""
    wired = _Wired(db, _settings(compensation_enabled=True))

    row, ctx = await wired.create_and_launch(undoable="false")
    assert row.undoable is False
    assert ctx.undoable is False


@pytest.mark.parametrize(
    "proceed_defaults, rule",
    [
        (False, "Must be a 'stop' option."),
        (True, "Must be a 'stop' option unless every acting node downstream is declared undoable=true."),
    ],
    ids=["proceed_defaults_off", "proceed_defaults_on"],
)
async def test_the_default_option_help_says_what_the_validator_enforces(db, proceed_defaults, rule):
    """The same function's schema text for `default_option`: it named the stop
    rule alone even where the proceed-default flag allows the exception."""
    flags = {"dag_approval_nodes_enabled": True}  # approval nodes are advertised
    if proceed_defaults:
        flags.update(
            dag_approval_proceed_default_enabled=True, compensation_enabled=True, compensation_auto_review_enabled=True
        )
    wired = _Wired(db, _settings(**flags))

    description = wired.node_schema()["default_option"]["description"]
    assert description == f"(type='approval' only) option id applied at the deadline. {rule}"
