"""Tests for F038 DAG tool handlers (dag_create, dag_manage)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore
from nous.api.tools import ToolDispatcher, register_dag_tools


@pytest_asyncio.fixture
async def dag_store(db):
    """DAGStore instance with unique agent_id."""
    agent_id = f"test-dag-tools-{uuid.uuid4().hex[:8]}"
    return DAGStore(db, agent_id=agent_id, settings=Settings(_env_file=None))


@pytest_asyncio.fixture
async def dag_orchestrator(dag_store):
    """DAGOrchestrator with mocked subtask_mgr and dynamic_loader."""
    orchestrator = DAGOrchestrator(
        store=dag_store,
        subtask_mgr=AsyncMock(),
        dynamic_loader=AsyncMock(),
        settings=Settings(_env_file=None),
    )
    # F087: clock_wired is False until whoever installs the tick declares it,
    # and dag_create refuses while it is False. These tests exercise the tool
    # surface, not the wiring, so they stand in for a wired deployment.
    # tests/test_dag_wiring.py covers the unwired refusal directly.
    orchestrator.clock_wired = True
    return orchestrator


@pytest.fixture
def dispatcher():
    """Create a ToolDispatcher."""
    return ToolDispatcher()


@pytest.fixture
def tools(dispatcher, dag_store, dag_orchestrator):
    """Register DAG tools and return the dispatcher."""
    register_dag_tools(dispatcher, dag_store, dag_orchestrator)
    return dispatcher


def _simple_nodes():
    return [
        {"name": "task-1", "type": "subtask", "instructions": "Do thing one"},
    ]


def _two_node_args():
    return {
        "name": "test-dag",
        "nodes": [
            {"name": "fetch", "type": "subtask", "instructions": "Fetch data"},
            {"name": "process", "type": "subtask", "instructions": "Process data"},
        ],
        "edges": [
            {"from_node": "fetch", "to_node": "process"},
        ],
    }


# ------------------------------------------------------------------
# dag_create
# ------------------------------------------------------------------


class TestDagCreateTool:
    @pytest.mark.asyncio
    async def test_dag_create_basic(self, tools):
        """dag_create creates a DAG and returns wave summary."""
        handler = tools._handlers["dag_create"]
        result = await handler(
            name="my-dag",
            nodes=_simple_nodes(),
            edges=[],
        )
        assert result["content"][0]["type"] == "text"
        text = result["content"][0]["text"]
        assert "my-dag" in text
        assert "running" in text.lower()

    @pytest.mark.asyncio
    async def test_dag_create_with_edges(self, tools):
        """dag_create with dependency edges shows wave info."""
        handler = tools._handlers["dag_create"]
        result = await handler(**_two_node_args())
        text = result["content"][0]["text"]
        assert "Wave 0" in text
        assert "Wave 1" in text
        assert "2 nodes" in text

    @pytest.mark.asyncio
    async def test_dag_create_invalid_cycle(self, tools):
        """dag_create with cycle returns error message."""
        handler = tools._handlers["dag_create"]
        result = await handler(
            name="cycle-dag",
            nodes=[
                {"name": "a", "type": "subtask", "instructions": "A"},
                {"name": "b", "type": "subtask", "instructions": "B"},
            ],
            edges=[
                {"from_node": "a", "to_node": "b"},
                {"from_node": "b", "to_node": "a"},
            ],
        )
        text = result["content"][0]["text"]
        assert "Error" in text or "cycle" in text.lower()

    @pytest.mark.asyncio
    async def test_dag_create_mcp_format(self, tools):
        """dag_create returns proper MCP format."""
        handler = tools._handlers["dag_create"]
        result = await handler(
            name="format-test",
            nodes=_simple_nodes(),
            edges=[],
        )
        assert "content" in result
        assert isinstance(result["content"], list)
        assert result["content"][0]["type"] == "text"
        assert isinstance(result["content"][0]["text"], str)

    @pytest.mark.asyncio
    async def test_dag_create_authors_fix_node(self, tools, dag_store):
        """F066.1 (2026-05-23): dag_create must thread fix-node fields
        (parent_node, fix_actions, max_fix_attempts, expected_modes) into
        DAGNodeSpec. The historical handler dropped them silently — the
        DAG appeared to create cleanly but the fix node was unreachable
        because its required fields were lost at the tool surface.
        """
        handler = tools._handlers["dag_create"]
        result = await handler(
            name="fix-node-dag",
            nodes=[
                {"name": "fetch", "type": "subtask", "instructions": "Fetch data"},
                {
                    "name": "fix-fetch",
                    "type": "fix",
                    "instructions": "Recover from fetch failure",
                    "parent_node": "fetch",
                    "fix_actions": ["retry_as_is", "skip_and_continue"],
                    "max_fix_attempts": 2,
                    "expected_modes": ["timed_out"],
                },
            ],
            edges=[
                {"from_node": "fetch", "to_node": "fix-fetch", "edge_type": "on_failure"},
            ],
        )
        text = result["content"][0]["text"]
        # Handler reports success — not "Error".
        assert "Error" not in text, f"dag_create unexpectedly failed: {text}"
        assert "fix-node-dag" in text

        # The fix-node fields actually landed in the persisted DAG.
        dags = await dag_store.get_active_dags()
        fix_node_dag = next(d for d in dags if d.name == "fix-node-dag")
        fix_node = next(n for n in fix_node_dag.nodes if n.name == "fix-fetch")
        assert fix_node.node_type == "fix"
        assert fix_node.parent_node == "fetch"
        assert fix_node.fix_actions == ["retry_as_is", "skip_and_continue"]
        assert fix_node.max_fix_attempts == 2
        assert fix_node.expected_modes == ["timed_out"]

    @pytest.mark.asyncio
    async def test_dag_create_fix_node_missing_parent_node_errors(self, tools):
        """A fix node without parent_node should be rejected by the
        DAGCreateRequest validator and surfaced as an error — not silently
        accepted. Guards against the prior bug where parent_node was
        dropped: an LLM that omits it accidentally would have gotten a
        "running" DAG with a broken fix node."""
        handler = tools._handlers["dag_create"]
        result = await handler(
            name="broken-fix",
            nodes=[
                {"name": "task", "type": "subtask", "instructions": "Do it"},
                {
                    "name": "fix-task",
                    "type": "fix",
                    "instructions": "Recover",
                    # parent_node MISSING — must fail loudly
                    "fix_actions": ["mark_unrecoverable"],
                },
            ],
            edges=[
                {"from_node": "task", "to_node": "fix-task", "edge_type": "on_failure"},
            ],
        )
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "parent_node" in text

    @pytest.mark.asyncio
    async def test_dag_create_tool_schema_advertises_fix_surface(self, tools):
        """The tool's JSON schema (consumed by Anthropic tool-use
        validation) must list 'fix' in node.type.enum and 'on_failure'
        in edge.edge_type.enum, plus the four fix-node fields under
        node.properties. Without these, the LLM can't even attempt to
        author a fix node — the API rejects the call before the handler
        sees it."""
        schema = tools._schemas["dag_create"]
        node_props = schema["properties"]["nodes"]["items"]["properties"]
        assert "fix" in node_props["type"]["enum"]
        for field in ("parent_node", "fix_actions", "max_fix_attempts", "expected_modes"):
            assert field in node_props, f"node schema missing {field}"
        edge_enum = schema["properties"]["edges"]["items"]["properties"]["edge_type"]["enum"]
        assert "on_failure" in edge_enum

    @pytest.mark.asyncio
    async def test_dag_create_handles_db_error(self, tools, dag_store, dag_orchestrator):
        """dag_create catches non-ValueError exceptions and returns clean error."""
        original_create = dag_store.create
        async def broken_create(req):
            raise RuntimeError("DB connection lost")
        dag_store.create = broken_create

        handler = tools._handlers["dag_create"]
        result = await handler(
            name="broken-dag",
            nodes=_simple_nodes(),
            edges=[],
        )
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "DB connection lost" in text

        dag_store.create = original_create  # Restore


# ------------------------------------------------------------------
# dag_manage
# ------------------------------------------------------------------


class TestDagManageTool:
    @pytest.mark.asyncio
    async def test_list_empty(self, tools):
        """dag_manage list with no DAGs returns empty message."""
        handler = tools._handlers["dag_manage"]
        result = await handler(action="list")
        text = result["content"][0]["text"]
        assert "No active DAGs" in text or "Active DAGs" in text

    @pytest.mark.asyncio
    async def test_list_with_dags(self, tools):
        """dag_manage list shows active DAGs."""
        # Create a DAG first
        create_handler = tools._handlers["dag_create"]
        await create_handler(name="list-test", nodes=_simple_nodes(), edges=[])

        handler = tools._handlers["dag_manage"]
        result = await handler(action="list")
        text = result["content"][0]["text"]
        assert "list-test" in text
        assert "Active DAGs" in text

    @pytest.mark.asyncio
    async def test_status_by_prefix(self, tools, dag_store):
        """dag_manage status works with 8-char prefix."""
        # Create a DAG
        create_handler = tools._handlers["dag_create"]
        await create_handler(name="status-test", nodes=_simple_nodes(), edges=[])

        # Get the DAG to find its ID
        dags = await dag_store.get_active_dags()
        assert len(dags) >= 1
        dag = dags[0]
        prefix = str(dag.id)[:8]

        handler = tools._handlers["dag_manage"]
        result = await handler(action="status", dag_id=prefix)
        text = result["content"][0]["text"]
        assert "status-test" in text
        assert "task-1" in text or "Nodes" in text

    @pytest.mark.asyncio
    async def test_cancel(self, tools, dag_store):
        """dag_manage cancel stops a DAG."""
        create_handler = tools._handlers["dag_create"]
        await create_handler(name="cancel-test", nodes=_simple_nodes(), edges=[])

        dags = await dag_store.get_active_dags()
        dag = dags[0]

        handler = tools._handlers["dag_manage"]
        result = await handler(action="cancel", dag_id=str(dag.id))
        text = result["content"][0]["text"]
        assert "Cancelled" in text

    @pytest.mark.asyncio
    async def test_status_not_found(self, tools):
        """dag_manage status with unknown ID returns error."""
        handler = tools._handlers["dag_manage"]
        result = await handler(action="status", dag_id="00000000")
        text = result["content"][0]["text"]
        assert "not found" in text.lower() or "Error" in text

    @pytest.mark.asyncio
    async def test_missing_dag_id(self, tools):
        """dag_manage actions requiring dag_id return error when missing."""
        handler = tools._handlers["dag_manage"]
        result = await handler(action="status")
        text = result["content"][0]["text"]
        assert "dag_id required" in text or "Error" in text

    @pytest.mark.asyncio
    async def test_retry_node_requires_name(self, tools, dag_store):
        """dag_manage retry_node without node_name returns error."""
        create_handler = tools._handlers["dag_create"]
        await create_handler(name="retry-test", nodes=_simple_nodes(), edges=[])

        dags = await dag_store.get_active_dags()
        dag = dags[0]

        handler = tools._handlers["dag_manage"]
        result = await handler(action="retry_node", dag_id=str(dag.id))
        text = result["content"][0]["text"]
        assert "node_name required" in text or "Error" in text


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


class TestDagToolRegistration:
    def test_tools_registered(self, tools):
        """Both dag_create and dag_manage are registered."""
        assert "dag_create" in tools._handlers
        assert "dag_manage" in tools._handlers

    def test_schemas_registered(self, tools):
        """Both tools have schemas registered."""
        assert "dag_create" in tools._schemas
        assert "dag_manage" in tools._schemas


# ------------------------------------------------------------------
# Callback documentation (F090 phase 1 task 3)
# ------------------------------------------------------------------


class TestCallbackDocumented:
    """The tool schema is the only place an authoring LLM reads before
    building a DAG, so its callback description must be true. It must
    describe predecessor context-flow and the feature flag, and it must
    NOT claim callbacks receive 'tools' — that field is honored only for
    'check' nodes and is silently dropped for every other node type
    (subtask, callback, gate, fix). See orchestrator.py's
    _launch_check_node (the sole node.tools forwarding site) and
    SubtaskManager.create, which has no tools parameter.

    Scope of this test class: these are anti-regression pins on two past
    wordings (verbatim reintroduction of the brief's false "tools /
    frame_type" phrase, and the "never forwarded" overstatement) plus one
    structural oracle — every sentence in the description that mentions
    "tools" must also mention "check" — which is a real invariant for
    the specific claim that shipped false twice, not a blacklist of
    yesterday's phrasing. This set does NOT verify the description's
    prose is true in general; a paraphrase of some other, unrelated
    inaccuracy would sail through untouched. Only a human or reviewer
    reading the description against the code establishes that.
    """

    def test_callback_semantics_are_described(self, tools):
        schema = tools._schemas["dag_create"]
        node_props = schema["properties"]["nodes"]["items"]["properties"]
        assert "callback" in node_props["type"]["enum"]
        blob = str(schema)
        assert "predecessor" in blob.lower()
        assert "NOUS_DAG_CALLBACK_EXECUTION_ENABLED" in blob

    def test_callback_description_does_not_claim_tools(self, tools):
        """The description must not tell the LLM that callbacks accept
        'tools' like a subtask — SubtaskManager.create has no tools
        parameter, so a callback author relying on that claim would be
        misled. The original brief said 'the same tools / frame_type /
        model / timeout_seconds as a subtask'; that phrasing must be gone."""
        schema = tools._schemas["dag_create"]
        type_description = schema["properties"]["nodes"]["items"]["properties"]["type"]["description"]
        assert "tools / frame_type" not in type_description
        assert "the same tools" not in type_description.lower()
        # The shared-fields list itself must still be present, minus tools.
        assert "frame_type / model / timeout_seconds" in type_description

    def test_tools_field_scoped_to_check_nodes(self, tools):
        """The description must state that 'tools' is honored only for
        'check' nodes — not that it's "never forwarded" (that overstates
        it; check nodes do get it)."""
        schema = tools._schemas["dag_create"]
        type_description = schema["properties"]["nodes"]["items"]["properties"]["type"]["description"]
        lowered = type_description.lower()
        assert "'tools'" in lowered or '"tools"' in lowered
        assert "only" in lowered and "check" in lowered
        assert "never forwarded" not in lowered

    def test_every_tools_mention_is_check_scoped(self, tools):
        """Structural oracle, not a substring blacklist: split the
        description into sentences and assert every sentence that
        mentions 'tools' also mentions 'check' in the same sentence.

        This is the invariant that actually matters — 'tools' is true
        ONLY in the context of check nodes, everywhere else in the
        description it must not appear. It catches paraphrases of the
        false "callbacks get tools" claim that don't reuse any of the
        exact substrings the other two tests blacklist — e.g. "It
        accepts tools, plus frame_type / model / timeout_seconds, just
        as a subtask does." mentions tools in a sentence with no
        'check' in it, and fails here even though it reuses none of
        yesterday's exact wording."""
        schema = tools._schemas["dag_create"]
        type_description = schema["properties"]["nodes"]["items"]["properties"]["type"]["description"]
        sentences = [s for s in type_description.split(".") if s.strip()]
        assert sentences, "description unexpectedly empty"
        for sentence in sentences:
            if "tools" in sentence.lower():
                assert "check" in sentence.lower(), (
                    "sentence mentions 'tools' without scoping it to "
                    f"'check' nodes: {sentence.strip()!r}"
                )
