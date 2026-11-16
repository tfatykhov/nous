"""F114: the goal store over ``brain.goals``, ``brain.goal_sessions`` and ``brain.goal_journal`` (migration 113).

Every change of a goal is one transaction: the goal row is taken ``FOR UPDATE``, the change is a targeted UPDATE
guarded by the state it was read in (never a save of the whole row, so a concurrent pause is never overwritten), and
one ``state`` journal row naming the actor is appended. A refusal raises ``GoalRefused`` with the reason in words and
changes nothing. The journal is append-only: the store can add to it and read it, never change or remove a row.
Every query is scoped to the store's agent. No method sets a goal's ``mode``: every goal stays ``contained``.

A session's ``spent`` is the uncached input plus output tokens of its chain. Cache reads and writes, jobs launched
from the chain, embeddings and helper calls are not counted.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.tool_classes import McpIdentity
from nous.brain.continuation import owner_channel
from nous.goals.envelope import Envelope, EnvelopeError, envelope_for, validate
from nous.storage.database import Database
from nous.storage.models import Goal, GoalJournal, GoalSession, GoalToolGrant

JOURNAL_MAX_CHARS = 4000
EVIDENCE_MAX_REFS = 10
EVIDENCE_REF_MAX_CHARS = 300
TAIL_CHARS = 6000
TAIL_ENTRY_CHARS = 1500
CLIP_MARKER = " [clipped]"
# Marks the chain's own words inside the harness's session-close journal row (written by the goal driver).
SESSION_NOTE_LABEL = "Note written by the session:"
# The evidence ref (+ the pause reason) of the harness row the goal driver writes with each pause it makes for a
# reason other than the owner's: the goal routes select a paused goal's row by it, and the driver restarts runs of
# outcomes after its session.
STOP_REF = "stop:"
# The evidence ref (+ the session's number) of the harness row written when a session's done claim made the goal wait
# for the owner: the goal driver leaves that session out of a run of sessions without a step.
WAITING_REF = "waiting:"
MAX_DEADLINE_DAYS = 90
ACTORS: frozenset[str] = frozenset({"owner", "host", "chain", "harness"})
JOURNAL_KINDS: frozenset[str] = frozenset(
    {"plan", "progress", "report", "question", "answer", "owner_note", "state", "done_claim"}
)
LIVE_STATES: tuple[str, ...] = ("draft", "active", "paused")
# Who proposed a goal, and the actor its creation is journalled under.
_PROPOSER_ACTOR = {"owner": "owner", "host": "host", "agent": "chain"}
# (column, longest, words): the text bounds of brain.goals.
_TEXT_BOUNDS = (("title", 200, "the title"), ("objective", 4000, "the objective"), ("done_when", 2000, "done_when"))
# Journal kinds the tail always keeps, however long the rest is: what the owner said.
_ALWAYS_IN_TAIL = ("owner_note", "answer")
_TAIL_OTHERS_SCANNED = 100
_NO_REQUEST = {"pending_request": None, "pending_request_at": None}
# Who may approve, resume, confirm, or change a goal's budget or deadline: the owner, or the owner's host command.
OWNER_ACTORS: frozenset[str] = frozenset({"owner", "host"})
# The most tools one goal may be allowed to use without asking at once (brain.goal_tool_grants, migration 117).
GRANTS_MAX = 20
_SHORT_ID_RE = re.compile(r"^(?:g-?)?0*(\d{1,12})$", re.IGNORECASE)
# The channel kinds an owner-facing row is delivered on: nous.handlers.continuation_publisher pushes rows whose
# channel is LIKE 'telegram:%', to the chat id after the colon. A goal's questions and reports go to its channel.
DELIVERED_CHANNEL_KINDS: tuple[str, ...] = ("telegram",)

GoalRef = UUID | int | str
# A plan keeps its open steps and its newest closed or dropped ones; an older one leaves the plan with one journal row
# of the chain (kind ``plan``) whose evidence is ``moved:<step id>`` and the refs the step was closed with, so one-use
# and step ids still see it. The row is known by that first ref (``moved_row``), never by its text, which a note can
# copy (``MOVED_TEXT`` only begins what a reader sees).
MOVED_TEXT = "Moved out of the plan"
MOVED_REF = "moved:"


class GoalRefused(ValueError):
    """A change the store will not make. The text says why, in words the owner can act on."""


def format_short_id(short_id: int) -> str:
    """``G-0007``: the form the owner reads and types."""
    return f"G-{short_id:04d}"


def parse_ref(ref: Any) -> UUID | int | None:
    """A goal's id (a UUID or its text) or short id (``G-0007``, ``g7``, ``"7"`` or ``7``); None for anything else."""
    if isinstance(ref, UUID):
        return ref
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref if ref > 0 else None
    value = str(ref).strip()
    try:
        return UUID(value)
    except ValueError:
        pass
    match = _SHORT_ID_RE.match(value)
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def clip(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, the marker included, when it is longer."""
    if len(text) <= limit:
        return text
    return text[: limit - len(CLIP_MARKER)] + CLIP_MARKER


def describe(goal: Goal) -> str:
    """``G-0007 paused (owner)``: the goal's label and state, in one line."""
    reason = f" ({goal.pause_reason})" if goal.pause_reason else ""
    return f"{format_short_id(goal.short_id)} {goal.state}{reason}"


def _envelope_of(goal: Goal) -> Envelope:
    return Envelope(
        total_budget=goal.total_budget,
        day_budget=goal.day_budget,
        deadline_days=goal.deadline_days,
        sessions_per_day=goal.sessions_per_day,
        min_interval_seconds=goal.min_interval_seconds,
        jobs_per_day=goal.jobs_per_day,
        quiet_hours_ok=goal.quiet_hours_ok,
    )


def _owner_only(actor: str, what: str) -> None:
    if actor not in OWNER_ACTORS:
        raise GoalRefused(f"only the owner can {what}")


def grant_identity(grant: GoalToolGrant) -> McpIdentity | None:
    """The MCP identity a grant was given with (its server's row id, URL digest and remote tool), or None for a
    grant of any other tool."""
    if grant.server_id is None or grant.url_sha256 is None or grant.remote_tool is None:
        return None
    return McpIdentity(
        grant.server_id,
        grant.url_sha256,
        grant.remote_tool,
        server_name=grant.server_name or "",
        server_host=grant.server_host or "",
    )


def _allowed_text(tool: str, identity: McpIdentity | None, *, replaced: bool = False) -> str:
    """The owner's journal row of a grant."""
    named = f"{tool} (server {identity.server_name})" if identity else tool
    if replaced:
        return f"Allowed to use {named} without asking, from now on, in place of its earlier grant."
    return f"Allowed to use {named} without asking, from now on."


def _tools_changeable(goal: Goal) -> None:
    if goal.state not in LIVE_STATES:
        raise GoalRefused(f"{format_short_id(goal.short_id)} is finished; its tools can no longer be changed.")


class GoalStore:
    """Goals of one agent. ``settings`` is read for the owner's default channel (``approve``)."""

    def __init__(self, database: Database, agent_id: str, settings: Any) -> None:
        self._db = database
        self.agent_id = agent_id
        self._settings = settings

    # ---- reads ---------------------------------------------------------------------------------------------

    async def get(self, ref: GoalRef) -> Goal | None:
        """The goal ``ref`` names (its id or short id), or None."""
        async with self._db.session() as s:
            return await self._find(s, ref)

    async def list(self, states: Iterable[str] | None = None) -> list[Goal]:
        """The agent's goals by short id, optionally only those in ``states``."""
        query = select(Goal).where(Goal.agent_id == self.agent_id).order_by(Goal.short_id)
        if states is not None:
            query = query.where(Goal.state.in_(tuple(states)))
        async with self._db.session() as s:
            return list((await s.execute(query)).scalars())

    async def sessions(self, goal_id: UUID) -> list[GoalSession]:
        """The goal's sessions, oldest first."""
        query = (
            select(GoalSession)
            .where(GoalSession.agent_id == self.agent_id, GoalSession.goal_id == goal_id)
            .order_by(GoalSession.n)
        )
        async with self._db.session() as s:
            return list((await s.execute(query)).scalars())

    async def spent(self, goal_id: UUID) -> int:
        """Tokens the goal's sessions have spent, in all."""
        async with self._db.session() as s:
            return await self._spent(s, goal_id)

    async def tool_grants(self, goal_id: UUID) -> list[GoalToolGrant]:
        """The goal's active tool grants (not revoked), oldest first."""
        query = (
            select(GoalToolGrant)
            .where(
                GoalToolGrant.agent_id == self.agent_id,
                GoalToolGrant.goal_id == goal_id,
                GoalToolGrant.revoked_at.is_(None),
            )
            .order_by(GoalToolGrant.granted_at, GoalToolGrant.tool)
        )
        async with self._db.session() as s:
            return list((await s.execute(query)).scalars())

    async def journal(self, goal_id: UUID, *, limit: int = 10) -> list[GoalJournal]:
        """The goal's newest ``limit`` journal rows, newest first."""
        query = (
            select(GoalJournal)
            .where(GoalJournal.agent_id == self.agent_id, GoalJournal.goal_id == goal_id)
            .order_by(GoalJournal.seq.desc())
            .limit(limit)
        )
        async with self._db.session() as s:
            return list((await s.execute(query)).scalars())

    async def journal_tail(self, goal_id: UUID, *, chars: int = TAIL_CHARS) -> list[GoalJournal]:
        """The journal a session is shown, newest first: every owner note and answer, then the newest other rows
        while they fit in ``chars`` (owner rows count first). Each row's text is clipped to ``TAIL_ENTRY_CHARS``
        in the returned (detached) rows; the stored text is unchanged."""
        base = select(GoalJournal).where(GoalJournal.agent_id == self.agent_id, GoalJournal.goal_id == goal_id)
        async with self._db.session() as s:
            kept = list((await s.execute(base.where(GoalJournal.kind.in_(_ALWAYS_IN_TAIL)))).scalars())
            others = list(
                (
                    await s.execute(
                        base.where(GoalJournal.kind.not_in(_ALWAYS_IN_TAIL))
                        .order_by(GoalJournal.seq.desc())
                        .limit(_TAIL_OTHERS_SCANNED)
                    )
                ).scalars()
            )
        for row in kept + others:
            row.text = clip(row.text, TAIL_ENTRY_CHARS)
        used = sum(len(row.text) for row in kept)
        for row in others:
            if used + len(row.text) > chars:
                break
            kept.append(row)
            used += len(row.text)
        return sorted(kept, key=lambda row: row.seq, reverse=True)

    # ---- creation and the owner's changes ------------------------------------------------------------------

    async def create(
        self,
        *,
        title: str,
        objective: str,
        done_when: str,
        preset: str = "small",
        overrides: Mapping[str, Any] | None = None,
        proposed_by: str,
        origin_channel: str | None = None,
        tools: Sequence[tuple[str, McpIdentity | None]] = (),
        proof: str | None = None,
    ) -> Goal:
        """A new ``draft`` goal with a preset's limits, or custom ones when ``overrides`` are given. ``tools`` (F115
        per-goal tools, the owner's only): each ``(tool, identity)`` the goal may use without asking, granted in the
        same transaction as the goal, each with ``proof`` as ``granted_by`` and an owner ``state`` journal row after
        the creation's; nothing is written when one cannot be."""
        actor = _PROPOSER_ACTOR.get(proposed_by)
        if actor is None:
            raise GoalRefused(f"proposed_by must be one of {', '.join(_PROPOSER_ACTOR)}")
        if tools:
            _owner_only(actor, "change the tools a goal may use")
            if proof is None:
                raise ValueError("a grant needs the proof the owner's route accepted")
            if len(tools) > GRANTS_MAX:
                raise GoalRefused(f"A goal can have at most {GRANTS_MAX} tools. Remove one first.")
            names = [tool for tool, _identity in tools]
            if len(set(names)) != len(names):
                raise GoalRefused("A tool is listed twice.")
        texts = {"title": title.strip(), "objective": objective.strip(), "done_when": done_when.strip()}
        for column, longest, words in _TEXT_BOUNDS:
            if not 1 <= len(texts[column]) <= longest:
                raise GoalRefused(f"{words} must be 1 to {longest} characters long")
        try:
            preset_name, limits = envelope_for(preset, overrides)
        except EnvelopeError as exc:
            raise GoalRefused(str(exc)) from exc
        channel = origin_channel.strip() if origin_channel and origin_channel.strip() else None
        if channel is not None:
            kind, colon, chat = channel.partition(":")
            if kind not in DELIVERED_CHANNEL_KINDS or not colon or not chat or any(c.isspace() for c in chat):
                raise GoalRefused(
                    f"the origin channel must be <kind>:<id> of a kind the owner is sent to "
                    f"({', '.join(f'{k}:<chat id>' for k in DELIVERED_CHANNEL_KINDS)}), not {channel!r}"
                )
        async with self._db.session() as s:
            goal = Goal(
                agent_id=self.agent_id,
                **texts,
                preset=preset_name,
                **asdict(limits),
                proposed_by=proposed_by,
                origin_channel=channel,
            )
            s.add(goal)
            await s.flush()
            self.add_journal_row(s, goal.id, "state", f"Created as a draft ({preset_name} limits).", actor=actor)
            for tool, identity in tools:
                assert proof is not None
                self._add_grant(s, goal.id, tool, identity, proof=proof)
                self.add_journal_row(s, goal.id, "state", _allowed_text(tool, identity), actor=actor)
            await s.commit()
            return goal

    async def approve(self, ref: GoalRef, *, actor: str) -> Goal:
        """``draft`` to ``active``: the deadline runs from now and the first session may start now. Refused when
        the goal has nowhere to ask the owner (no origin channel and no default chat): its questions and reports
        would be dropped."""
        _owner_only(actor, "approve a goal")
        async with self._locked(ref) as (s, goal, now):
            if goal.state != "draft":
                raise GoalRefused(f"{describe(goal)}: only a draft can be approved")
            if owner_channel(self._settings, goal.origin_channel) is None:
                raise GoalRefused(
                    f"{describe(goal)} has nowhere to ask the owner: give it an origin channel or set "
                    "NOUS_TELEGRAM_CHAT_ID"
                )
            deadline = now + timedelta(days=goal.deadline_days)
            values = {"state": "active", "approved_at": now, "deadline_at": deadline, "next_wake_at": now}
            note = f"Approved: active until {deadline:%Y-%m-%d %H:%M} UTC."
            return await self._apply(s, goal, ("draft",), {**values, **_NO_REQUEST}, actor=actor, note=note)

    async def pause(self, ref: GoalRef, *, actor: str) -> Goal:
        """``active`` to ``paused (owner)``: no new session starts; an open one runs to its end."""
        async with self._locked(ref) as (s, goal, now):
            if goal.state != "active":
                raise GoalRefused(f"{describe(goal)}: only an active goal can be paused")
            values = {"state": "paused", "pause_reason": "owner", "next_wake_at": None, **_NO_REQUEST}
            return await self._apply(s, goal, ("active",), values, actor=actor, note="Paused.")

    async def resume(self, ref: GoalRef, *, actor: str) -> Goal:
        """``paused`` to ``active``, any request cleared. Refused while the reason still holds: a goal paused for
        its budget whose total is still spent (``raise_budget`` first), or for its deadline that has passed
        (``extend_deadline`` first)."""
        _owner_only(actor, "resume a goal")
        async with self._locked(ref) as (s, goal, now):
            if goal.state != "paused":
                raise GoalRefused(f"{describe(goal)}: only a paused goal can be resumed")
            if goal.pause_reason == "budget":
                spent = await self._spent(s, goal.id)
                if spent >= goal.total_budget:
                    raise GoalRefused(
                        f"{describe(goal)}: the total budget is spent ({spent} of {goal.total_budget} tokens); "
                        "raise it first"
                    )
            if goal.pause_reason == "deadline" and goal.deadline_at is not None and goal.deadline_at <= now:
                raise GoalRefused(
                    f"{describe(goal)}: the deadline has passed ({goal.deadline_at:%Y-%m-%d %H:%M} UTC); "
                    "extend it first"
                )
            values = {"state": "active", "pause_reason": None, "next_wake_at": now, **_NO_REQUEST}
            note = f"Resumed (it was paused: {goal.pause_reason})."
            return await self._apply(s, goal, ("paused",), values, actor=actor, note=note)

    async def confirm_done(self, ref: GoalRef, *, actor: str) -> Goal:
        """``paused (waiting_for_owner)`` to ``done``: only the owner's confirmation finishes a goal."""
        _owner_only(actor, "confirm a goal done")
        async with self._locked(ref) as (s, goal, now):
            if (goal.state, goal.pause_reason) != ("paused", "waiting_for_owner"):
                raise GoalRefused(f"{describe(goal)}: only a goal waiting for the owner can be confirmed done")
            values = {"state": "done", "pause_reason": None, "finished_at": now, "next_wake_at": None}
            return await self._apply(s, goal, ("paused",), values, actor=actor, note="Confirmed done.")

    async def cancel(self, ref: GoalRef, *, actor: str) -> Goal:
        """A draft, active or paused goal to ``cancelled``. With a session open, the driver is also asked to stop
        that session (``pending_request = 'cancel'``)."""
        async with self._locked(ref) as (s, goal, now):
            if goal.state not in LIVE_STATES:
                raise GoalRefused(f"{describe(goal)}: only a draft, active or paused goal can be cancelled")
            session = await self._open_session(s, goal.id)
            request = {"pending_request": "cancel", "pending_request_at": now} if session else _NO_REQUEST
            values = {"state": "cancelled", "pause_reason": None, "next_wake_at": None, "finished_at": now, **request}
            note = "Cancelled." + (f" Session {session.n} is open: it is to be stopped." if session else "")
            return await self._apply(
                s, goal, LIVE_STATES, values, actor=actor, note=note, session_n=session.n if session else None
            )

    async def stop(self, ref: GoalRef, *, actor: str) -> Goal:
        """Stop now. An active goal is paused (``owner``), as by ``pause``; a paused goal keeps its pause. With a
        session open, the driver is also asked to stop it (``pending_request = 'stop'``). A paused goal with no
        open session has nothing to stop: refused."""
        async with self._locked(ref) as (s, goal, now):
            if goal.state not in ("active", "paused"):
                raise GoalRefused(f"{describe(goal)}: only an active or paused goal can be stopped")
            session = await self._open_session(s, goal.id)
            if goal.state == "paused" and session is None:
                raise GoalRefused(f"{describe(goal)}: no session is open, so there is nothing to stop")
            request = {"pending_request": "stop", "pending_request_at": now} if session else _NO_REQUEST
            if goal.state == "active":
                values = {"state": "paused", "pause_reason": "owner", "next_wake_at": None, **request}
                note = "Paused."
            else:
                values, note = request, "Stop asked."
            if session is not None:
                note += f" Session {session.n} is open: it is to be stopped."
            return await self._apply(
                s, goal, (goal.state,), values, actor=actor, note=note, session_n=session.n if session else None
            )

    async def request_run_now(self, ref: GoalRef, *, actor: str) -> Goal:
        """Ask the driver for a session now (``pending_request = 'run_now'``): an active goal with no open session."""
        _owner_only(actor, "ask for a session now")
        async with self._locked(ref) as (s, goal, now):
            if goal.state != "active":
                raise GoalRefused(f"{describe(goal)}: only an active goal can run a session now")
            session = await self._open_session(s, goal.id)
            if session is not None:
                raise GoalRefused(f"{describe(goal)}: session {session.n} is already running")
            values = {"pending_request": "run_now", "pending_request_at": now}
            return await self._apply(s, goal, ("active",), values, actor=actor, note="Asked for a session now.")

    async def raise_budget(self, ref: GoalRef, *, total: int | None = None, day: int | None = None, actor: str) -> Goal:
        """New total and/or day budget for a draft, active or paused goal, within the table's ranges. A lowered
        total cannot go below what the goal has already spent; a raised one may stay under it (a chain can
        overshoot its goal's total)."""
        _owner_only(actor, "change a goal's budget")
        async with self._locked(ref) as (s, goal, now):
            if goal.state not in LIVE_STATES:
                raise GoalRefused(f"{describe(goal)}: only a draft, active or paused goal's budget can change")
            if total is None and day is None:
                raise GoalRefused("give a new total budget, a new day budget, or both")
            old = _envelope_of(goal)
            new = replace(
                old,
                total_budget=old.total_budget if total is None else total,
                day_budget=old.day_budget if day is None else day,
            )
            try:
                validate(new)
            except EnvelopeError as exc:
                raise GoalRefused(str(exc)) from exc
            if new.total_budget < old.total_budget:  # only a lowered total is checked: a raise moves the right way
                spent = await self._spent(s, goal.id)
                if new.total_budget < spent:
                    raise GoalRefused(
                        f"{describe(goal)}: {spent} tokens are already spent; the total cannot be lowered below that"
                    )
            values = {"total_budget": new.total_budget, "day_budget": new.day_budget}
            note = (
                f"Budget: total {old.total_budget} -> {new.total_budget} tokens, "
                f"day {old.day_budget} -> {new.day_budget} tokens."
            )
            return await self._apply(s, goal, LIVE_STATES, values, actor=actor, note=note)

    async def extend_deadline(self, ref: GoalRef, *, days: int, actor: str) -> Goal:
        """``days`` more. A draft's length (``deadline_days``, applied at approval) grows, up to 90 days. An approved
        goal's deadline moves to ``days`` after the later of its deadline and now, and may be at most 90 days from
        now; the length it was approved with is kept."""
        _owner_only(actor, "extend a goal's deadline")
        async with self._locked(ref) as (s, goal, now):
            if goal.state not in LIVE_STATES:
                raise GoalRefused(f"{describe(goal)}: only a draft, active or paused goal's deadline can change")
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DEADLINE_DAYS:
                raise GoalRefused(f"the extension must be a whole number of days, 1 to {MAX_DEADLINE_DAYS}")
            values: dict[str, Any]
            if goal.deadline_at is None:
                if goal.deadline_days + days > MAX_DEADLINE_DAYS:
                    raise GoalRefused(
                        f"a goal's deadline is at most {MAX_DEADLINE_DAYS} days; {describe(goal)} has "
                        f"{goal.deadline_days} already"
                    )
                values = {"deadline_days": goal.deadline_days + days}
                note = f"Deadline extended by {days} days (to {goal.deadline_days + days} days from approval)."
            else:
                deadline = max(goal.deadline_at, now) + timedelta(days=days)
                if deadline <= now:
                    raise GoalRefused("the new deadline would not be in the future")
                if deadline > now + timedelta(days=MAX_DEADLINE_DAYS):
                    raise GoalRefused(
                        f"a goal's deadline can be at most {MAX_DEADLINE_DAYS} days from now; "
                        f"{deadline:%Y-%m-%d %H:%M} UTC is further"
                    )
                values = {"deadline_at": deadline}
                note = f"Deadline extended by {days} days, to {deadline:%Y-%m-%d %H:%M} UTC."
            return await self._apply(s, goal, LIVE_STATES, values, actor=actor, note=note)

    async def add_note(self, ref: GoalRef, text: str, *, actor: str) -> Goal:
        """An ``owner_note`` journal row on a draft, active or paused goal. An active goal with no open session is
        woken now so the next session reads it; a note never resumes a paused goal. Only the owner writes one."""
        _owner_only(actor, "write an owner's note")
        body = text.strip()
        if not body:
            raise GoalRefused("a note needs some text")
        async with self._locked(ref) as (s, goal, now):
            if goal.state not in LIVE_STATES:
                raise GoalRefused(f"{describe(goal)}: a note can only be added to a draft, active or paused goal")
            self.add_journal_row(s, goal.id, "owner_note", body, actor=actor)
            if goal.state == "active" and await self._open_session(s, goal.id) is None:
                await self._wake(s, goal.id, now)
            return await self._commit_read(s, goal)

    # ---- the tools a goal may use without asking -----------------------------------------------------------

    async def grant_tool(
        self, ref: GoalRef, tool: str, identity: McpIdentity | None, *, actor: str, proof: str
    ) -> bool:
        """Allow a draft, active or paused goal to use ``tool`` without asking: a ``brain.goal_tool_grants`` row
        (``granted_by`` is ``proof``, the proof the owner's route accepted) and an owner ``state`` journal row, in one
        transaction under the goal row's lock. ``identity`` is an MCP tool's identity as the owner saw it (None for any
        other tool). False, and nothing written, when the tool is granted already with the same identity; an MCP tool
        granted with another identity has that grant revoked and a new one written. Refused past ``GRANTS_MAX``
        grants. The goal's wake and request are not touched."""
        _owner_only(actor, "change the tools a goal may use")
        async with self._locked(ref) as (s, goal, now):
            _tools_changeable(goal)
            current = await self._active_grant(s, goal.id, tool)
            if current is not None and grant_identity(current) == identity:
                return False
            if current is None:
                count = await s.scalar(
                    select(func.count())
                    .select_from(GoalToolGrant)
                    .where(GoalToolGrant.goal_id == goal.id, GoalToolGrant.revoked_at.is_(None))
                )
                if int(count or 0) >= GRANTS_MAX:
                    raise GoalRefused(f"A goal can have at most {GRANTS_MAX} tools. Remove one first.")
            else:
                await self._revoke_grant(s, current.id, proof, now)
            self._add_grant(s, goal.id, tool, identity, proof=proof, at=now)
            self.add_journal_row(
                s, goal.id, "state", _allowed_text(tool, identity, replaced=current is not None), actor=actor
            )
            await s.commit()
            return True

    async def revoke_tool(self, ref: GoalRef, tool: str, *, actor: str, proof: str) -> bool:
        """No longer allow a draft, active or paused goal to use ``tool`` without asking: its active grant gets
        ``revoked_by`` (``proof``) and ``revoked_at``, with an owner ``state`` journal row, in one transaction under
        the goal row's lock. False, and nothing written, when the tool has no active grant."""
        _owner_only(actor, "change the tools a goal may use")
        async with self._locked(ref) as (s, goal, now):
            _tools_changeable(goal)
            current = await self._active_grant(s, goal.id, tool)
            if current is None:
                return False
            await self._revoke_grant(s, current.id, proof, now)
            self.add_journal_row(s, goal.id, "state", f"No longer allowed to use {tool} without asking.", actor=actor)
            await s.commit()
            return True

    def _add_grant(
        self,
        s: AsyncSession,
        goal_id: UUID,
        tool: str,
        identity: McpIdentity | None,
        *,
        proof: str,
        at: datetime | None = None,
    ) -> None:
        """One grant row in the caller's transaction (``at`` None: the database's time)."""
        s.add(
            GoalToolGrant(
                agent_id=self.agent_id,
                goal_id=goal_id,
                tool=tool,
                server_name=identity.server_name if identity else None,
                server_host=identity.server_host if identity else None,
                server_id=identity.server_id if identity else None,
                url_sha256=identity.url_sha256 if identity else None,
                remote_tool=identity.remote_tool if identity else None,
                granted_by=proof,
                **({"granted_at": at} if at is not None else {}),
            )
        )

    async def _active_grant(self, s: AsyncSession, goal_id: UUID, tool: str) -> GoalToolGrant | None:
        query = select(GoalToolGrant).where(
            GoalToolGrant.agent_id == self.agent_id,
            GoalToolGrant.goal_id == goal_id,
            GoalToolGrant.tool == tool,
            GoalToolGrant.revoked_at.is_(None),
        )
        return (await s.execute(query)).scalar_one_or_none()

    async def _revoke_grant(self, s: AsyncSession, grant_id: UUID, proof: str, now: datetime) -> None:
        await s.execute(
            update(GoalToolGrant)
            .where(GoalToolGrant.id == grant_id, GoalToolGrant.revoked_at.is_(None))
            .values(revoked_by=proof, revoked_at=now)
            .execution_options(synchronize_session=False)
        )

    async def delay_wake(self, goal_id: UUID, *, seconds: int, seen: datetime | None) -> bool:
        """Move an active goal's next wake to ``seconds`` from now, never earlier than it is, only if it is still
        ``seen``: the wake the caller read before deciding to delay. A wake someone set since (``add_note`` or
        ``resume`` set it to now) is kept, and so is a goal paused or finished meanwhile. A wake already later than
        ``seconds`` from now stays where it is (a failed run-now must not bring the next timer session forward).
        True when the update matched."""
        if seconds < 0:
            raise GoalRefused("a wake can only be delayed, not moved back")
        async with self._db.session() as s:
            at = func.greatest(Goal.next_wake_at, func.clock_timestamp() + timedelta(seconds=seconds))
            changed = await self._wake(s, goal_id, at, if_seen=True, seen=seen)
            await s.commit()
            return changed

    async def append_journal(
        self,
        goal_id: UUID,
        kind: str,
        text: str,
        *,
        actor: str,
        session_n: int | None = None,
        evidence: Iterable[str] = (),
    ) -> GoalJournal:
        """Append one journal row in its own transaction: text clipped to ``JOURNAL_MAX_CHARS``, at most
        ``EVIDENCE_MAX_REFS`` evidence refs of at most ``EVIDENCE_REF_MAX_CHARS`` each. Never under the
        ``harness`` actor: the goal driver writes those rows itself, in its own transactions, and reads some of them
        back (where a run of outcomes restarts, which session's chain it is ending)."""
        if actor == "harness":
            raise GoalRefused("harness rows are written by the goal driver only")
        async with self._db.session() as s:
            owned = await s.scalar(select(Goal.id).where(Goal.id == goal_id, Goal.agent_id == self.agent_id))
            if owned is None:
                raise GoalRefused(f"no goal {goal_id}")
            row = self.add_journal_row(s, goal_id, kind, text, actor=actor, session_n=session_n, evidence=evidence)
            await s.commit()
            return row

    # ---- inside a transaction ------------------------------------------------------------------------------

    async def _find(self, s: AsyncSession, ref: GoalRef, *, lock: bool = False) -> Goal | None:
        key = parse_ref(ref)
        if key is None:
            return None
        match = Goal.id == key if isinstance(key, UUID) else Goal.short_id == key
        query = select(Goal).where(Goal.agent_id == self.agent_id, match)
        if lock:
            query = query.with_for_update()
        return (await s.execute(query)).scalar_one_or_none()

    @asynccontextmanager
    async def _locked(self, ref: GoalRef) -> AsyncIterator[tuple[AsyncSession, Goal, datetime]]:
        """A session holding the goal row ``FOR UPDATE`` until it commits or closes, and the database's clock read
        once after the lock: every time the store writes comes from it, never from this process's clock, so a
        request is never stamped earlier than a session row written before it under the same lock."""
        async with self._db.session() as s:
            goal = await self._find(s, ref, lock=True)
            if goal is None:
                raise GoalRefused(f"no goal {ref}")
            now = (await s.execute(select(func.clock_timestamp()))).scalar_one()
            yield s, goal, now

    async def _apply(
        self,
        s: AsyncSession,
        goal: Goal,
        from_states: tuple[str, ...],
        values: Mapping[str, Any],
        *,
        actor: str,
        note: str,
        session_n: int | None = None,
    ) -> Goal:
        """The targeted UPDATE (guarded by ``from_states``) and its ``state`` journal row, committed together."""
        result = await s.execute(
            update(Goal)
            .where(Goal.id == goal.id, Goal.agent_id == self.agent_id, Goal.state.in_(from_states))
            .values(**values, updated_at=func.clock_timestamp())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise GoalRefused(f"{describe(goal)} changed meanwhile; nothing was done")
        self.add_journal_row(s, goal.id, "state", note, actor=actor, session_n=session_n)
        return await self._commit_read(s, goal)

    async def _commit_read(self, s: AsyncSession, goal: Goal) -> Goal:
        """Read ``goal`` back inside the changing transaction (its row lock still held, so the values read are the ones
        committed), then commit, and return it. Nothing is read after the commit, so a read that fails rolls the
        change back with it instead of answering a change that was made with an error."""
        await s.refresh(goal)
        await s.commit()
        return goal

    async def _wake(
        self, s: AsyncSession, goal_id: UUID, at: Any, *, if_seen: bool = False, seen: datetime | None = None
    ) -> bool:
        """Set an active goal's next wake to ``at``; with ``if_seen``, only while it is still ``seen``."""
        guard = [Goal.id == goal_id, Goal.agent_id == self.agent_id, Goal.state == "active"]
        if if_seen:
            guard.append(Goal.next_wake_at.is_not_distinct_from(seen))
        result = await s.execute(
            update(Goal)
            .where(*guard)
            .values(next_wake_at=at, updated_at=func.clock_timestamp())
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def _open_session(self, s: AsyncSession, goal_id: UUID) -> GoalSession | None:
        query = select(GoalSession).where(
            GoalSession.agent_id == self.agent_id, GoalSession.goal_id == goal_id, GoalSession.ended_at.is_(None)
        )
        return (await s.execute(query)).scalar_one_or_none()

    async def _spent(self, s: AsyncSession, goal_id: UUID) -> int:
        query = select(func.coalesce(func.sum(GoalSession.spent), 0)).where(
            GoalSession.agent_id == self.agent_id, GoalSession.goal_id == goal_id
        )
        return int((await s.execute(query)).scalar_one())

    def add_journal_row(
        self,
        s: AsyncSession,
        goal_id: UUID,
        kind: str,
        text: str,
        *,
        actor: str,
        session_n: int | None = None,
        evidence: Iterable[str] = (),
    ) -> GoalJournal:
        """Append one journal row in the caller's transaction: it is added to ``s`` and neither flushed nor
        committed, so it is written with the caller's other changes or not at all (the goal driver claims a report
        and journals it this way). Same bounds as ``append_journal``. The caller has already scoped ``goal_id``
        to this store's agent: the row is not checked against it."""
        if kind not in JOURNAL_KINDS:
            raise ValueError(f"unknown journal kind {kind!r}")
        if actor not in ACTORS:
            raise ValueError(f"unknown actor {actor!r}")
        refs = [clip(str(ref), EVIDENCE_REF_MAX_CHARS) for ref in list(evidence)[:EVIDENCE_MAX_REFS]]
        row = GoalJournal(
            agent_id=self.agent_id,
            goal_id=goal_id,
            session_n=session_n,
            kind=kind,
            actor=actor,
            text=clip(text, JOURNAL_MAX_CHARS),
            evidence=refs,
        )
        s.add(row)
        return row


def moved_row() -> ColumnElement[bool]:
    """A journal row that keeps a step the plan moved out: a ``plan`` row of the chain whose first evidence ref is
    ``moved:<step id>``. Only the plan tool's own code writes that ref: a ref a call sends must pass
    ``evidence.check`` (a subtask, a DAG, a recorded call or a file), and ``goal_note`` writes no evidence."""
    return and_(
        GoalJournal.kind == "plan",
        GoalJournal.actor == "chain",
        func.coalesce(GoalJournal.evidence[0].astext, "").startswith(MOVED_REF),
    )


async def moved_steps(session: AsyncSession, agent_id: str, goal_id: UUID) -> list[list[str]]:
    """The evidence of each journal row that keeps a step the goal's plan moved out (``moved_row``), oldest first:
    ``moved:<step id>`` and the refs that step was closed with."""
    query = (
        select(GoalJournal.evidence)
        .where(GoalJournal.agent_id == agent_id, GoalJournal.goal_id == goal_id, moved_row())
        .order_by(GoalJournal.seq)
    )
    return [[str(ref) for ref in evidence or []] for evidence in (await session.execute(query)).scalars()]
