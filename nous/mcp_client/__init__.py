"""F112 MCP client: the agent uses tools of external MCP servers, as ``mcp__<server>__<tool>``.

Imported only when ``NOUS_MCP_CLIENT_ENABLED`` is on (``nous/main.py``). ``transport.py`` is the one module that
imports the MCP SDK's client, and the manager imports it only when it is built without a test connector.

Who adds a server: the rows (``nous_system.mcp_servers`` and ``mcp_server_tools``) are written by the owner's seed
command, but anything that reaches the database can write them, the agent's own shell included. What the owner alone
controls is the pin, ``NOUS_MCP_CLIENT_PINNED_SERVERS`` in the host's environment: a server's tools register only
when its name and its stored URL's host match a pin. The pin stops a row written since the last boot from taking
effect at the next one. It does not stop code the agent runs inside this process (``run_python`` can import this
package), which is a residual risk of every in-process client.
"""

from nous.mcp_client.manager import McpClientManager

__all__ = ["McpClientManager"]
