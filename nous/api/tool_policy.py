"""What each execution context may do (harness Phase 2a).

One table, consulted once per call at AgentRunner._authorize_tool_call. Rows
are the INTENDED policy; in `warn` mode a deviation is logged and persisted as
`harness_context_policy_violation` and the call still runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from nous.api.execution_context import FOREGROUND_KINDS, ExecutionContext
from nous.api.tool_classes import tool_class
from nous.cognitive.execution_ledger import classify_side_effect

_ALL = frozenset({"none", "write", "external", "irreversible"})
_WORK = frozenset({"none", "write", "external"})
_LOCAL = frozenset({"none", "write"})


@dataclass(frozen=True)
class ContextPolicy:
    levels: frozenset[str]          # side-effect levels this context may use
    spawn: bool | frozenset[str]    # True: any spawn tool; a set: only these; False: none


CONTEXT_POLICY: Mapping[str, ContextPolicy] = MappingProxyType({
    "interactive": ContextPolicy(_ALL, spawn=True),
    "mcp": ContextPolicy(_ALL, spawn=True),
    "subtask": ContextPolicy(_WORK, spawn=False),
    "dag_node": ContextPolicy(_WORK, spawn=False),
    "scheduled": ContextPolicy(_WORK, spawn=False),
    "agent_action": ContextPolicy(_WORK, spawn=False),
    # Prod delivers DAG results by email through spawn_task from this turn:
    # that one spawn tool is allowed explicitly, the rest are not (roadmap §4).
    "dag_summary": ContextPolicy(_WORK, spawn=frozenset({"spawn_task"})),
    "heartbeat_triage": ContextPolicy(_LOCAL, spawn=False),
    # A check/callback may spawn only what it declared (check pipelines
    # declare heartbeat_check_create) -- see evaluate().
    "heartbeat_check": ContextPolicy(_WORK, spawn=False),
    "heartbeat_callback": ContextPolicy(_WORK, spawn=False),
    "background": ContextPolicy(_LOCAL, spawn=False),
})


def evaluate(ctx: ExecutionContext, tool_name: str, tool_input: Mapping[str, Any]) -> str | None:
    """None if ``ctx`` may make this call; otherwise the violation code:
    ``unclassified``, ``undeclared``, ``level:<level>``, ``spawn``, ``reenable``."""
    if ctx.kind in FOREGROUND_KINDS:
        return None
    policy = CONTEXT_POLICY[ctx.kind]
    cls = tool_class(tool_name)
    if cls is None:
        return "unclassified"
    declared = ctx.declared_tools
    if declared is not None and tool_name not in declared:
        return "undeclared"
    level = classify_side_effect(tool_name, dict(tool_input))
    if level not in policy.levels:
        return f"level:{level}"
    if cls.spawns and not (
        policy.spawn is True
        or (isinstance(policy.spawn, frozenset) and tool_name in policy.spawn)
        or (declared is not None and tool_name in declared)
    ):
        return "spawn"
    if (ctx.kind == "heartbeat_callback" and tool_name == "heartbeat_check_manage"
            and str(tool_input.get("action", "")).lower() == "enable"
            and ctx.check_name is not None
            and str(tool_input.get("name", "")).strip().lower() == ctx.check_name.lower()):
        return "reenable"
    return None
