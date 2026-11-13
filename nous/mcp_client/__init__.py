"""F112 MCP client: the agent uses tools of external MCP servers, as ``mcp__<server>__<tool>``.

Imported only when ``NOUS_MCP_CLIENT_ENABLED`` is on (``nous/main.py``). ``transport.py`` is the one module that
imports the MCP SDK's client, and the manager imports it only when it is built without a test connector.
"""

from nous.mcp_client.manager import McpClientManager

__all__ = ["McpClientManager"]
