"""What each execution context may do (harness Phase 2a).

One table, consulted once per call at AgentRunner._authorize_tool_call. Rows
are the INTENDED policy; in `warn` mode a deviation is logged and persisted as
`harness_context_policy_violation` and the call still runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nous.api.execution_context import FOREGROUND_KINDS, ExecutionContext
from nous.api.tool_classes import is_compensable_call, tool_class
from nous.cognitive.execution_ledger import classify_side_effect

_ALL = frozenset({"none", "write", "external", "irreversible"})
_WORK = frozenset({"none", "write", "external"})
_LOCAL = frozenset({"none", "write"})


@dataclass(frozen=True)
class ContextPolicy:
    levels: frozenset[str]  # side-effect levels this context may use
    spawn: bool | frozenset[str]  # True: any spawn tool; a set: only these; False: none


CONTEXT_POLICY: Mapping[str, ContextPolicy] = MappingProxyType(
    {
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
        # F099: Nous's own turn on a background result. Local levels only, and the
        # two spawn tools; _offered_tools and the strict path in
        # _authorize_tool_call narrow it further (the policy is the floor).
        "continuation": ContextPolicy(_LOCAL, spawn=frozenset({"spawn_task", "dag_create"})),
        # F099: one owner-approved call, declared_tools=(tool,). Levels wide because the
        # proposed call is by definition outward or a denylisted local tool.
        "approved_action": ContextPolicy(_ALL, spawn=True),
    }
)

# F099 section 4.4: tools an internal_only turn may NOT use although their class is
# none or write. They schedule, persist policy, reach the host, or resolve the Brain's
# own records.
INTERNAL_ONLY_DENYLIST: frozenset[str] = frozenset(
    {
        "schedule_task",
        "heartbeat_check_create",
        "heartbeat_check_manage",
        "create_censor",
        "learn_skill",
        "store_identity",
        "complete_initiation",
        "dag_manage",
        "push_surface",
        "compose_surface",
        "bash",
        "run_python",
        "spawn_sync",
        "resolve_decision",
        "resolve_decisions",
    }
)
# Offered to continuation turns only; removed when ctx.spawn_blocked.
INTERNAL_ONLY_SPAWN_TOOLS: frozenset[str] = frozenset({"spawn_task", "dag_create"})
# Per-call rules (path and lineage), evaluated at dispatch.
INTERNAL_ONLY_CHECKED_TOOLS: frozenset[str] = frozenset({"write_file", "cancel_task"})
# Logged with their root when called from a lineage (spec section 9).
INTERNAL_ONLY_LOGGED_TOOLS: frozenset[str] = frozenset({"web_fetch", "web_search"})


def internal_only_allowed(name: str, *, ctx: ExecutionContext) -> bool:
    """Whether an internal_only context may be OFFERED ``name``.

    Fails closed on an unclassified tool: a tool nobody classified is not
    allowed, so a new tool is denied to a lineage until someone adds it on purpose.
    """
    cls = tool_class(name)
    if cls is None or cls.side_effect not in ("none", "write"):
        return False
    if name in INTERNAL_ONLY_DENYLIST:
        return False
    if name in INTERNAL_ONLY_SPAWN_TOOLS:
        return ctx.kind == "continuation" and not ctx.spawn_blocked
    return True


def _write_path_allowed(ctx: ExecutionContext, tool_input: Mapping[str, Any], workspace_dir: str) -> bool:
    """A write_file target is inside ``<workspace_dir>/intentions/<root_id>/`` once resolved.

    Resolves the way the tool does (``builtin_tools._validate_path``): relative to the
    workspace, symlinks and ``..`` followed. A lineage with no root id, or a call with
    no usable path, is refused.
    """
    path = tool_input.get("path")
    if ctx.root_intention_id is None or not isinstance(path, str) or not path.strip():
        return False
    try:
        workspace = Path(workspace_dir).resolve()
        root_dir = workspace / "intentions" / str(ctx.root_intention_id)
        target = Path(path).resolve() if Path(path).is_absolute() else (workspace / path).resolve()
    except (OSError, ValueError):  # a NUL byte, an unresolvable name
        return False
    return target.is_relative_to(root_dir)


def internal_only_call_violation(
    ctx: ExecutionContext, name: str, tool_input: Mapping[str, Any], *, workspace_dir: str
) -> str | None:
    """The per-call rules of an internal_only turn, for a tool that WAS offered.

    ``"external"``: ``classify_side_effect`` rates the call external (or
    irreversible), such as a URL in run_python. ``"write_path"``: write_file outside
    its root's directory. ``"foreign_cancel"``: cancel_task with an id that is not a
    UUID. The cancel_task lineage check needs the target's row, so it lives in the
    handler (``tools.py``); this sync function only rejects what needs no read.
    """
    if classify_side_effect(name, dict(tool_input)) in ("external", "irreversible"):
        return "external"
    if name == "write_file" and not _write_path_allowed(ctx, tool_input, workspace_dir):
        return "write_path"
    if name == "cancel_task":
        try:
            uuid.UUID(str(tool_input.get("task_id", "")))
        except (TypeError, ValueError):
            return "foreign_cancel"
    return None


def undoable_violation(ctx: ExecutionContext, tool_name: str, tool_input: Mapping[str, Any]) -> str | None:
    """``"not_compensable"`` when an ``undoable`` context makes a call that
    has a side effect and cannot be undone, else None.

    Separate from :func:`evaluate` because it is a safety invariant, not a
    policy preference: the runner enforces it even when the context policy
    is ``off`` or ``warn`` -- an undoable claim with no undo is false."""
    if not ctx.undoable:
        return None
    if classify_side_effect(tool_name, dict(tool_input)) == "none":
        return None
    if not is_compensable_call(tool_name, tool_input):
        return "not_compensable"
    return None


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
    # Phase 2.8: check undoability BEFORE the spawn gate so that a tool that
    # both spawns and lacks a compensator (e.g. dag_create) returns
    # "not_compensable" — the true constraint — rather than "spawn", which
    # masks the undoability violation and prevents force_block from activating
    # in warn mode.
    if undoable_violation(ctx, tool_name, tool_input) is not None:
        return "not_compensable"
    if cls.spawns and not (
        policy.spawn is True
        or (isinstance(policy.spawn, frozenset) and tool_name in policy.spawn)
        or (declared is not None and tool_name in declared)
    ):
        return "spawn"
    # a callback may not resurrect the check that triggered it: neither by
    # enabling it nor by creating one under its name (after deleting it)
    own = (ctx.check_name or "").strip().lower()
    if ctx.kind == "heartbeat_callback" and own:
        named = str(tool_input.get("name", "")).strip().lower() == own
        action = str(tool_input.get("action", "")).strip().lower()
        if named and (
            (tool_name == "heartbeat_check_manage" and action == "enable") or tool_name == "heartbeat_check_create"
        ):
            return "reenable"
    return None
