"""F112 MCP client: add or update one server and its tool permissions. Run by the owner, on the host:

    docker exec <nous container> python -m nous.mcp_client.seed --name deepwiki \\
        --url https://mcp.deepwiki.com/mcp --allow read_wiki_structure,ask_question [--background] [--write]

Connects once, through the client's address guard (``NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS`` applies), lists the
server's tools and prints each one's permission, name and description. With ``--write`` it stores the server row
and one row per tool: ``allow`` for the tools named in ``--allow``, ``deny`` for every other. Without ``--write``
nothing is written. Nous reads the rows at startup, with ``NOUS_MCP_CLIENT_ENABLED`` on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from nous.config import Settings
from nous.mcp_client.bridge import clean_description, has_reserved_property, input_schema_ok, safe_error, tool_name
from nous.mcp_client.config import ServerConfig, ToolConfig, parse_host_list
from nous.mcp_client.registry import McpRegistry
from nous.storage.database import Database

_LIST_TIMEOUT_SECONDS = 60
_PRINTED_DESCRIPTION_CHARS = 200


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m nous.mcp_client.seed", description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="the server's name in its tools' names (mcp__<name>__<tool>)")
    parser.add_argument("--url", required=True, help="the server's streamable-HTTP endpoint (https, no credentials)")
    parser.add_argument("--allow", default="", help="comma list of the server's tool names to allow; others are denied")
    parser.add_argument("--background", action="store_true", help="make the server available to background work")
    parser.add_argument("--write", action="store_true", help="store the server and the permissions")
    return parser.parse_args(argv)


def _startup_note(name: str | None, schema: Any) -> str:
    """Why the client would skip this tool at startup, or an empty string."""
    if name is None:
        return " (skipped at startup: its name is empty or too long)"
    if not input_schema_ok(schema):
        return " (skipped at startup: its input schema is not a valid object schema)"
    if has_reserved_property(schema):
        return " (skipped at startup: an argument name starts with '_')"
    return ""


async def seed(args: argparse.Namespace, settings: Settings) -> int:
    """List the server's tools, print them and, with ``--write``, store them. Returns the exit status."""
    from nous.mcp_client.transport import list_all_tools, open_session

    try:
        server = ServerConfig(name=args.name, url=args.url, background=args.background)
    except ValidationError as exc:
        for error in exc.errors():  # the message only: the error's input would repeat the URL
            print(f"error: {error['loc'][0]}: {error['msg']}", file=sys.stderr)
        return 2
    allowed = {tool.strip() for tool in args.allow.split(",") if tool.strip()}
    try:
        async with asyncio.timeout(_LIST_TIMEOUT_SECONDS):
            async with open_session(server.url, parse_host_list(settings.mcp_client_allow_private_hosts)) as session:
                tools = await list_all_tools(session)
    except Exception as exc:
        print(f"error: could not list the server's tools: {safe_error(exc)}", file=sys.stderr)
        return 1
    rows: list[ToolConfig] = []
    for tool in sorted(tools, key=lambda t: t.name):
        permission = "allow" if tool.name in allowed else "deny"
        name = tool_name(server.name, tool.name)
        description = tool.description or ""
        shown = clean_description(server.name, description)[:_PRINTED_DESCRIPTION_CHARS].replace("\n", " ")
        print(f"{permission:<5}  {name or repr(tool.name[:80])}{_startup_note(name, tool.inputSchema)}")
        print(f"       {shown}")
        rows.append(
            ToolConfig(
                server=server.name,
                tool=tool.name,
                description=description,
                input_schema=dict(tool.inputSchema),
                permission=permission,
            )
        )
    unknown = sorted(allowed - {row.tool for row in rows})
    if unknown:
        print(f"error: --allow names tools the server does not list: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not args.write:
        print("Nothing written: pass --write to store the server and these permissions.")
        return 0
    database = Database(settings)
    try:
        await database.connect()
        registry = McpRegistry(database, settings.agent_id)
        await registry.upsert_server(server)
        await registry.upsert_tools(server.name, rows)
    finally:
        await database.disconnect()
    print(
        f"Stored server {server.name!r}: {len(allowed)} tool(s) allowed, {len(rows) - len(allowed)} denied. "
        "Nous reads them at its next start, with NOUS_MCP_CLIENT_ENABLED=true."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m nous.mcp_client.seed``."""
    return asyncio.run(seed(_parse_args(argv), Settings()))


if __name__ == "__main__":
    sys.exit(main())
