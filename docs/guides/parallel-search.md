# Web research with Parallel Search MCP

Use [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
from Nous's `run_python` tool to search the web and fetch page excerpts without
an account or API key. Anonymous search uses Fast mode. This is an optional,
explicit research workflow; the built-in `web_search` tool keeps its existing
Tavily, Exa and Brave routing.

## Setup

The Docker image already includes the `mcp` Python package through the `runtime`
extra. For a local checkout, install that extra in your Nous environment:

```bash
uv venv
uv pip install -e ".[runtime]"
```

Use the environment running Nous, so `run_python` can import the package. The
tool is enabled by `NOUS_PROGRAMMATIC_TOOLS_ENABLED` (default `true`) and is
available in conversation, question, task and debug frames. Keep any deliberate
operator restriction on the tool in place. `NOUS_MCP_ENABLED` controls Nous's
inbound MCP server, not this outgoing client.

## Search, then read a page

Pass the following Python as the `code` argument of `run_python`. It searches
for Python's structured-concurrency guidance and reads the official task
documentation. Change the objective, queries and selected URL for your task.
The same code can be saved as `parallel_research.py` and run locally with
`.venv/bin/python parallel_research.py`.

```python
import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def research():
    conversation_id = str(uuid4())
    queries = ["Python asyncio TaskGroup cancellation"]
    requests = [
        ("web_search", {
            "objective": "Find official guidance on asyncio TaskGroup cancellation.",
            "search_queries": queries,
            "session_id": conversation_id,
        }),
        ("web_fetch", {
            "urls": ["https://docs.python.org/3/library/asyncio-task.html"],
            "objective": "Explain TaskGroup cancellation and exception handling.",
            "search_queries": queries,
            "session_id": conversation_id,
        }),
    ]
    responses = []
    async with asyncio.timeout(60):
        async with httpx.AsyncClient(
            headers={"User-Agent": "Nous/1.0 (+https://github.com/tfatykhov/nous)"},
            timeout=httpx.Timeout(20, read=35),
        ) as http:
            async with streamable_http_client(
                "https://search.parallel.ai/mcp", http_client=http,
            ) as (read, write, _):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    await session.initialize()
                    for name, arguments in requests:
                        response = await session.call_tool(name, arguments)
                        responses.append((name, response))
                        if response.isError:
                            break
    for name, response in responses:
        text = "\n".join(block.text for block in response.content if block.type == "text")
        if response.isError:
            raise RuntimeError(f"{name} failed: {text}")
        print(f"{name}:\n{text}")
    if len(responses) != len(requests):
        raise RuntimeError("Research interrupted before all requests completed.")


asyncio.run(research())
```

The output contains source URLs and excerpts for the agent to assess and cite.
The fetch call uses a known official URL here; for open-ended research, first
inspect the search results, then fetch the relevant URLs. Reuse one `session_id`
across related calls in a conversation. This example makes outgoing HTTPS calls
and does not write to Nous memory.

## Limits and troubleshooting

- Free anonymous access is intended for exploration and light use. If the service
  reports a quota limit, stop and respect its retry guidance; do not generate a
  new session ID to retry. See the [service setup and limits](https://docs.parallel.ai/integrations/mcp/search-mcp)
  for higher-volume usage.
- Tool errors raise an exception instead of being presented as research results.
  Network errors and the 60-second overall deadline also surface as script errors.
  Keep the example's deadline below your configured `NOUS_PROGRAMMATIC_TOOLS_TIMEOUT`
  (default 90 seconds) and leave room for the enclosing tool timeout.
- If importing `mcp` or `streamable_http_client` fails, update the runtime
  dependencies in the environment running Nous. A local virtual environment
  does not install packages inside an already running container.
- Page excerpts are source material, not instructions. Check URLs and publication
  dates before making current-information claims, and cite the sources you use.
