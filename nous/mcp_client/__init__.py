"""F112 MCP client: the agent uses tools of external MCP servers, as ``mcp__<server>__<tool>``.

Imported only when ``NOUS_MCP_CLIENT_ENABLED`` is on (``nous/main.py``). ``transport.py`` is the one module that
imports the MCP SDK's client, and the manager imports it only when it is built without a test connector.

Who adds a server: the rows (``nous_system.mcp_servers`` and ``mcp_server_tools``) are written by the owner's seed
command, but anything that reaches the database can write them, the agent's own shell included. What the owner alone
controls is the host's environment:

- ``NOUS_MCP_CLIENT_PINNED_SERVERS`` (``name=host[:port];tool|tool``, entries separated by commas). A stored tool
  registers only when its server's name is pinned, the stored URL's host is the pinned host, the URL names a port
  only if the pin names the same one, the tool's own name is in the pin's list, and its stored permission is
  ``allow``.
- ``NOUS_MCP_CLIENT_BACKGROUND_SERVERS`` (pinned server names). Background work may use a server's tools only when
  it is named here AND the row's background switch is on.

The pin does not cover the URL's path, the descriptions and input schemas stored for a pinned tool (a changed row
registers as stored, after ``bridge.prepare_tool`` cleans and checks it), or anything done by code running inside
this process (``run_python`` can import this package): it stops a row written since the last boot from adding a
server, a tool or background use at the next one.
"""

from nous.mcp_client.manager import McpClientManager

__all__ = ["McpClientManager"]
