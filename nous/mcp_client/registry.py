"""F112 MCP client: the server and tool rows (``nous_system.mcp_servers``, ``nous_system.mcp_server_tools``) and
the servers' sealed credentials (``nous_system.mcp_server_secrets``).

Read by the client at startup and at every reload, written by the owner's routes (``nous/api/mcp_client_routes.py``)
and by ``python -m nous.mcp_client.seed``. Dialect-neutral (ORM statements), so the tests' SQLite database runs it
too. A credential is never opened here: the rows hold it sealed (``secrets.py``). It stays with its server's URL:
storing a server at another URL deletes its credential in the same transaction, whoever writes the row.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Text, delete, func, select, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.mcp_client.config import Permission, ServerConfig, ToolConfig
from nous.mcp_client.secrets import SealedSecret, StoredSecret
from nous.storage.database import Database
from nous.storage.models import McpServer, McpServerSecret, McpServerTool

logger = logging.getLogger(__name__)


def _header_names(value: Any) -> tuple[str, ...]:
    """A credential row's plain header names, read as text: Postgres (asyncpg) gives the array as a list, the tests'
    SQLite backend gives its JSON text (read as an ARRAY, that text would be split into characters)."""
    if isinstance(value, str):
        value = json.loads(value)
    return tuple(value or ())


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

    async def list_secrets(self) -> dict[str, StoredSecret]:
        """Every server's stored credential, sealed, by server name."""
        secret = McpServerSecret
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(
                        McpServer.name,
                        secret.id,
                        secret.kind,
                        secret.key_id,
                        secret.nonce,
                        secret.ciphertext,
                        type_coerce(secret.header_names, Text),  # see _header_names
                        secret.updated_at,
                    )
                    .join(McpServer, McpServer.id == secret.server_id)
                    .where(secret.agent_id == self._agent_id, McpServer.agent_id == self._agent_id)
                )
            ).all()
        return {
            name: StoredSecret(
                id=secret_id,
                kind=kind,
                key_id=key_id,
                nonce=bytes(nonce),
                ciphertext=bytes(ciphertext),
                header_names=_header_names(names),
                updated_at=updated_at,
            )
            for name, secret_id, kind, key_id, nonce, ciphertext, names, updated_at in rows
        }

    async def upsert_server(self, server: ServerConfig) -> uuid.UUID:
        """Insert the server's row, or update the row with its name (a new URL deletes its stored credential).
        Returns the row's id."""
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

    async def replace_server(
        self, server: ServerConfig, tools: Sequence[ToolConfig], *, secret: SealedSecret | None = None
    ) -> bool:
        """``upsert_server`` and ``upsert_tools`` in one transaction: a new URL is never stored with the old
        listing's permissions, nor with the old credential. With ``secret``, it becomes the server's credential in
        the same transaction. True when the server's row was created."""
        async with self._db.session() as session:
            server_id, created = await self._upsert_server(session, server)
            await self._upsert_tools(session, server_id, tools)
            if secret is not None:
                await self._set_secret(session, server_id, secret)
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
        secret: SealedSecret | None = None,
        remove_secret: bool = False,
    ) -> bool:
        """Set the given fields of the server's row and the given tools' permissions (stored tools only; another name
        is ignored), make ``secret`` its credential or, with ``remove_secret``, delete its credential, in one
        transaction. False when no server has that name."""
        async with self._db.session() as session:
            row = await self._server_row(session, server_name)
            if row is None:
                return False
            if secret is not None:
                await self._set_secret(session, row.id, secret)
            elif remove_secret:
                await session.execute(delete(McpServerSecret).where(McpServerSecret.server_id == row.id))
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

    async def update_secret_in_place(self, secret_id: uuid.UUID, secret: SealedSecret) -> bool:
        """Overwrite the sealed value of the credential row ``secret_id`` (the same id: the same version), for a
        token refresh (PR 2d). False when no such row exists any more (replaced, removed or signed out meanwhile):
        the caller then discards what it refreshed."""
        async with self._db.session() as session:
            result = await session.execute(
                update(McpServerSecret)
                .where(McpServerSecret.agent_id == self._agent_id, McpServerSecret.id == secret_id)
                .values(
                    key_id=secret.key_id,
                    nonce=secret.nonce,
                    ciphertext=secret.ciphertext,
                    header_names=list(secret.header_names),
                    updated_at=func.now(),
                )
            )
            await session.commit()
        return bool(getattr(result, "rowcount", 0))

    async def delete_server(self, server_name: str) -> bool:
        """Delete the server's row and its tool rows (configuration, not memory: nothing is kept); its credential goes
        with it (ON DELETE CASCADE). False when no server has that name."""
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
        elif row.url != server.url:
            # A credential never follows a server to another address, whoever stores it (the routes, the seed).
            await session.execute(delete(McpServerSecret).where(McpServerSecret.server_id == row.id))
        row.url = server.url
        row.transport = server.transport
        row.enabled = server.enabled
        row.background = server.background
        row.call_timeout_seconds = server.call_timeout_seconds
        row.updated_at = func.now()
        await session.flush()
        return row.id, created

    async def _set_secret(self, session: AsyncSession, server_id: uuid.UUID, secret: SealedSecret) -> None:
        """Make ``secret`` the server's credential: the old row is deleted and a new one inserted, so the new
        credential has a new id (the manager drops a connection whose credential id changed)."""
        await session.execute(delete(McpServerSecret).where(McpServerSecret.server_id == server_id))
        await session.flush()
        session.add(
            McpServerSecret(
                id=uuid.uuid4(),
                agent_id=self._agent_id,
                server_id=server_id,
                kind=secret.kind,
                key_id=secret.key_id,
                nonce=secret.nonce,
                ciphertext=secret.ciphertext,
                header_names=list(secret.header_names),
            )
        )
        await session.flush()

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
