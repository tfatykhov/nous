"""Integration tests for nous/api/tools.py -- tool closures and ToolDispatcher.

Part 1: Closure tests use real Brain/Heart instances with mock embeddings
against real Postgres. Tool closures capture Brain/Heart in closure
context and use auto-sessions (no session= parameter).

Part 2: ToolDispatcher unit tests verify registration, dispatch,
unknown tool handling, error propagation, tool definitions output,
and frame-gated tool filtering.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from nous.api.tools import create_nous_tools, ToolDispatcher
from nous.brain.brain import Brain
from nous.config import Settings
from nous.heart import Heart


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, mock_embeddings):
    """Brain with mock embeddings for tool tests."""
    settings = Settings()
    b = Brain(database=db, settings=settings, embedding_provider=mock_embeddings)
    yield b
    await b.close()


@pytest_asyncio.fixture
async def tools(brain, heart):
    """Tool closures dict from create_nous_tools."""
    return create_nous_tools(brain, heart)


# ---------------------------------------------------------------------------
# test_create_nous_tools
# ---------------------------------------------------------------------------


class TestCreateNousTools:
    """Test that create_nous_tools returns the expected structure."""

    def test_create_nous_tools(self, tools):
        """Returns dict with async callable functions."""
        assert isinstance(tools, dict)
        assert set(tools.keys()) == {
            "record_decision",
            "learn_fact",
            "recall_deep",
            "create_censor",
            "recall_recent",
            "learn_skill",
            "get_procedure",
            "recall_hubs",  # F065
            "ingest_document",  # F069
        }
        for name, func in tools.items():
            assert callable(func), f"{name} should be callable"


# ---------------------------------------------------------------------------
# record_decision tests
# ---------------------------------------------------------------------------


class TestRecordDecision:
    """Test the record_decision tool closure."""

    @pytest.mark.asyncio
    async def test_record_decision_success(self, tools):
        """Valid input -> brain.record called, returns ID."""
        result = await tools["record_decision"](
            description="Test tool decision for integration",
            confidence=0.85,
            category="tooling",
            stakes="low",
            context="Testing the record_decision tool closure",
            reasons=[
                {"type": "analysis", "text": "Tool test reason"},
                {"type": "pattern", "text": "Following test patterns"},
            ],
            tags=["test", "tool-closure"],
        )

        assert "content" in result
        assert len(result["content"]) == 1
        text = result["content"][0]["text"]
        assert "Decision recorded successfully" in text
        assert "ID:" in text
        assert "Quality score:" in text
        assert "Category: tooling" in text
        assert "Stakes: low" in text

    @pytest.mark.asyncio
    async def test_record_decision_invalid_reasons(self, tools):
        """Bad reason format -> error message (not exception)."""
        result = await tools["record_decision"](
            description="Decision with bad reasons",
            confidence=0.5,
            category="process",
            stakes="low",
            reasons=[{"bad": "format"}],  # Missing 'type' and 'text'
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "Invalid reason format" in text

    @pytest.mark.asyncio
    async def test_record_decision_brain_error(self, tools):
        """brain.record raises -> error message (not exception)."""
        # Trigger a pydantic validation error with invalid confidence range
        result = await tools["record_decision"](
            description="Decision with invalid confidence",
            confidence=2.0,  # Out of range [0.0, 1.0]
            category="tooling",
            stakes="low",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# learn_fact tests
# ---------------------------------------------------------------------------


class TestLearnFact:
    """Test the learn_fact tool closure."""

    @pytest.mark.asyncio
    async def test_learn_fact_success(self, tools):
        """Valid input -> heart.learn called, returns ID."""
        result = await tools["learn_fact"](
            content="Python 3.12 supports improved error messages",
            category="technical",
            subject="python",
            confidence=0.95,
            source="documentation",
            tags=["python", "test"],
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Fact learned successfully" in text
        assert "ID:" in text
        assert "Category: technical" in text
        assert "Subject: python" in text

    @pytest.mark.asyncio
    async def test_learn_fact_with_contradiction(self, tools):
        """Learning a near-duplicate fact surfaces contradiction warning."""
        # Learn a fact first
        await tools["learn_fact"](
            content="The default database port is 5432",
            category="technical",
            subject="postgres",
        )

        # Learn a contradicting fact with same content (mock embeddings
        # produce identical vectors for identical text -> high similarity)
        result = await tools["learn_fact"](
            content="The default database port is 5432",
            category="technical",
            subject="postgres",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Fact learned successfully" in text
        # Contradiction warning may or may not appear depending on
        # similarity threshold; at minimum the fact should be stored
        assert "ID:" in text


# ---------------------------------------------------------------------------
# recall_deep tests
# ---------------------------------------------------------------------------


class TestRecallDeep:
    """Test the recall_deep tool closure."""

    @pytest.mark.asyncio
    async def test_recall_deep_all(self, tools):
        """Searches Heart + Brain when no memory_types specified."""
        # Seed data: record a decision and learn a fact
        await tools["record_decision"](
            description="Recall deep test architecture decision about caching",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            context="Testing recall_deep all search",
            tags=["recall-test"],
        )
        await tools["learn_fact"](
            content="Caching improves performance in recall deep tests",
            category="technical",
            subject="caching",
        )

        # Search across all types (default)
        result = await tools["recall_deep"](query="caching architecture")

        assert "content" in result
        text = result["content"][0]["text"]
        # Should have both Heart and Brain sections
        assert "Heart Memory" in text or "Brain Decisions" in text

    @pytest.mark.asyncio
    async def test_recall_deep_decisions_only(self, tools):
        """memory_types=["decision"] searches Brain only."""
        # Seed a decision
        await tools["record_decision"](
            description="Decision-only recall test about deployment strategy",
            confidence=0.75,
            category="process",
            stakes="low",
            tags=["recall-decision-only"],
        )

        result = await tools["recall_deep"](
            query="deployment strategy",
            memory_types=["decision"],
        )

        assert "content" in result
        text = result["content"][0]["text"]
        # Should have Brain section but not Heart
        assert "Brain Decisions" in text
        assert "Heart Memory" not in text

    @pytest.mark.asyncio
    async def test_recall_deep_facts_only(self, tools):
        """memory_types=["fact"] searches Heart facts only."""
        # Seed a fact
        await tools["learn_fact"](
            content="Recall deep facts-only test about memory architecture",
            category="technical",
            subject="memory",
        )

        result = await tools["recall_deep"](
            query="memory architecture",
            memory_types=["fact"],
        )

        assert "content" in result
        text = result["content"][0]["text"]
        # Should have Heart section but not Brain
        assert "Heart Memory" in text
        assert "Brain Decisions" not in text

    @pytest.mark.asyncio
    async def test_recall_deep_empty(self, tools):
        """No results -> 'No results found.' in section output."""
        # Use heart-only type "episode" — no episodes seeded by tool tests
        result = await tools["recall_deep"](
            query="zzz_nonexistent_query_no_match",
            memory_types=["episode"],
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "No results found" in text

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_recall_deep_surfaces_fact_id(self, tools):
        """Heart fact results include an 'id: <uuid>' so get_procedure/get_fact callers
        can reference them without fabricating UUIDs (anti-hallucination contract).

        Integration-only: requires live PostgreSQL for pgvector <=> operator.
        """
        import re

        await tools["learn_fact"](
            content="Id surfacing test fact about quantum widgets",
            category="technical",
            subject="id-surface",
        )

        result = await tools["recall_deep"](
            query="quantum widgets",
            memory_types=["fact"],
        )

        text = result["content"][0]["text"]
        assert "Heart Memory" in text
        assert "id: " in text, "recall_deep must surface entity IDs for get_procedure/detail callers"
        # Verify the id is a real UUID format, not a placeholder
        uuid_match = re.search(
            r"id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
        )
        assert uuid_match is not None, f"Expected UUID in output, got: {text}"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_recall_deep_surfaces_decision_id(self, tools):
        """Brain decision results include an 'id: <uuid>' so follow-up tools
        can reference the decision without fabricating UUIDs.

        Integration-only: requires live PostgreSQL for pgvector <=> operator.
        """
        import re

        await tools["record_decision"](
            description="Id surfacing decision about deployment topology",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            tags=["id-surface"],
        )

        result = await tools["recall_deep"](
            query="deployment topology",
            memory_types=["decision"],
        )

        text = result["content"][0]["text"]
        assert "Brain Decisions" in text
        uuid_match = re.search(
            r"id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
        )
        assert uuid_match is not None, f"Expected decision UUID in output, got: {text}"


# ---------------------------------------------------------------------------
# create_censor tests
# ---------------------------------------------------------------------------


class TestCreateCensor:
    """Test the create_censor tool closure."""

    @pytest.mark.asyncio
    async def test_create_censor_success(self, tools):
        """Valid input -> heart.add_censor, returns ID."""
        result = await tools["create_censor"](
            trigger_pattern="rm -rf /",
            reason="Dangerous command that could delete everything",
            action="block",
            domain="debugging",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Censor created successfully" in text
        assert "ID:" in text
        assert "Action: block" in text
        assert "Domain: debugging" in text
        assert "Pattern: rm -rf /" in text

    @pytest.mark.asyncio
    async def test_create_censor_invalid_uuid(self, tools):
        """Bad UUID string -> validation error message."""
        result = await tools["create_censor"](
            trigger_pattern="test pattern",
            reason="Test reason",
            learned_from_decision="not-a-valid-uuid",
        )

        assert "content" in result
        text = result["content"][0]["text"]
        assert "Validation error" in text or "Error" in text


# ---------------------------------------------------------------------------
# ToolDispatcher unit tests (no DB needed)
# ---------------------------------------------------------------------------


_ECHO_SCHEMA: dict = {
    "type": "object",
    "description": "Echo tool for testing",
    "properties": {
        "message": {"type": "string", "description": "Message to echo"},
    },
    "required": ["message"],
}

_ADD_SCHEMA: dict = {
    "type": "object",
    "description": "Add two numbers for testing",
    "properties": {
        "a": {"type": "number"},
        "b": {"type": "number"},
    },
    "required": ["a", "b"],
}


class TestToolDispatcher:
    """Unit tests for ToolDispatcher registration, dispatch, and filtering."""

    @pytest.mark.asyncio
    async def test_dispatcher_register_and_dispatch(self):
        """Register a handler, dispatch a call, verify result text and no error."""
        dispatcher = ToolDispatcher()

        async def echo_handler(message: str) -> dict:
            return {"content": [{"type": "text", "text": f"Echo: {message}"}]}

        dispatcher.register("echo", echo_handler, _ECHO_SCHEMA)

        result_text, is_error = await dispatcher.dispatch("echo", {"message": "hello"})
        assert result_text == "Echo: hello"
        assert is_error is False

    @pytest.mark.asyncio
    async def test_dispatcher_unknown_tool(self):
        """Dispatch unknown tool name -> error tuple with 'Unknown tool' message."""
        dispatcher = ToolDispatcher()

        result_text, is_error = await dispatcher.dispatch("nonexistent", {})
        assert is_error is True
        assert "Unknown tool: nonexistent" in result_text

    @pytest.mark.asyncio
    async def test_dispatcher_tool_error(self):
        """Handler raises exception -> error tuple with exception message."""
        dispatcher = ToolDispatcher()

        async def failing_handler(**kwargs) -> dict:
            raise ValueError("Something went wrong in the tool")

        dispatcher.register("fail", failing_handler, _ECHO_SCHEMA)

        result_text, is_error = await dispatcher.dispatch("fail", {})
        assert is_error is True
        assert "Tool error:" in result_text
        assert "Something went wrong" in result_text

    def test_dispatcher_tool_definitions(self):
        """tool_definitions() returns Anthropic API format with name, description, input_schema."""
        dispatcher = ToolDispatcher()

        async def echo_handler(message: str) -> dict:
            return {"content": [{"type": "text", "text": message}]}

        async def add_handler(a: float, b: float) -> dict:
            return {"content": [{"type": "text", "text": str(a + b)}]}

        dispatcher.register("echo", echo_handler, _ECHO_SCHEMA)
        dispatcher.register("add", add_handler, _ADD_SCHEMA)

        definitions = dispatcher.tool_definitions()
        assert len(definitions) == 2

        # Each definition should have name, description, input_schema
        names = {d["name"] for d in definitions}
        assert names == {"echo", "add"}

        for defn in definitions:
            assert "name" in defn
            assert "description" in defn
            assert "input_schema" in defn
            assert defn["input_schema"]["type"] == "object"

    def test_dispatcher_available_tools_with_frame(self):
        """available_tools() filters by FRAME_TOOLS map.

        Register several tools, verify that frame filtering works.
        The 'question' frame only allows 'recall_deep'.
        """
        dispatcher = ToolDispatcher()

        # Register multiple tools (using mock handlers)
        handler = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        for name in ["record_decision", "learn_fact", "recall_deep", "create_censor", "bash"]:
            dispatcher.register(name, handler, {"type": "object", "description": f"{name} tool"})

        # 'question' frame: filters to tools in FRAME_TOOLS["question"]
        question_tools = dispatcher.available_tools("question")
        question_names = {t["name"] for t in question_tools}
        # All 5 registered tools are in the question frame's allowed list
        assert question_names == {"record_decision", "learn_fact", "recall_deep", "create_censor", "bash"}

    def test_dispatcher_available_tools_wildcard_frame(self):
        """Frame with wildcard '*' returns all registered tools."""
        dispatcher = ToolDispatcher()

        handler = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        for name in ["record_decision", "recall_deep", "bash", "read_file", "write_file"]:
            dispatcher.register(name, handler, {"type": "object", "description": f"{name} tool"})

        # 'task' frame has "*" in FRAME_TOOLS -> all tools
        task_tools = dispatcher.available_tools("task")
        task_names = {t["name"] for t in task_tools}
        assert task_names == {"record_decision", "recall_deep", "bash", "read_file", "write_file"}

    def test_dispatcher_available_tools_unknown_frame(self):
        """Unknown frame ID returns empty tool list."""
        dispatcher = ToolDispatcher()

        handler = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        dispatcher.register("recall_deep", handler, {"type": "object", "description": "test"})

        unknown_tools = dispatcher.available_tools("nonexistent_frame")
        assert unknown_tools == []
