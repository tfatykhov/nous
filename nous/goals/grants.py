"""F115 per-goal tools: the tools the owner allowed one goal to use without asking, as the runner reads them.

A turn may use such a tool without the class rule that keeps an internal_only turn to tools that act inside Nous
(``tool_policy.internal_only_allowed`` and the two per-call checks take ``lifted``) only when ``decide`` says so. The
first failing step wins:

0. the tool guard is installed (``tool_guard.current()``): the guard's holds are what keep a granted tool off when the
   owner switches it off for background work or the host locks it;
1. the turn is internal_only, of a kind in ``tool_policy.GRANT_KINDS`` (a chain decision, a task or a workflow step
   that runs as a task) and names a root (``ctx.root_intention_id``, set from the lineage stamp, never by the model);
2. the tool is one a goal may be given (``tool_policy.grant_refusal``);
3. the view is fresh: fully read within ``max(3 * goals_tick_seconds, MIN_FRESH_SECONDS)`` seconds of the injected
   monotonic clock;
4. the view holds a grant of the tool for the turn's root. It holds only the roots of OPEN sessions of ACTIVE goals,
   so a paused, finished or ended one gets nothing;
5. for an MCP client tool, the tool registered now has the identity the grant was given with (server row, URL digest
   and remote tool name).

Every other check stays where it is and does not read the lift: a cancelled root, the guard's holds, the MCP client
floor (a server not available to background work), the write_file and cancel_task rules, the undoable rule and
ActionGate. ``decide`` never raises: an error is ``Decision(False, "error")``, logged by its class.

``GoalToolGrants`` is the view: dict lookups for the runner, and refreshes that each hold one lock around their whole
read and apply, so a refresh that read before a revoke committed can never be applied after the revoke's own refresh.
A refresh that fails clears the whole view. The first use of a granted tool in a session writes one harness journal row
(``note_use``).

The screen's words come from the tables here (``GRANT_PLACES``, ``NEVER_SENTENCE``, ``GRANT_FACTS``), which the
routes read and tests tie to the code's own tables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select, text

from nous.api import tool_policy
from nous.api.execution_context import ExecutionContext
from nous.api.tool_classes import MCP_TOOL_PREFIX, McpIdentity, mcp_tool_identity
from nous.autonomy import tool_guard
from nous.brain.intentions import AUTHORITY_INTERNAL
from nous.goals.store import grant_identity
from nous.storage.models import Goal, GoalSession, GoalToolGrant

logger = logging.getLogger(__name__)

# The view is fresh this long after its last full read, at least (or three driver ticks, when longer).
MIN_FRESH_SECONDS = 180
USED_REF = "grant-used:"

# ---- the words --------------------------------------------------------------------------------------------------

# Where a grant applies: each kind of tool_policy.GRANT_KINDS, in the tool guard's words (tool_guard.KINDS).
GRANT_PLACES: Mapping[str, str] = MappingProxyType(
    {
        "continuation": "chain decisions",
        "subtask": "background tasks",
        "dag_node": "workflow steps that run as tasks",
    }
)
NEVER_SENTENCE = (
    "The shell and Python cannot be allowed here. Neither can scheduling, starting other work, or changing settings."
)
# The tools each phrase of NEVER_SENTENCE stands for (each one on the internal_only denylist or a spawn tool).
NEVER_NAMED: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "The shell": ("bash",),
        "Python": ("run_python",),
        "scheduling": ("schedule_task", "heartbeat_check_create"),
        "starting other work": ("spawn_task", "spawn_sync", "dag_create"),
        "changing settings": ("create_censor", "store_identity", "learn_skill", "heartbeat_check_manage"),
    }
)


@dataclass(frozen=True)
class GrantFact:
    """What a built-in tool does when a goal may use it without asking (``does``, a verb phrase), one more sentence
    the owner should read first (``note``), and why it cannot be allowed while the server lacks a setting
    (``ready``: None when nothing is missing)."""

    does: str
    note: str
    ready: Callable[[Any], str | None] = lambda _settings: None


def _email_ready(settings: Any) -> str | None:
    if str(getattr(settings, "email_allowlist", "") or "").strip():
        return None
    if str(getattr(settings, "email_allowlist_file", "") or "").strip():
        return None
    return "The email allowlist is empty, so send_email could send nothing."


def _file_ready(settings: Any) -> str | None:
    if str(getattr(settings, "telegram_chat_id", "") or "").strip():
        return None
    return "send_file has no Telegram chat set, so it could send to any chat a call names. Set the chat first."


# Every built-in tool a goal may be given (tool_policy.grant_refusal), with its words. A test fails while one has none.
GRANT_FACTS: Mapping[str, GrantFact] = MappingProxyType(
    {
        "send_email": GrantFact(
            "send email to the addresses on your email allowlist", "A sent email cannot be taken back.", _email_ready
        ),
        "send_file": GrantFact(
            "send files from the server to your Telegram chat", "A sent file cannot be taken back.", _file_ready
        ),
        "browser_task": GrantFact(
            "open web pages on the sites each call names, click and type",
            "It has no logins, and it stops before signing in, paying, buying, accepting terms, sending messages or "
            "deleting anything.",
        ),
    }
)
MCP_NOTE = "It can do whatever that tool does there, which Nous cannot check: read, change or delete things."


def lead_sentence() -> str:
    """Where a goal's grants apply, from ``GRANT_PLACES``."""
    return (
        "These tools can be used without asking you, by this goal's own sessions and the work they start "
        f"({tool_guard.list_words(list(GRANT_PLACES.values()))}). A workflow's check steps are not included. "
        "Nothing else gets them."
    )


