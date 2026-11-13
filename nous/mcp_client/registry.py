"""F112 MCP client: the server and tool rows (``nous_system.mcp_servers``, ``nous_system.mcp_server_tools``).

Read by the client at startup and at every reload, written by the owner's routes (``nous/api/mcp_client_routes.py``)
and by ``python -m nous.mcp_client.seed``. Dialect-neutral (ORM statements), so the tests' SQLite database runs it
too.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.mcp_client.config import Permission, ServerConfig, ToolConfig
from nous.storage.database import Database
from nous.storage.models import McpServer, McpServerTool

logger = logging.getLogger(__name__)


class McpRegistry:
    """The MCP client's rows for one agent."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

    async def list_enabled(self) -> list[tuple[ServerConfig, list[ToolConfig]]]:
        """Every enabled server with all its tool rows, by server name. A row that no longer validates is skipped
        and logged by its server's name only (never its URL)."""
        return await self._list(enabled_only=True)

    async def list_all(self) -> list[tuple[ServerConfig, list[ToolConfig]]]:
        """Every server, enabled or not, with all its tool rows, as ``list_enabled`` reads them."""
        return await self._list(enabled_only=False)

    async def _list(self, *, enabled_only: bool) -> list[tuple[ServerConfig, list[ToolConfig]]]:
        async with self._db.session() as session:
            query = select(McpServer).where(McpServer.agent_id == self._agent_id)
            if enabled_only:
                query = query.where(McpServer.enabled.is_(True))
            servers = (await session.execute(query.order_by(McpServer.name))).scalars().all()
            ids = [server.id for server in servers]
            tools = (
                (
                    await session.execute(
                        select(McpServerTool).where(
                            McpServerTool.agent_id == self._agent_id, McpServerTool.server_id.in_(ids)
                        )
                    )
                ).scalars().all()
                if ids
                else []
            )
        out: list[tuple[ServerConfig, list[ToolConfig]]] = []
        for server in servers:
            try:
                config = ServerConfig(
                    name=server.name,
                    url=server.url,
                    transport=server.transport,
                    enabled=server.enabled,
                    background=server.background,
                    call_timeout_seconds=server.call_timeout_seconds,
                )
            except ValidationError:
                logger.warning("MCP client: server row %r does not validate; skipped", server.name)
                continue
            rows: list[ToolConfig] = []
            for tool in tools:
                if tool.server_id != server.id:
                    continue
                try:
                    rows.append(
                        ToolConfig(
                            server=server.name,
                            tool=tool.tool,
                            description=tool.description,
                            input_schema=tool.input_schema,
                            permission=tool.permission,
                        )
                    )
                except ValidationError:
                    logger.warning("MCP client: a tool row of server %r does not validate; skipped", server.name)
            out.append((config, rows))
        return out

    async def upsert_server(self, server: ServerConfig) -> uuid.UUID:
        """Insert the server's row, or update the row with its name. Returns the row's id."""
        async with self._db.session() as session:
            server_id, _created = await self._upsert_server(session, server)
            await session.commit()
        return server_id

    async def upsert_tools(self, server_name: str, tools: Sequence[ToolConfig]) -> None:
        """Make ``tools`` the server's listing: insert or update one row per tool of the server named ``server_name``
        (which must exist), and set every stored tool of it that ``tools`` does not name to ``deny`` (the row stays:
        nothing is deleted)."""
        async with self._db.session() as session:
            await self._upsert_tools(session, await self._server_id(session, server_name), tools)
            await session.commit()

    async def replace_server(self, server: ServerConfig, tools: Sequence[ToolConfig]) -> bool:
        """``upsert_server`` and ``upsert_tools`` in one transaction: a new URL is never stored with the old
        listing's permissions. True when the server's row was created."""
        async with self._db.session() as session:
            server_id, created = await self._upsert_server(session, server)
            await self._upsert_tools(session, server_id, tools)
            await session.commit()
        return created

    async def update_server(
        self,
        server_name: str,
        *,
        enabled: bool | None = None,
        background: bool | None = None,
        call_timeout_seconds: int | None = None,
        permissions: Mapping[str, Permission] | None = None,
    ) -> bool:
        """Set the given fields of the server's row and the given tools' permissions (stored tools only; another name
        is ignored), in one transaction. False when no server has that name."""
        async with self._db.session() as session:
            row = await self._server_row(session, server_name)
            if row is None:
                return False
            if enabled is not None:
                row.enabled = enabled
            if background is not None:
                row.background = background
            if call_timeout_seconds is not None:
                row.call_timeout_seconds = call_timeout_seconds
            row.updated_at = func.now()
            if permissions:
                tools = (
                    await session.execute(
                        select(McpServerTool).where(
                            McpServerTool.agent_id == self._agent_id,
                            McpServerTool.server_id == row.id,
                            McpServerTool.tool.in_(list(permissions)),
                        )
                    )
                ).scalars()
                for tool in tools:
                    tool.permission = permissions[tool.tool]
                    tool.updated_at = func.now()
            await session.commit()
        return True

    async def delete_server(self, server_name: str) -> bool:
        """Delete the server's row and its tool rows (configuration, not memory: nothing is kept). False when no
        server has that name."""
        async with self._db.session() as session:
            row = await self._server_row(session, server_name)
            if row is None:
                return False
            await session.execute(
                delete(McpServerTool).where(McpServerTool.agent_id == self._agent_id, McpServerTool.server_id == row.id)
            )
            await session.delete(row)
            await session.commit()
        return True

    async def _upsert_server(self, session: AsyncSession, server: ServerConfig) -> tuple[uuid.UUID, bool]:
        row = await self._server_row(session, server.name)
        created = row is None
        if row is None:
            row = McpServer(agent_id=self._agent_id, name=server.name)
            session.add(row)
        row.url = server.url
        row.transport = server.transport
        row.enabled = server.enabled
        row.background = server.background
        row.call_timeout_seconds = server.call_timeout_seconds
        row.updated_at = func.now()
        await session.flush()
        return row.id, created

    async def _upsert_tools(self, session: AsyncSession, server_id: uuid.UUID, tools: Sequence[ToolConfig]) -> None:
        existing = {
            row.tool: row
            for row in (
                await session.execute(
                    select(McpServerTool).where(
                        McpServerTool.agent_id == self._agent_id, McpServerTool.server_id == server_id
                    )
                )
            ).scalars()
        }
        for tool in tools:
            row = existing.get(tool.tool)
            if row is None:
                row = McpServerTool(agent_id=self._agent_id, server_id=server_id, tool=tool.tool)
                session.add(row)
            row.description = tool.description
            row.input_schema = tool.input_schema
            row.permission = tool.permission
            row.updated_at = func.now()
        listed = {tool.tool for tool in tools}
        for name, row in existing.items():
            if name not in listed and row.permission != "deny":
                row.permission = "deny"
                row.updated_at = func.now()

    async def _server_row(self, session: AsyncSession, server_name: str) -> McpServer | None:
        return (
            await session.execute(
                select(McpServer).where(McpServer.agent_id == self._agent_id, McpServer.name == server_name)
            )
        ).scalar_one_or_none()

    async def set_permission(self, server_name: str, tool: str, permission: Permission) -> bool:
        """Set one tool's permission. True when the row existed."""
        async with self._db.session() as session:
            server_id = await self._server_id(session, server_name)
            row = (
                await session.execute(
                    select(McpServerTool).where(
                        McpServerTool.agent_id == self._agent_id,
                        McpServerTool.server_id == server_id,
                        McpServerTool.tool == tool,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.permission = permission
            row.updated_at = func.now()
            await session.commit()
        return True

    async def _server_id(self, session: AsyncSession, server_name: str) -> uuid.UUID:
        server_id = (
            await session.execute(
                select(McpServer.id).where(McpServer.agent_id == self._agent_id, McpServer.name == server_name)
            )
        ).scalar_one_or_none()
        if server_id is None:
            raise LookupError(f"no MCP server named {server_name!r}")
        return server_id
