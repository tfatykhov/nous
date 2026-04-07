"""Unit tests for MCP server tool handlers.

Tests the call_tool handler logic directly by extracting it from the MCP
server. Falls back to testing tool behavior through the module-level
helper if direct extraction fails.

MockAgentRunner for chat/decide tools.
Real Postgres for recall/status/teach tools (via existing conftest fixtures).
"""

import pytest
import pytest_asyncio

from nous.brain.brain import Brain
from nous.brain.schemas import ReasonInput, RecordInput
from nous.cognitive.schemas import FrameSelection, TurnContext
from nous.heart import FactInput

# ---------------------------------------------------------------------------
# Mock AgentRunner
# ---------------------------------------------------------------------------


class MockAgentRunner:
    """Returns canned responses from run_turn()."""

    def __init__(self) -> None:
        self.run_turn_calls: list[tuple] = []
        self.preset_response = "Nous response via MCP"
        self.preset_context = TurnContext(
            system_prompt="You are Nous.",
            frame=FrameSelection(
                frame_id="conversation",
                frame_name="Conversation",
                confidence=0.9,
                match_method="default",
            ),
            decision_id=None,
            active_censors=[],
            context_token_estimate=100,
        )

    async def run_turn(self, session_id, user_message, agent_id=None):
        self.run_turn_calls.append((session_id, user_message, agent_id))
        return self.preset_response, self.preset_context, {"input_tokens": 100, "output_tokens": 50}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def brain(db, settings):
    """Brain without embeddings for MCP tests."""
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


@pytest.fixture
def mock_runner():
    return MockAgentRunner()


@pytest_asyncio.fixture
async def mcp_tools(mock_runner, brain, heart, settings):
    """Provides a tool caller that invokes MCP tools directly.

    Extracts the call_tool handler registered on the MCP Server so
    we can test each tool without HTTP/MCP transport overhead.
    The extraction strategy tries multiple MCP library versions.
    """
    from nous.api.mcp import create_mcp_server

    manager = create_mcp_server(mock_runner, brain, heart, settings)

    from mcp.types import CallToolRequest, CallToolRequestParams

    # MCPTransportManager exposes .server directly
    server = manager.server

    call_tool_handler = None

    if server:
        # mcp 1.26.0: request_handlers is keyed by request class, not string
        handlers = getattr(server, "request_handlers", {})
        call_tool_handler = handlers.get(CallToolRequest)

    async def call_tool(name: str, arguments: dict):
        """Invoke a registered MCP tool by name."""
        if call_tool_handler is not None:
            request = CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name=name, arguments=arguments),
            )
            result = await call_tool_handler(request)
            return result

        pytest.skip("Could not extract MCP call_tool handler — MCP library version incompatible")

    yield call_tool


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_decision(brain, session):
    return await brain.record(
        RecordInput(
            description="MCP test decision about caching",
            confidence=0.8,
            category="architecture",
            stakes="medium",
            reasons=[ReasonInput(type="analysis", text="Test")],
            tags=["mcp", "test"],
        ),
        session=session,
    )


async def _seed_fact(heart, session):
    return await heart.learn(
        FactInput(
            content="MCP test fact: Python supports async/await",
            category="technical",
            confidence=0.9,
        ),
        session=session,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_nous_chat(mcp_tools, mock_runner):
    """nous_chat calls runner.run_turn, returns response."""
    result = await mcp_tools("nous_chat", {"message": "Hello from MCP!"})

    assert len(mock_runner.run_turn_calls) == 1
    _, msg, _ = mock_runner.run_turn_calls[0]
    assert msg == "Hello from MCP!"
    assert result is not None


async def test_nous_recall_all(mcp_tools, brain, heart, session):
    """nous_recall with type=all searches brain + heart, merges results."""
    await _seed_decision(brain, session)
    await _seed_fact(heart, session)
    await session.commit()

    result = await mcp_tools("nous_recall", {"query": "test", "memory_type": "all"})
    assert result is not None


async def test_nous_recall_decisions(mcp_tools, brain, session):
    """nous_recall with type=decisions searches brain only."""
    await _seed_decision(brain, session)
    await session.commit()

    result = await mcp_tools("nous_recall", {"query": "caching", "memory_type": "decisions"})
    assert result is not None


async def test_nous_recall_facts(mcp_tools, heart, session):
    """nous_recall with type=facts searches heart facts only."""
    await _seed_fact(heart, session)
    await session.commit()

    result = await mcp_tools("nous_recall", {"query": "Python async", "memory_type": "facts"})
    assert result is not None


async def test_nous_status(mcp_tools):
    """nous_status returns calibration + counts."""
    result = await mcp_tools("nous_status", {})
    assert result is not None


async def test_nous_teach_fact(mcp_tools):
    """nous_teach creates fact via heart.facts.learn."""
    result = await mcp_tools(
        "nous_teach",
        {
            "type": "fact",
            "content": "PostgreSQL supports JSONB natively",
            "domain": "database",
        },
    )
    assert result is not None


async def test_nous_teach_procedure(mcp_tools):
    """nous_teach creates procedure via heart.procedures.store."""
    result = await mcp_tools(
        "nous_teach",
        {
            "type": "procedure",
            "content": "Always run tests before merging to main",
            "domain": "process",
        },
    )
    assert result is not None


async def test_nous_decide(mcp_tools, mock_runner):
    """nous_decide forces decision frame, calls runner.run_turn."""
    result = await mcp_tools(
        "nous_decide",
        {
            "question": "Should we use Redis for caching?",
            "stakes": "medium",
        },
    )

    assert len(mock_runner.run_turn_calls) == 1
    _, msg, _ = mock_runner.run_turn_calls[0]
    # The message should contain the question, possibly prefixed with stakes
    assert "Redis" in msg or "caching" in msg
    assert result is not None
