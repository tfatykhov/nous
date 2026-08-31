"""F031: Censor action executor — runs prescribed read-only actions.

When a warn censor with trigger_action fires, this module validates
and executes the action via Heart methods directly. No ToolDispatcher
dependency — the cognitive layer stays decoupled from the runtime.

Only read-only operations are permitted.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from nous.heart.heart import Heart

logger = logging.getLogger(__name__)

# Allowed read-only tools that censors can trigger.
ALLOWED_TOOLS: set[str] = {
    "recall",
    "recall_recent",
    "search_facts",
    "search_episodes",
    "search_procedures",
    "list_tasks",
}


class CensorActionExecutor:
    """Validates and executes censor trigger_action payloads."""

    def __init__(self, heart: Heart) -> None:
        self._heart = heart

    async def execute(
        self,
        trigger_action: dict[str, Any],
        session: AsyncSession | None = None,
    ) -> str | None:
        """Execute a trigger_action and return formatted results.

        Returns None if the action is invalid or disallowed.
        Returns a formatted string of results on success.
        """
        if not isinstance(trigger_action, dict):
            logger.warning("Censor trigger_action is not a dict: %s", type(trigger_action))
            return None

        tool = trigger_action.get("tool")
        if not tool or tool not in ALLOWED_TOOLS:
            logger.warning("Censor trigger_action tool not allowed: %s", tool)
            return None

        args = trigger_action.get("args", {})
        if not isinstance(args, dict):
            logger.warning("Censor trigger_action args is not a dict: %s", type(args))
            return None

        try:
            return await self._dispatch(tool, args, session)
        except Exception:
            logger.exception("Censor action execution failed for tool=%s", tool)
            return None

    async def _dispatch(
        self,
        tool: str,
        args: dict[str, Any],
        session: AsyncSession | None,
    ) -> str:
        """Route to the appropriate Heart method and format results."""
        handler = {
            "recall": self._run_recall,
            "recall_recent": self._run_recall_recent,
            "search_facts": self._run_search_facts,
            "search_episodes": self._run_search_episodes,
            "search_procedures": self._run_search_procedures,
            "list_tasks": self._run_list_tasks,
        }.get(tool)
        if handler:
            return await handler(args, session)
        return ""

    async def _run_recall(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)  # Cap at 10
        results = await self._heart.recall(query, limit=limit, session=session)
        if not results:
            return f"[Censor recall: no results for '{query}']"
        lines = [f"[Censor recall for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. [{r.type}] {r.summary} (score: {r.score:.3f})")
        return "\n".join(lines)

    async def _run_recall_recent(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        hours = min(int(args.get("hours", 24)), 168)  # Cap at 1 week
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.list_episodes(limit=limit, hours=hours, session=session)
        if not results:
            return f"[Censor recall_recent: no episodes in last {hours}h]"
        lines = [f"[Censor recall_recent: {len(results)} episodes in last {hours}h]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.summary}")
        return "\n".join(lines)

    async def _run_search_facts(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.search_facts(query, limit=limit, session=session)
        if not results:
            return f"[Censor search_facts: no results for '{query}']"
        lines = [f"[Censor search_facts for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.content}")
        return "\n".join(lines)

    async def _run_search_episodes(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.search_episodes(query, limit=limit, session=session)
        if not results:
            return f"[Censor search_episodes: no results for '{query}']"
        lines = [f"[Censor search_episodes for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.summary}")
        return "\n".join(lines)

    async def _run_search_procedures(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.search_procedures(query, limit=limit, session=session)
        if not results:
            return f"[Censor search_procedures: no results for '{query}']"
        lines = [f"[Censor search_procedures for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.name}: {r.description}")
        return "\n".join(lines)

    async def _run_list_tasks(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        status = args.get("status")
        subtasks = await self._heart.subtasks.list(status=status, limit=10)
        schedules = await self._heart.schedules.list(active_only=True, limit=10)
        if not subtasks and not schedules:
            return "[Censor list_tasks: no active tasks or schedules]"
        lines = ["[Censor list_tasks]"]
        if subtasks:
            for st in subtasks:
                lines.append(f"  - [{st.status}] {st.task[:80]}")
        if schedules:
            for sc in schedules:
                lines.append(f"  - [schedule] {sc.task[:80]} ({sc.cron_expr})")
        return "\n".join(lines)


async def gate_subtask_task(heart: Any, task: str) -> tuple[str | None, str]:
    """F078 (R3) spawn gate, shared: censor-check a subtask's task text at
    creation time. Subtasks are non-interactive, so censor checks are
    skipped during execution (pre_turn) — this gate is the ONLY censor
    enforcement the background path gets. Tiers:

      abort / refuse -> REJECT (the autonomous-exfil path), unless an F031
                        unblock_pattern downgrades the refuse to steer.
      steer          -> do NOT reject; inject the directive into the task.

    Returns ``(rejection_message, effective_task)``: rejection_message is
    None when the subtask may proceed, and effective_task carries any
    injected steer guidance. Extracted from spawn_task (F092.2) so every
    path that creates a subtask directly — spawn_task, app.act — runs the
    SAME gate; a second caller must never re-derive this control flow.

    Fails OPEN with a WARN, matching the original gate: a censor-infra
    error must not brick every background task, but it is a silent
    enforcement gap and is logged loudly.
    """
    try:
        steer_directives: list[str] = []
        matches = await heart.check_censors(task)
        for match in matches:
            if match.action in ("abort", "refuse"):
                # F031: refuse may downgrade to steer via unblock_pattern.
                unblocked = False
                if match.trigger_action and match.unblock_pattern:
                    executor = CensorActionExecutor(heart)
                    action_result = await executor.execute(match.trigger_action)
                    if action_result:
                        import re

                        try:
                            if re.search(match.unblock_pattern, action_result, re.IGNORECASE):
                                unblocked = True
                        except re.error:
                            pass
                if not unblocked:
                    reason = match.reason or match.trigger_pattern
                    msg = f"Subtask rejected by censor: {reason}"
                    if match.action_instruction:
                        msg += f"\n{match.action_instruction}"
                    return msg, task
                directive = match.action_instruction or match.reason
                if directive:
                    steer_directives.append(directive)
            elif match.action == "steer":
                logger.info("Censor STEER on subtask creation: %s", match.trigger_pattern)
                directive = match.action_instruction or match.reason
                if directive:
                    steer_directives.append(directive)
        if steer_directives:
            task = task + "\n\n## Active Guidance\n" + "\n".join(
                f"- {d}" for d in steer_directives
            )
    except Exception:
        logger.warning("Censor check failed during subtask gate, proceeding", exc_info=True)
    return None, task
