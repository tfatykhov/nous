"""MCP interface — lets other agents interact with Nous.

Exposes 5 tools:
  nous_chat    - Send a message, get a response
  nous_recall  - Search across all memory types
  nous_status  - Get agent status and calibration
  nous_teach   - Add a fact or procedure to Nous's memory
  nous_decide  - Ask Nous to make a decision (forces decision frame)

Uses mcp library's Server + StreamableHTTPServerTransport,
mounted at /mcp on the same Starlette app.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import TextContent, Tool

from nous.api.runner import AgentRunner
from nous.brain import Brain
from nous.config import Settings
from nous.heart import Heart
from nous.heart.schemas import FactInput, ProcedureInput

logger = logging.getLogger(__name__)


class MCPTransportManager:
    """Manages MCP Server + StreamableHTTPServerTransport lifecycle.

    Provides an ASGI-compatible handle_request method and a close()
    method for shutdown.  On the first request the transport is created
    and server.run() is launched in a background task.
    """

    def __init__(self, server: Server) -> None:
        self.server = server
        self._transport: StreamableHTTPServerTransport | None = None
        self._task_group: anyio.abc.TaskGroup | None = None
        self._started = False

    async def _ensure_started(self) -> None:
        """Lazily start transport + server on first request."""
        if self._started:
            return

        session_id = uuid.uuid4().hex
        self._transport = StreamableHTTPServerTransport(
            mcp_session_id=session_id,
            is_json_response_enabled=True,
        )
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()

        ready_event = anyio.Event()

        async def _run_server(
            transport: StreamableHTTPServerTransport,
            server: Server,
        ) -> None:
            async with transport.connect() as (read_stream, write_stream):
                ready_event.set()
                init_options = server.create_initialization_options()
                await server.run(
                    read_stream,
                    write_stream,
                    init_options,
                    stateless=True,
                )

        self._task_group.start_soon(_run_server, self._transport, self.server)
        await ready_event.wait()
        self._started = True

    async def handle_request(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI entry point — delegates to transport."""
        await self._ensure_started()
        assert self._transport is not None
        await self._transport.handle_request(scope, receive, send)

    async def close(self) -> None:
        """Shutdown transport and cancel server task."""
        if self._transport is not None:
            self._transport.terminate()
        if self._task_group is not None:
            self._task_group.cancel_scope.cancel()
            try:
                await self._task_group.__aexit__(None, None, None)
            except Exception:
                logger.debug("MCP task group cleanup exception (expected)")
        self._started = False


