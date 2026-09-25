"""Idempotency keys for external sends (harness Phase 2b).

Computed in the runner, where the execution context and the call's arguments
meet. The key names the logical send -- who it goes to, within which unit of
work -- not its wording: a retry re-runs the objective and rewrites subject,
body and caption, so those are never part of the key. An intentional second
message to the same recipients carries a `send_label`.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from typing import Any

from nous.api.execution_context import ExecutionContext
from nous.api.tool_classes import tool_class

# The DAG summary turn's session: `dag-summary-{dag.id.hex}-g{delivery_generation}`.
# Its own sends and the subtasks it spawns share it as their scope.
SUMMARY_SESSION_PREFIX = "dag-summary-"


def is_keyed_tool(name: str) -> bool:
    """External sends: the calls a retry can duplicate at a recipient."""
    cls = tool_class(name)
    return name != "bash" and cls is not None and cls.side_effect == "external"


def _scope(ctx: ExecutionContext) -> str | None:
    """The unit of work a send belongs to, or None (unkeyed: an operator
    re-send from chat, a check tick, triage and generic background turns)."""
    if ctx.kind == "dag_node" and ctx.dag_id is not None and ctx.dag_node_name:
        return f"dag:{ctx.dag_id}:{ctx.dag_node_name}"
    if ctx.kind == "dag_summary" and ctx.session_id:
        return ctx.session_id
    if ctx.parent_session_id and ctx.parent_session_id.startswith(SUMMARY_SESSION_PREFIX):
        return ctx.parent_session_id
    if ctx.kind == "heartbeat_callback" and ctx.check_name and ctx.run_id:
        return f"callback:{ctx.check_name}:{ctx.run_id}"
    if ctx.kind in ("subtask", "scheduled", "agent_action") and ctx.subtask_id is not None:
        return f"subtask:{ctx.subtask_id}"
    return None


def normalize_recipients(value: Any) -> list[str]:
    """A recipient field (a string or a list) as the stripped addresses it names.

    The ONE definition: ``send_email`` sends to exactly these, and the
    idempotency key is built from exactly these, so a relaunch that passes
    ``["a, b"]`` where the first attempt passed ``"a, b"`` is the same send.
    Only a comma separates (a list element may hold several).
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = []
        for v in value:
            items.extend(str(v).split(","))
    else:
        items = [str(value)]
    return [a.strip() for a in items if a and a.strip()]


def canonical_recipients(value: Any) -> list[str]:
    """Sorted, lowercased, de-duplicated recipients -- ``normalize_recipients``."""
    return sorted({a.lower() for a in normalize_recipients(value)})


def _material(tool_name: str, args: Mapping[str, Any], default_chat_id: str | None) -> str | None:
    label = str(args.get("send_label") or "").strip()
    if tool_name == "send_email":
        to = ",".join(canonical_recipients(args.get("to")) + canonical_recipients(args.get("cc")))
        return f"{to}|{label}"
    if tool_name == "send_file":
        chat = str(args.get("chat_id") or default_chat_id or "")
        return f"{chat}|{os.path.basename(str(args.get('file_path') or ''))}|{label}"
    return None


def idempotency_key(
    ctx: ExecutionContext, tool_name: str, tool_input: Mapping[str, Any],
    *, default_chat_id: str | None = None,
) -> str | None:
    """``{scope}:{digest}``, or None when this call is not keyed."""
    if not is_keyed_tool(tool_name):
        return None
    scope = _scope(ctx)
    material = _material(tool_name, tool_input, default_chat_id)
    if scope is None or material is None:
        return None
    return f"{scope}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"
