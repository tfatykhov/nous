"""Registers the browser_task agent tool (F100). Gated on NOUS_BROWSER_TASK_ENABLED in main.py."""

from __future__ import annotations

import logging
from typing import Any

from nous.api.tools import ToolDispatcher, _tool_error
from nous.browser.loop import UnsupportedModelError, run_browser_task, validate_model

logger = logging.getLogger(__name__)

_BROWSER_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Do a task inside web pages (click, type, read pages that need JavaScript) with a separate, "
        "isolated browser agent. You get back only a short text summary of what it found or did, "
        "plus a saved screenshot path, never the page content itself. Navigation is limited to "
        "allowed_domains. It stops and returns needs_confirmation, without acting, before logging in, "
        "paying, purchasing, accepting terms, sending messages or deleting anything; it has no "
        "credentials. Slow and costly: prefer web_fetch or web_search for reading a page or finding sources."
    ),
    "properties": {
        "goal": {"type": "string", "description": "What the browser agent should accomplish, stated fully."},
        "allowed_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Hosts it may visit, e.g. ['example.com']; subdomains are included.",
        },
        "start_url": {"type": "string", "description": "Optional page to open first; must be on allowed_domains."},
        "max_steps": {
            "type": "integer",
            "description": "Model turns before it gives up (default 30, capped by NOUS_BROWSER_TASK_MAX_STEPS).",
            "minimum": 1,
        },
    },
    "required": ["goal", "allowed_domains"],
}


def register_browser_tools(dispatcher: ToolDispatcher, settings: Any) -> None:
    """Register browser_task. An unsupported NOUS_BROWSER_TASK_MODEL is logged now and refused on every call."""
    try:
        validate_model(settings.browser_task_model)
    except UnsupportedModelError as e:
        logger.error("%s browser_task calls will fail until it is fixed.", e)

    async def _browser_task(
        goal: str,
        allowed_domains: list[str],
        start_url: str | None = None,
        max_steps: int = 30,
    ) -> dict[str, Any]:
        result = await run_browser_task(goal, allowed_domains, start_url, max_steps, settings=settings)
        text = result.render()
        if result.status in ("error", "timeout", "busy"):
            return _tool_error(text)
        return {"content": [{"type": "text", "text": text}]}

    dispatcher.register("browser_task", _browser_task, _BROWSER_TASK_SCHEMA)
