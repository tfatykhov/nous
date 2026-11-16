"""F114: what a goal session's chain knows about its goal, and where its reports go.

A goal session's root is its first subtask's intention (``nous.goals.roots``), so a root belongs to a goal exactly
when its source is the ``first_subtask_id`` of a goal session (the key the start writes before the subtask exists,
so the lookup holds before the session's ``root_id`` is written). Two hooks use it, both bound by ``main.py`` only
with ``NOUS_GOALS_ENABLED`` on:

- ``GoalChain.turn_context`` (``ContinuationRunner.set_goal_turns``): a decision turn of such a root gets the goal
  block (``charter.render_goal_block``) and the goal tools (``nous.goals.tools``); any other root gets None;
- ``report_channel`` (``continuation.set_goal_report_channel``): a REPORT of such a root goes to the goal's channel
  (``goal:<id>``), which the driver claims into the journal, instead of the owner's chat.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Text, and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import intentions
from nous.goals import charter, evidence, spend
from nous.goals.store import GoalStore, moved_steps
from nous.goals.tools import Executor, GoalSessionRef, make_goal_tools
from nous.storage.database import Database
from nous.storage.models import Goal, GoalSession, Intention

if TYPE_CHECKING:
    from nous.goals.grants import GoalToolGrants


@dataclass(frozen=True)
class GoalTurn:
    """What a decision turn of a goal session's root is given: the goal block for its prompt, and its extra tools."""

    block: str
    tools: dict[str, tuple[dict[str, Any], Executor]]


def _session_of_root(agent_id: str, root_id: UUID) -> Any:
    """The goal session whose first subtask is the source of the root ``root_id`` (a root: its own root)."""
    root = Intention
    return (
        select(GoalSession)
        .join(
            root,
            and_(
                root.agent_id == agent_id,
                root.id == root_id,
                root.root_id == root.id,
                root.source_kind == intentions.SOURCE_SUBTASK,
                root.source_id == cast(GoalSession.first_subtask_id, Text),
            ),
        )
        .where(GoalSession.agent_id == agent_id)
    )


async def report_channel(session: AsyncSession, agent_id: str, root_id: UUID) -> str | None:
    """``goal:<id>`` when ``root_id`` is the root of a session of that goal, else None. One read, in the caller's
    transaction (``continuation._insert_report_row``)."""
    row = (await session.execute(_session_of_root(agent_id, root_id))).scalar_one_or_none()
    return f"goal:{row.goal_id}" if row is not None else None


class GoalChain:
    """The goals' side of a decision turn (``turn_context``). ``tool_grants`` is the runner's view of the goals' tool
    grants (F115 per-goal tools, main.py with the flag on), read again for the root at the start of each of its
    decision turns."""

    def __init__(
        self, database: Database, settings: Any, store: GoalStore, *, tool_grants: GoalToolGrants | None = None
    ) -> None:
        self._db = database
        self._settings = settings
        self._store = store
        self._agent_id = store.agent_id
        self._tool_grants = tool_grants

    async def turn_context(self, root_id: UUID) -> GoalTurn | None:
        """None unless ``root_id`` is a goal session's root. Else the goal block, built from the goal, its plan, its
        journal tail, its spend and the evidence the session's lineage can cite, as read now; and the goal tools,
        bound to that session. A goal session's root has its tool grants read again before the turn is offered its
        tools (a failed read is logged and leaves the view empty; the turn runs)."""
        async with self._db.session() as s:
            row = (await s.execute(_session_of_root(self._agent_id, root_id))).scalar_one_or_none()
            if row is None:
                return None
            goal = (
                await s.execute(select(Goal).where(Goal.id == row.goal_id, Goal.agent_id == self._agent_id))
            ).scalar_one()
            total, today = await spend.goal_spent(s, self._agent_id, goal.id)
            moved = len(await moved_steps(s, self._agent_id, goal.id))
            ref = GoalSessionRef(
                goal_id=goal.id, session_id=row.id, session_n=row.n, root_id=root_id, started_at=row.started_at
            )
            scope = evidence.Scope(
                agent_id=self._agent_id,
                root_id=root_id,
                started_at=row.started_at,
                workspace_dir=str(getattr(self._settings, "workspace_dir", "")),
            )
            refs = await evidence.citeable(s, scope, limit=charter.REFS_SHOWN)
            ended = row.ended_at is not None
        tools = None
        if self._tool_grants is not None:
            await self._tool_grants.refresh_root(root_id)
            tools = await self._tool_lines(goal.id, root_id)
        # The directory is walked after the transaction and off the event loop: nothing in it is a database row.
        refs += await asyncio.to_thread(evidence.files, scope)
        journal = await self._store.journal_tail(goal.id)
        block = charter.render_goal_block(
            goal,
            plan=list(goal.plan or []),
            journal_rows=journal,
            spent=total,
            day_spent=today,
            session_n=ref.session_n,
            session_ended=ended,
            citeable=refs,
            root_dir=scope.root_dir,
            moved=moved,
            tools=tools,
        )
        tools_of_turn = make_goal_tools(self._db, self._settings, self._store, ref, tool_grants=self._tool_grants)
        return GoalTurn(block=block, tools=tools_of_turn)

    async def _tool_lines(self, goal_id: UUID, root_id: UUID) -> list[Any]:
        """F115 per-goal tools: what the turn is told of the goal's grants, each available when the runner would
        offer it to a decision turn of ``root_id`` (``grants.decide``, then the guard and the MCP floor)."""
        from nous.api.execution_context import ExecutionContext
        from nous.brain.intentions import AUTHORITY_INTERNAL
        from nous.goals.grants import decide, offered_now, session_tool_lines

        view = self._tool_grants
        assert view is not None
        ctx = ExecutionContext(
            kind="continuation", authority=AUTHORITY_INTERNAL, intention_id=root_id, root_intention_id=root_id
        )
        grants = await self._store.tool_grants(goal_id)
        return session_tool_lines(
            grants, lambda grant: offered_now(grant.tool, lifted=decide(view, ctx, grant.tool).lifted)
        )
