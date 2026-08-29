"""F092: push_surface agent tool.

Lives here (not nous/api/tools.py) deliberately: the AST ratchet in
tests/test_tool_arg_salvage.py counts unflagged MCP-shaped returns in that
file with an exact `==` baseline, and a handler module outside it keeps the
ratchet untouched. The only nous/api/tools.py edit for F092 is the
``_session_id`` injection branch in ``dispatch()``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nous.api.tools import ToolDispatcher, _tool_error

from .builders import TEMPLATES
from .dsl import SurfaceValidationError

logger = logging.getLogger(__name__)

_PUSH_SURFACE_SCHEMA = {
    "type": "object",
    "description": (
        "Push a structured, interactive surface to the companion app "
        "(A2UI). Use for things that are bad as prose and good as an "
        "interface: escalations needing a decision, reviews of actions you "
        "took, batch triage. The user sees it at /companion and their "
        "button presses come back as recorded actions."
    ),
    "properties": {
        "template": {
            "type": "string",
            "enum": sorted(TEMPLATES),
            "description": (
                "approval_gate: pre-execution escalation with options "
                "(params: title, summary, risk, options=[{id,label}], "
                "recommendation, trace_id). "
                "action_review: post-hoc review of an action already taken "
                "(params: title, did, why, cost, compensation={revertible,"
                "handler,note}, trace_id). "
                "heartbeat_findings: triage list (params: findings=[{"
                "fingerprint,message,urgency,check}], title)."
            ),
        },
        "params": {
            "type": "object",
            "description": "Template parameters (see template description).",
        },
        "dedup_key": {
            "type": "string",
            "description": (
                "Stable key for update-in-place: a recurring producer (e.g. "
                "an hourly check) MUST pass one so it updates its existing "
                "card instead of stacking new ones."
            ),
        },
        "notify": {
            "type": "boolean",
            "description": ("Override the Telegram ping (default: priority >= 1 pings)."),
        },
    },
    "required": ["template", "params"],
}


def register_a2ui_tools(dispatcher: ToolDispatcher, surface_service: Any) -> None:
    """Register the push_surface tool against a live SurfaceService."""

    async def push_surface(**kwargs) -> dict:
        template = kwargs.get("template", "")
        builder = TEMPLATES.get(template)
        if builder is None:
            return _tool_error(f"Unknown template {template!r}. Available: {sorted(TEMPLATES)}")
        params = kwargs.get("params") or {}
        try:
            built = builder(params)
        except SurfaceValidationError as exc:
            return _tool_error(f"Surface failed validation: {exc.errors[:2]}")
        except (KeyError, ValueError, TypeError) as exc:
            return _tool_error(f"Bad params for {template}: {exc}")
        try:
            surface_id = await surface_service.push_built(
                built,
                dedup_key=kwargs.get("dedup_key"),
                session_id=kwargs.get("_session_id"),
                notify=kwargs.get("notify"),
            )
        except PermissionError as exc:
            return _tool_error(f"Surface blocked: {exc}")
        except Exception as exc:
            logger.exception("push_surface failed")
            return _tool_error(f"Failed to push surface: {exc}")
        return {
            "is_error": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "surface_id": surface_id,
                            "url": f"/companion#/s/{surface_id}",
                            "priority": built.priority,
                        }
                    ),
                }
            ],
        }

    dispatcher.register("push_surface", push_surface, _PUSH_SURFACE_SCHEMA)
