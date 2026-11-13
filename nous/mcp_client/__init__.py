"""F112 MCP client: the agent uses tools of external MCP servers, as ``mcp__<server>__<tool>``.

Imported only when ``NOUS_MCP_CLIENT_ENABLED`` is on (``nous/main.py``, and ``nous/api/rest.py`` for the routes).
``transport.py`` is the one module that imports the MCP SDK's client, and the manager imports it only when it is built
without a test connector.

Who adds a server: the owner, with a request to the ``/integrations/mcp`` routes (``nous/api/mcp_client_routes.py``,
behind the proxy's secret header) or with the seed command on the host. That request is the approval: the change
applies at once (``McpClientManager.reload``). The proxy secret protects against the LAN, not against code running in
the Nous process, which could call the routes or write the rows (``nous_system.mcp_servers`` and
``mcp_server_tools``) itself, as could anything else that reaches the database. The hard limit, for an owner who wants
one, is the host's environment, which neither the routes nor the rows can change:

- ``NOUS_MCP_CLIENT_PINNED_SERVERS`` (optional; ``name=host[:port];tool|tool``, entries separated by commas). Set, a
  stored tool registers only when its server's name is pinned, the stored URL's host is the pinned host, the URL
  names a port only if the pin names the same one, and the tool's own name is in the pin's list. Empty, every
  ``allow`` tool of an enabled server registers.
- ``NOUS_MCP_CLIENT_BACKGROUND_SERVERS`` (optional; server names). Set, background work may use a server's tools only
  when it is named here AND the row's background switch is on. Empty, the row's switch alone decides.

The pin does not cover the URL's path, the descriptions and input schemas stored for a pinned tool (a changed row
registers as stored, after ``bridge.prepare_tool`` cleans and checks it), or anything done by code running inside
this process (``run_python`` can import this package).
"""

from nous.mcp_client.manager import McpClientManager

__all__ = ["McpClientManager"]
