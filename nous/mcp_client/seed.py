"""F112 MCP client: add or update one server and its tool permissions. Run by the owner, on the host:

    docker exec <nous container> python -m nous.mcp_client.seed --name deepwiki \\
        --url https://mcp.deepwiki.com/mcp --allow read_wiki_structure,ask_question [--background] [--write]

Connects once, through the client's address guard (``NOUS_MCP_CLIENT_ALLOW_PRIVATE_HOSTS`` applies), lists the
server's tools and prints, for each, its permission, its registered name, why it would be skipped at startup if it
would, and its description and input schema exactly as the model would be offered them (``bridge.prepare_tool``).
It also prints the pin entry the owner adds to ``NOUS_MCP_CLIENT_PINNED_SERVERS`` on the host: without it, no tool of
the server registers.

With ``--write`` it stores the server row and one row per listed tool: ``allow`` for the tools named in ``--allow``,
``deny`` for every other. Running it again for a server replaces the row's URL and background switch, enables it and
sets its call timeout to 30 s, and makes the new listing the permissions: a stored tool the server no longer lists
is set to ``deny`` (its row is kept). Without ``--write`` nothing is written. Nous reads the rows at startup, with
``NOUS_MCP_CLIENT_ENABLED`` on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from nous.config import Settings
from nous.mcp_client.bridge import TOOLS_PER_SERVER, prepare_tool, safe_error, serialize_schema
from nous.mcp_client.config import ServerConfig, ToolConfig, parse_host_list, url_host
from nous.mcp_client.registry import McpRegistry
from nous.storage.database import Database

_LIST_TIMEOUT_SECONDS = 60


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m nous.mcp_client.seed", description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="the server's name in its tools' names (mcp__<name>__<tool>)")
    parser.add_argument("--url", required=True, help="the server's streamable-HTTP endpoint (https, no credentials)")
    parser.add_argument("--allow", default="", help="comma list of the server's tool names to allow; others are denied")
    parser.add_argument("--background", action="store_true", help="make the server available to background work")
    parser.add_argument("--write", action="store_true", help="store the server and the permissions")
    return parser.parse_args(argv)


async def seed(args: argparse.Namespace, settings: Settings) -> int:
    """List the server's tools, print them and, with ``--write``, store them. Returns the exit status."""
    from nous.mcp_client.transport import BlockedAddress, open_session

    try:
        server = ServerConfig(name=args.name, url=args.url, background=args.background)
    except ValidationError as exc:
        for error in exc.errors():  # the message only: the error's input would repeat the URL
            print(f"error: {error['loc'][0]}: {error['msg']}", file=sys.stderr)
        return 2
    allowed = {tool.strip() for tool in args.allow.split(",") if tool.strip()}
    try:
        async with asyncio.timeout(_LIST_TIMEOUT_SECONDS):
            async with open_session(
                server.url,
                parse_host_list(settings.mcp_client_allow_private_hosts),
                max_response_bytes=settings.mcp_client_max_response_bytes,
            ) as session:
                tools = await session.list_tools()
    except BlockedAddress as exc:  # the guard's own words: a host name at most, never a query string
        print(f"error: the address guard refused the server: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: could not list the server's tools: {safe_error(exc)}", file=sys.stderr)
        return 1
    rows: list[ToolConfig] = []
    registered: set[str] = set()  # what start() would register, in the same order
    for tool in sorted(tools, key=lambda t: t.name):
        permission = "allow" if tool.name in allowed else "deny"
        description = tool.description or ""
        prepared = prepare_tool(server.name, tool.name, description, tool.inputSchema)
        note = f"skipped at startup: {prepared.problem}" if prepared.problem else ""
        if permission == "allow" and not note and prepared.name is not None:
            if prepared.name in registered:
                note = "skipped at startup: a tool of that name is already registered"
            elif len(registered) >= TOOLS_PER_SERVER:
                note = f"skipped at startup: over the {TOOLS_PER_SERVER}-tool cap"
            else:
                registered.add(prepared.name)
        print(f"{permission:<5}  {prepared.name or repr(tool.name[:80])}{f'  ({note})' if note else ''}")
        if prepared.schema is not None:
            print(f"       description: {prepared.schema['description']}")
            print(f"       input schema: {serialize_schema(prepared.schema)}")
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
    pin = f"{server.name}={url_host(server.url)}"
    print(f"Pin entry, for NOUS_MCP_CLIENT_PINNED_SERVERS on the host (a comma list; no pin, no tools): {pin}")
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
        f"Stored server {server.name!r}: {len(registered)} tool(s) will register, "
        f"{sum(1 for row in rows if row.permission == 'deny')} denied. Nous reads them at its next start, with "
        f"NOUS_MCP_CLIENT_ENABLED=true and the pin entry {pin}."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m nous.mcp_client.seed``."""
    return asyncio.run(seed(_parse_args(argv), Settings()))


if __name__ == "__main__":
    sys.exit(main())
