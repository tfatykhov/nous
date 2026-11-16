"""F115: where the code itself offers a tool to background work, and in words why not where it does not.

The tools screen shows, per tool, the kinds of background work the code does not offer it to, whatever the owner
sets: a tool switched on there stays not offered. ``why_not_offered`` follows the steps of
``AgentRunner._offered_tools`` that hold for a kind of turn as such (the subtask exclusion, the triage exclusion, the
chains' own tool set and the MCP floor); the steps that depend on one turn (its frame, a check's own tool list, a
censor) are left out. ``tests/test_autonomy_offered.py`` holds it equal to ``_offered_tools`` for every place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from nous.api import tool_policy
from nous.api.execution_context import ExecutionContext
from nous.api.runner import _SUBTASK_EXCLUDED_TOOLS
from nous.brain.intentions import AUTHORITY_INTERNAL, AUTHORITY_OWNER

__all__ = ["PLACES", "Place", "context_for", "why_not_offered"]


@dataclass(frozen=True)
class Place:
    """One kind of background work as the screen names it: the turn's kind, its authority and whether it runs as a
    subtask (``is_subtask``, which the runner is given)."""

    key: str
    words: str
    kind: str
    authority: str
    is_subtask: bool


PLACES: tuple[Place, ...] = (
    Place("chain_decisions", "Chain decisions", "continuation", AUTHORITY_INTERNAL, False),
    Place("chain_work", "Work in a chain", "subtask", AUTHORITY_INTERNAL, True),
    Place("tasks", "Background tasks", "subtask", AUTHORITY_OWNER, True),
    Place("workflow_steps", "Workflow steps", "dag_node", AUTHORITY_OWNER, True),
    Place("schedules", "Scheduled tasks", "scheduled", AUTHORITY_OWNER, True),
    Place("checks", "Heartbeat checks", "heartbeat_check", AUTHORITY_OWNER, True),
    Place("triage", "Heartbeat triage", "heartbeat_triage", AUTHORITY_OWNER, True),
)

_PLACEHOLDER_ROOT = uuid.UUID(int=0)


def context_for(place: Place) -> ExecutionContext:
    """A turn of ``place`` as the runner sees it (a chain decision names an intention and its root)."""
    lineage = {"intention_id": _PLACEHOLDER_ROOT, "root_intention_id": _PLACEHOLDER_ROOT}
    return ExecutionContext(
        kind=place.kind,  # type: ignore[arg-type]
        authority=place.authority,
        **(lineage if place.kind == "continuation" else {}),
    )


def why_not_offered(place: Place, tool: str, *, lineage_shell: bool) -> str | None:
    """Why the code does not offer ``tool`` to ``place``, in one sentence; None when it does."""
    ctx = context_for(place)
    if place.is_subtask and tool in _SUBTASK_EXCLUDED_TOOLS:
        return "Work started in the background may not start more work this way."
    if place.kind == "heartbeat_triage" and (
        tool_policy.is_spawn_tool(tool) or tool_policy.heartbeat_resolve_violation(tool)
    ):
        return "Triage may not start work or resolve decisions."
    if place.authority == AUTHORITY_INTERNAL and not tool_policy.internal_only_allowed(
        tool, ctx=ctx, lineage_shell=lineage_shell
    ):
        return "A chain keeps to a fixed set of tools that act inside Nous."
    violation = tool_policy.mcp_client_violation(place.kind, tool)
    if violation == "mcp_unregistered":
        return "Its server is not connected."
    if violation is not None:
        return "Its server is not available to background work."
    return None
