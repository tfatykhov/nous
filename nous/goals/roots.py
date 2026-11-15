"""F114: the root of a goal session. A session is a continuation chain the goal driver starts."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import intentions
from nous.brain.intentions import IntentionSpec
from nous.storage.models import Subtask


class GoalsUnavailable(RuntimeError):
    """Intentions or continuation is off: a ``continue`` root would never be claimed."""


async def create_session_root(
    heart: Any,
    settings: Any,
    *,
    goal: Any,
    session_n: int,
    subtask_id: UUID,
    task: str,
    session: AsyncSession | None = None,
) -> Subtask:
    """Create the first subtask of a goal session and, in the same transaction, its root intention.

    The intention has internal authority, so ``resolve_wake_policy`` gives it ``continue`` and the row is
    ``internal_only``: the whole lineage runs with the lineage tool set and its results go back to the
    continuation, never to the chat. The wake policy is not passed but resolved; ``tests/test_goals_roots.py``
    pins what it resolves to. The task text is built by the caller. With ``session`` both rows are written in
    the caller's transaction and not committed (the goal driver attaches the root to its session in the same one).
    """
    if not intentions.enabled(settings) or getattr(settings, "continuation_enabled", False) is not True:
        raise GoalsUnavailable("goal sessions need NOUS_INTENTIONS_ENABLED and NOUS_CONTINUATION_ENABLED")
    spec = intentions.with_bounds(
        IntentionSpec(
            intent=intentions.intent_line(f"Goal G-{goal.short_id:04d}, session {session_n}: {goal.title}"),
            origin_kind=intentions.ORIGIN_GOAL,
            origin_channel=goal.origin_channel,
            origin_authority=intentions.AUTHORITY_INTERNAL,
        ),
        settings,
    )
    timeout = min(settings.subtask_default_timeout, settings.subtask_max_timeout)
    return await heart.subtasks.create(
        task=task,
        timeout=timeout,
        notify=False,
        model=settings.background_model,
        metadata={"goal_id": str(goal.id), "goal_session": session_n},
        subtask_id=subtask_id,
        intention=spec,
        session=session,
    )