def words_for(tool: str, identity: McpIdentity | None) -> tuple[str, str]:
    """``(does, note)`` for ``tool``: its ``GRANT_FACTS`` entry, or for an MCP client tool the server's own name for
    it, the server and its host. A tool with neither: its name, and the MCP note."""
    if tool.startswith(MCP_TOOL_PREFIX):
        if identity is None:
            return f"use {tool}", MCP_NOTE
        return f"use {identity.remote_tool} on {identity.server_name} ({identity.server_host})", MCP_NOTE
    fact = GRANT_FACTS.get(tool)
    if fact is None:
        return f"use {tool}", ""
    return fact.does, fact.note


def not_ready(tool: str, settings: Any) -> str | None:
    """Why ``tool`` cannot be allowed while the server lacks a setting, or None."""
    fact = GRANT_FACTS.get(tool)
    return fact.ready(settings) if fact is not None else None


def used_ref(tool: str) -> str:
    """The evidence ref of the journal row of a session's first use of ``tool``."""
    return f"{USED_REF}{tool}"


# ---- the view ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    """A grant as the view holds it for one open session's root: the goal, the session's number, the tool and, for
    an MCP client tool, the identity it was given with."""

    goal_id: UUID
    session_n: int
    tool: str
    identity: McpIdentity | None


@dataclass(frozen=True)
class Decision:
    """Whether a turn may use a tool without the class rule (``lifted``), and the step that decided (``why``)."""

    lifted: bool
    why: str


class GoalToolGrants:
    """The grants of the open sessions of active goals, by root and tool. Built from the database by ``refresh``
    (all of it), ``refresh_root`` (one session's root) and ``refresh_goal`` (one goal's roots); each returns whether
    it read, never raises, and clears the whole view when it fails. Only ``refresh`` renews ``fresh``."""

    def __init__(self, database: Any, settings: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._db = database
        self._agent_id = settings.agent_id
        self._max_age = float(max(3 * int(settings.goals_tick_seconds), MIN_FRESH_SECONDS))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._by_root: dict[UUID, dict[str, Grant]] = {}
        self._loaded_at: float | None = None
        self._used: set[tuple[UUID, str]] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # ---- the runner's reads (sync, no await) ----------------------------------------------------------------

    def grant(self, root_id: UUID, tool: str) -> Grant | None:
        """The grant of ``tool`` for the session whose root is ``root_id``, or None."""
        return self._by_root.get(root_id, {}).get(tool)

    def fresh(self) -> bool:
        """Whether the view was fully read within its age, by the injected clock."""
        return self._loaded_at is not None and self._clock() - self._loaded_at <= self._max_age

    def lifts(self, ctx: ExecutionContext, tool: str) -> bool:
        """``decide``'s answer, as the runner's hook takes it."""
        return decide(self, ctx, tool).lifted

    def note_used(self, ctx: ExecutionContext, tool: str) -> None:
        """The runner's hook after it authorized a call ``lifts`` allowed: ``note_use`` for the turn's root."""
        if ctx.root_intention_id is not None:
            self.note_use(ctx.root_intention_id, tool)

    def note_use(self, root_id: UUID, tool: str) -> None:
        """A granted tool was authorized in the session of ``root_id``: on the first time per (root, tool) in this
        process, write the session's journal row in a task of its own (``_journal_use``), whose failure is logged by
        its class."""
        key = (root_id, tool)
        if key in self._used:
            return
        grant = self.grant(root_id, tool)
        if grant is None:
            return
        self._used.add(key)
        try:
            task = asyncio.get_running_loop().create_task(self._journal_use(grant))
        except Exception as exc:  # no running loop: nothing is written
            self._used.discard(key)
            logger.warning("F115: a granted tool's first use was not journalled (%s)", type(exc).__name__)
            return
        self._tasks.add(task)
        task.add_done_callback(self._used_done)

    def _used_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("F115: a granted tool's first use was not journalled (%s)", type(task.exception()).__name__)

    async def _journal_use(self, grant: Grant) -> None:
        """One ``progress`` row of the harness for the session, unless the session has one for this tool already."""
        ref = json.dumps([used_ref(grant.tool)])
        body = f"Session {grant.session_n} was allowed to use {grant.tool} without asking, as you set for this goal."
        async with self._db.session() as s:
            await s.execute(
                text(
                    "INSERT INTO brain.goal_journal (agent_id, goal_id, session_n, kind, actor, text, evidence) "
                    "SELECT :agent, :goal, :n, 'progress', 'harness', :body, CAST(:ref AS JSONB) "
                    "WHERE NOT EXISTS (SELECT 1 FROM brain.goal_journal WHERE agent_id = :agent AND goal_id = :goal "
                    "AND session_n = :n AND kind = 'progress' AND actor = 'harness' "
                    "AND evidence @> CAST(:ref AS JSONB))"
                ),
                {"agent": self._agent_id, "goal": grant.goal_id, "n": grant.session_n, "body": body, "ref": ref},
            )
            await s.commit()

    # ---- refreshes -------------------------------------------------------------------------------------------

    async def refresh(self) -> bool:
        """Read every grant of an open session of an active goal; renews ``fresh``."""
        return await self._refresh()

    async def refresh_root(self, root_id: UUID) -> bool:
        """Read the grants of the session whose root is ``root_id`` again."""
        return await self._refresh(root_id=root_id)

    async def refresh_goal(self, goal_id: UUID) -> bool:
        """Read the grants of the goal's open session again."""
        return await self._refresh(goal_id=goal_id)

    async def _refresh(self, *, root_id: UUID | None = None, goal_id: UUID | None = None) -> bool:
        async with self._lock:  # around the whole read and apply
            try:
                found = await self._read(root_id=root_id, goal_id=goal_id)
            except Exception as exc:
                self._by_root = {}
                self._loaded_at = None
                logger.warning("F115: the goals' tool grants could not be read; none apply (%s)", type(exc).__name__)
                return False
            if root_id is None and goal_id is None:
                self._by_root = found
                self._loaded_at = self._clock()
                self._used = {key for key in self._used if key[0] in found}
                return True
            kept = {
                root: tools
                for root, tools in self._by_root.items()
                if root != root_id and not any(grant.goal_id == goal_id for grant in tools.values())
            }
            self._by_root = {**kept, **found}
            return True

    async def _read(self, *, root_id: UUID | None, goal_id: UUID | None) -> dict[UUID, dict[str, Grant]]:
        query = (
            select(GoalSession.root_id, GoalSession.n, GoalToolGrant)
            .join(
                Goal,
                and_(Goal.id == GoalToolGrant.goal_id, Goal.agent_id == self._agent_id, Goal.state == "active"),
            )
            .join(
                GoalSession,
                and_(
                    GoalSession.goal_id == GoalToolGrant.goal_id,
                    GoalSession.agent_id == self._agent_id,
                    GoalSession.ended_at.is_(None),
                    GoalSession.root_id.is_not(None),
                ),
            )
            .where(GoalToolGrant.agent_id == self._agent_id, GoalToolGrant.revoked_at.is_(None))
        )
        if root_id is not None:
            query = query.where(GoalSession.root_id == root_id)
        if goal_id is not None:
            query = query.where(GoalToolGrant.goal_id == goal_id)
        found: dict[UUID, dict[str, Grant]] = {}
        async with self._db.session() as s:
            for root, n, row in (await s.execute(query)).all():
                found.setdefault(root, {})[row.tool] = Grant(row.goal_id, n, row.tool, grant_identity(row))
        return found


def decide(view: GoalToolGrants, ctx: ExecutionContext, tool: str) -> Decision:
    """Whether a turn of ``ctx`` may use ``tool`` without the class rule, and why (see the module's steps). Never
    raises."""
    try:
        if tool_guard.current() is None:
            return Decision(False, "no_guard")
        if ctx.authority != AUTHORITY_INTERNAL:
            return Decision(False, "authority")
        if ctx.kind not in tool_policy.GRANT_KINDS:
            return Decision(False, "kind")
        if ctx.root_intention_id is None:
            return Decision(False, "no_root")
        if tool_policy.grant_refusal(tool) is not None:
            return Decision(False, "not_grantable")
        if not view.fresh():
            return Decision(False, "stale")
        grant = view.grant(ctx.root_intention_id, tool)
        if grant is None:
            return Decision(False, "not_granted")
        if tool.startswith(MCP_TOOL_PREFIX) and (grant.identity is None or mcp_tool_identity(tool) != grant.identity):
            return Decision(False, "identity_changed")
        return Decision(True, "granted")
    except Exception as exc:
        logger.warning("F115: a goal tool grant could not be decided; not lifted (%s)", type(exc).__name__)
        return Decision(False, "error")