def create_mcp_server(
    runner: AgentRunner,
    brain: Brain,
    heart: Heart,
    settings: Settings,
) -> MCPTransportManager:
    """Create MCP server with Nous tools.

    Returns MCPTransportManager to be mounted on Starlette.
    The manager exposes handle_request() for ASGI and close() for shutdown.
    """
    server = Server("nous")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="nous_chat",
                description=(
                    "Send a message to Nous and get a response. Nous will think about "
                    "your message using its decision intelligence and memory systems."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Your message to Nous"},
                        "session_id": {
                            "type": "string",
                            "description": "Optional session ID for multi-turn conversations",
                        },
                    },
                    "required": ["message"],
                },
            ),
            Tool(
                name="nous_recall",
                description=(
                    "Search Nous's memory for relevant information. Searches across "
                    "decisions, facts, episodes, and procedures."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for"},
                        "memory_type": {
                            "type": "string",
                            "enum": ["all", "decisions", "facts", "episodes", "procedures"],
                            "description": "Type of memory to search (default: all)",
                        },
                        "limit": {"type": "integer", "description": "Max results (default: 5)"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="nous_status",
                description="Get Nous's current status: calibration accuracy, memory counts, active frames.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="nous_teach",
                description=(
                    "Teach Nous a new fact or procedure. Facts are things to know; procedures are how-to knowledge."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["fact", "procedure"],
                            "description": "What kind of knowledge",
                        },
                        "content": {"type": "string", "description": "The knowledge to teach"},
                        "domain": {"type": "string", "description": "Domain or category (optional)"},
                        "source": {"type": "string", "description": "Where this knowledge came from"},
                    },
                    "required": ["type", "content"],
                },
            ),
            Tool(
                name="nous_decide",
                description=(
                    "Ask Nous to make a decision. Forces the decision cognitive frame "
                    "for thorough analysis with guardrails, similar past decisions, "
                    "and calibrated confidence."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The decision to make"},
                        "stakes": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "critical"],
                            "description": "How important is this decision (default: medium)",
                        },
                    },
                    "required": ["question"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        """Route tool calls to appropriate handlers."""
        try:
            if name == "nous_chat":
                return await _handle_chat(arguments)
            elif name == "nous_recall":
                return await _handle_recall(arguments)
            elif name == "nous_status":
                return await _handle_status(arguments)
            elif name == "nous_teach":
                return await _handle_teach(arguments)
            elif name == "nous_decide":
                return await _handle_decide(arguments)
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
        except Exception as e:
            logger.error("MCP tool %s error: %s", name, e)
            return [TextContent(type="text", text=f"Error: {e}")]

    async def _handle_chat(args: dict) -> list[TextContent]:
        message = args["message"]
        session_id = args.get("session_id", "mcp-session")
        response_text, turn_context, _usage = await runner.run_turn(session_id, message)
        result = {
            "response": response_text,
            "session_id": session_id,
            "frame": turn_context.frame.frame_id,
            "decision_id": turn_context.decision_id,
        }
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def _handle_recall(args: dict) -> list[TextContent]:
        query = args["query"]
        memory_type = args.get("memory_type", "all")
        # AS-10: cap caller-supplied limit so a hostile/buggy client can't
        # request an unbounded expensive recall.
        limit = min(int(args.get("limit", 5)), 100)

        results: list[dict] = []

        # Brain decisions
        if memory_type in ("all", "decisions"):
            decisions = await brain.query(query, limit=limit)
            for d in decisions:
                results.append(
                    {
                        "source": "brain",
                        "type": "decision",
                        "id": str(d.id),
                        "summary": d.description,
                        "score": d.score,
                        "metadata": {
                            "category": d.category,
                            "stakes": d.stakes,
                            "outcome": d.outcome,
                            "confidence": d.confidence,
                        },
                    }
                )

        # Heart memories
        if memory_type == "all":
            recalls = await heart.recall(query, limit=limit)
            for r in recalls:
                results.append(
                    {
                        "source": "heart",
                        "type": r.type,
                        "id": str(r.id),
                        "summary": r.summary,
                        "score": r.score,
                        "metadata": r.metadata,
                    }
                )
        elif memory_type in ("facts", "episodes", "procedures"):
            # Map to heart recall types
            type_map = {"facts": ["fact"], "episodes": ["episode"], "procedures": ["procedure"]}
            recalls = await heart.recall(query, limit=limit, types=type_map[memory_type])
            for r in recalls:
                results.append(
                    {
                        "source": "heart",
                        "type": r.type,
                        "id": str(r.id),
                        "summary": r.summary,
                        "score": r.score,
                        "metadata": r.metadata,
                    }
                )

        return [TextContent(type="text", text=json.dumps(results, default=str))]

    async def _handle_status(args: dict) -> list[TextContent]:
        calibration = await brain.get_calibration()
        result = {
            "agent_id": settings.agent_id,
            "agent_name": settings.agent_name,
            "model": settings.model,
            "calibration": {
                "brier_score": calibration.brier_score,
                "accuracy": calibration.accuracy,
                "total_decisions": calibration.total_decisions,
                "reviewed_count": calibration.reviewed_decisions,
            },
        }
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    async def _handle_teach(args: dict) -> list[TextContent]:
        teach_type = args["type"]
        content = args["content"]
        domain = args.get("domain")
        source = args.get("source", "mcp")

        if teach_type == "fact":
            fact_input = FactInput(
                content=content,
                category=domain,
                source=source,
            )
            fact = await heart.learn(fact_input)
            return [TextContent(type="text", text=f"Learned fact: {fact.id}")]

        elif teach_type == "procedure":
            # F11: Derive name from content[:100]
            name = content[:100].strip()
            if len(content) > 100:
                name = name.rsplit(" ", 1)[0] + "..."
            proc_input = ProcedureInput(
                name=name,
                domain=domain,
                description=content,
            )
            proc = await heart.store_procedure(proc_input)
            return [TextContent(type="text", text=f"Stored procedure: {proc.id}")]

        return [TextContent(type="text", text=f"Unknown teach type: {teach_type}")]

    async def _handle_decide(args: dict) -> list[TextContent]:
        question = args["question"]
        stakes = args.get("stakes", "medium")

        # F21: Include stakes in message prefix
        message = f"Decision (stakes: {stakes}): {question}"
        session_id = "mcp-decision"

        response_text, turn_context, _usage = await runner.run_turn(session_id, message)
        result = {
            "response": response_text,
            "decision_id": turn_context.decision_id,
            "frame": turn_context.frame.frame_id,
            "stakes": stakes,
        }
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return MCPTransportManager(server)
