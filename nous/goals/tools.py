"""F114: ``goal_note`` and ``goal_plan``, the two tools a decision turn of a goal session's root is given.

They are per-turn extra tools (``extra_tools`` of the continuation runner's turn), never registered with a
dispatcher, so no other turn can be offered them; ``nous.api.tool_classes`` classifies them and
``nous.api.tool_policy.INTERNAL_ONLY_EXTRA_TOOLS`` lists them. The executors are bound to one session of one goal and
write as the chain: the actor of every row they write is ``chain`` (``harness`` for the fixed words of a state change
a done claim makes), never the owner, and neither can approve, resume, confirm, cancel or change a goal's limits.

Every write is one transaction that takes the session row ``FOR UPDATE`` and then the goal row ``FOR UPDATE`` (the
order the driver's close takes them), re-reads both, and writes targeted columns with a journal row. So a close of the
session, an owner's pause or cancel, or another call of the same chain is either finished before the call reads, and
seen, or waits for the call to commit:

- a step closed while the session is open is counted by the session's close; a step can no longer be closed once
  the session has ended (its outcome is written), and the refusal says so;
- a goal that is no longer active or paused (done, failed or cancelled) takes no write;
- the evidence of a closed step is checked against the session's lineage as of the call (``nous.goals.evidence``),
  and no ref closes two steps of the goal.

The executors never raise: a bad call, or a failure, is a text the model reads, and nothing is written.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain import continuation
from nous.goals import evidence
from nous.goals.quote import STEP_EVIDENCE_MAX, STEP_EVIDENCE_REF_MAX, STEP_TEXT_MAX, _visible, neutralize
from nous.goals.store import JOURNAL_MAX_CHARS, GoalStore, clip, format_short_id
from nous.storage.database import Database
from nous.storage.models import Goal, GoalSession

logger = logging.getLogger(__name__)

MAX_STEPS = 20  # brain.goals.plan holds at most 20 steps (migration 113)
STEP_STATUSES = ("open", "closed", "dropped")
NOTE_KINDS = ("progress", "done_claim")
WRITABLE_STATES = ("active", "paused")  # a running session of a paused goal still runs to its end
_STEP_ID = re.compile(r"s(\d{1,4})")
# The owner message of a done claim: one per session (its source id).
DONE_NAMESPACE = uuid.UUID("5b0c3f8e-7d1a-4f0e-9a51-6d2c8e4b1f14")
MESSAGE_TITLE_CHARS = 120  # the goal's title, as the done message quotes it

GOAL_NOTE_SCHEMA: dict[str, Any] = {
    "name": "goal_note",
    "description": (
        "Write a note to the journal of the goal this work belongs to. kind 'progress': what this work did or found, "
        "for the next session. kind 'done_claim': the goal's done-when is met; the goal then waits for the owner, "
        "who confirms it done or resumes it (only the owner can finish a goal). Does not end the turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(NOTE_KINDS)},
            "text": {"type": "string", "description": f"The note, at most {JOURNAL_MAX_CHARS} characters."},
        },
        "required": ["kind", "text"],
    },
}

GOAL_PLAN_SCHEMA: dict[str, Any] = {
    "name": "goal_plan",
    "description": (
        "Change the plan of the goal this work belongs to. Send only the steps you change: a step without an id is "
        "new, a step with an id changes that step, and steps you leave out stay as they are. Close a step with "
        "status 'closed' and 1 to 3 evidence refs from the goal block's list (a ref closes one step only); "
        "'dropped' needs none. A closed step cannot change. If any step is refused, nothing is written. Does not "
        "end the turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "The steps to add or change (not the whole plan).",
                "maxItems": MAX_STEPS,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The id of a step to change (s1, s2 ...)."},
                        "text": {"type": "string", "description": f"At most {STEP_TEXT_MAX} characters."},
                        "status": {"type": "string", "enum": list(STEP_STATUSES)},
                        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": STEP_EVIDENCE_MAX},
                    },
                },
            }
        },
        "required": ["steps"],
    },
}

Executor = Callable[..., Awaitable[tuple[str, bool]]]
FAILED = "Error: the {tool} call could not be completed; nothing was written. Record what you found with {other}."


@dataclass(frozen=True)
class GoalSessionRef:
    """The session a turn's root belongs to, as read when the turn began."""

    goal_id: UUID
    session_id: UUID
    session_n: int
    root_id: UUID
    started_at: datetime


class _Refused(Exception):
    """A call the tool will not carry out; the text says why, in words the model can act on."""


def _plain(text: str) -> str:
    """``text`` without NUL, control characters other than newline and tab, and invisible format characters."""
    return _visible(text)


def _one_line(text: str) -> str:
    return " ".join(_plain(text).split())


class GoalTools:
    """The two executors of one session's turn (``make_goal_tools``)."""

    def __init__(self, database: Database, settings: Any, store: GoalStore, ref: GoalSessionRef) -> None:
        self._db = database
        self._settings = settings
        self._store = store
        self._agent_id = store.agent_id
        self._ref = ref
        self._scope = evidence.Scope(
            agent_id=store.agent_id,
            root_id=ref.root_id,
            started_at=ref.started_at,
            workspace_dir=str(getattr(settings, "workspace_dir", "")),
        )

    # ---- the shared transaction --------------------------------------------------------------------------------

    async def _locked(self, s: AsyncSession) -> tuple[GoalSession, Goal]:
        """The session row, then the goal row, each ``FOR UPDATE``: the order the driver's close takes them."""
        row = (
            await s.execute(
                select(GoalSession)
                .where(GoalSession.id == self._ref.session_id, GoalSession.agent_id == self._agent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        goal = (
            await s.execute(
                select(Goal).where(Goal.id == self._ref.goal_id, Goal.agent_id == self._agent_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or goal is None:
            raise _Refused("this goal or its session is gone; nothing was written")
        if goal.state not in WRITABLE_STATES:
            raise _Refused(f"the goal is {goal.state}, so nothing more is recorded for it; nothing was written")
        return row, goal

    # ---- goal_note ---------------------------------------------------------------------------------------------

    async def goal_note(self, **kwargs: Any) -> tuple[str, bool]:
        try:
            return await self._goal_note(kwargs), False
        except _Refused as refused:
            return f"Error: {refused}.", True
        except Exception as exc:  # the class only: the error's text can repeat the call's
            logger.warning("F114: goal_note failed (%s)", type(exc).__name__, exc_info=True)
            return FAILED.format(tool="goal_note", other="resolve_intention's note"), True

    async def _goal_note(self, kwargs: dict[str, Any]) -> str:
        kind, text = kwargs.get("kind"), kwargs.get("text")
        if kind not in NOTE_KINDS:
            raise _Refused(f"kind must be one of {', '.join(NOTE_KINDS)}")
        if not isinstance(text, str) or not _plain(text).strip():
            raise _Refused("text is required: what this work did or found, or why the goal is done")
        body = _plain(text).strip()
        clipped = len(body) > JOURNAL_MAX_CHARS
        async with self._db.session() as s:
            row, goal = await self._locked(s)
            n, ended, state, reason = row.n, row.ended_at is not None, goal.state, goal.pause_reason
            self._store.add_journal_row(s, goal.id, kind, body, actor="chain", session_n=n)
            waiting = False
            if kind == "done_claim" and state == "active":
                waiting = await self._wait_for_owner(s, goal, row)
            await s.commit()
        said = "Noted" + (f" (clipped to {JOURNAL_MAX_CHARS} characters)" if clipped else "") + "."
        if ended:
            said += f" Session {n} has ended; the note is kept under it."
        if kind == "done_claim":
            if waiting:
                said += " The goal now waits for the owner, who confirms it done or resumes it."
            else:
                said += f" The goal is {state} ({reason}), so it stays as it is; the claim is kept for the owner."
        return said

    async def _wait_for_owner(self, s: AsyncSession, goal: Goal, row: GoalSession) -> bool:
        """A done claim on an active goal: paused for the owner (``waiting_for_owner``), its next wake cleared, a
        harness ``state`` row in fixed words, and one message to the owner (per session). The goal is locked."""
        result = await s.execute(
            update(Goal)
            .where(Goal.id == goal.id, Goal.agent_id == self._agent_id, Goal.state == "active")
            .values(state="paused", pause_reason="waiting_for_owner", next_wake_at=None, updated_at=_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        note = f"Waiting for the owner: session {row.n} claimed that the goal is done. Only the owner can finish it."
        self._store.add_journal_row(s, goal.id, "state", note, actor="harness", session_n=row.n)
        await self._tell_owner(s, goal, row)
        return True

    async def _tell_owner(self, s: AsyncSession, goal: Goal, row: GoalSession) -> None:
        """The done claim's message to the owner: one fixed sentence naming the goal by its ref and its title (the
        owner-approved charter's, on one line, quoted and bounded), and what only the owner can do. No text the chain
        wrote and no command line (a claimed message reaches the next chat turn)."""
        channel = continuation.owner_channel(self._settings, goal.origin_channel)
        if channel is None:
            logger.warning("F114: goal %s waits for the owner, who has no channel to be told on", goal.id)
            return
        label = format_short_id(goal.short_id)
        title = clip(neutralize(_one_line(goal.title)).replace('"', "'"), MESSAGE_TITLE_CHARS)
        body = (
            f'Goal {label} "{title}" says it is done; only you can finish it: on the Goals screen, confirm it done or '
            "resume it."
        )
        source_id = uuid.uuid5(DONE_NAMESPACE, f"{goal.id}:done:{row.n}")
        await continuation.insert_inbox_row(
            s,
            self._agent_id,
            source_kind=continuation.SOURCE_INTENTION_REPORT,
            source_id=source_id,
            msg_type=continuation.MSG_REPORT,
            title=f"Goal {label} may be done",
            body=body,
            channel=channel,
            correlation_id=str(source_id),
            intention_id=self._ref.root_id,
            push_after=await continuation.owner_push_after(
                s, self._agent_id, self._settings, channel=channel, root_id=self._ref.root_id
            ),
        )

    # ---- goal_plan ---------------------------------------------------------------------------------------------

    async def goal_plan(self, **kwargs: Any) -> tuple[str, bool]:
        try:
            return await self._goal_plan(kwargs), False
        except _Refused as refused:
            return f"Error: {refused}.", True
        except Exception as exc:
            logger.warning("F114: goal_plan failed (%s)", type(exc).__name__, exc_info=True)
            return FAILED.format(tool="goal_plan", other="goal_note"), True

    async def _goal_plan(self, kwargs: dict[str, Any]) -> str:
        entries = _entries(kwargs.get("steps"))
        async with self._db.session() as s:
            row, goal = await self._locked(s)
            plan = [dict(step) for step in goal.plan or [] if isinstance(step, dict)]
            changes, cited, problems = await self._apply(s, plan, entries, ended=row.ended_at is not None)
            if not problems and len(plan) > MAX_STEPS:
                problems.append(f"the plan would have {len(plan)} steps; it holds at most {MAX_STEPS}")
            if problems:
                raise _Refused("nothing was written: " + "; ".join(problems))
            if not changes:
                return "Nothing changed: every step you sent is already as you sent it."
            result = await s.execute(
                update(Goal)
                .where(Goal.id == goal.id, Goal.agent_id == self._agent_id, Goal.state.in_(WRITABLE_STATES))
                .values(plan=plan, updated_at=_now())
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise _Refused("the goal changed meanwhile; nothing was written")
            note = "\n".join(changes)
            if row.ended_at is not None:
                note += f"\n(Written after session {row.n} had ended.)"
            self._store.add_journal_row(s, goal.id, "plan", note, actor="chain", session_n=row.n, evidence=cited)
            await s.commit()
        closed = sum(1 for step in plan if step.get("status") == "closed")
        return f"Plan updated: {'; '.join(changes)}. It has {len(plan)} steps, {closed} closed."

    async def _apply(
        self, s: AsyncSession, plan: list[dict[str, Any]], entries: list[dict[str, Any]], *, ended: bool
    ) -> tuple[list[str], list[str], list[str]]:
        """Apply ``entries`` to ``plan`` in place: ``(changes, cited refs, problems)``. Each ref of a step being
        closed is checked against the lineage and against every ref already closing a step of the goal."""
        by_id = {step.get("id"): step for step in plan if isinstance(step.get("id"), str)}
        used = {
            ref: step.get("id")
            for step in plan
            if step.get("status") == "closed"
            for ref in step.get("evidence") or []
            if isinstance(ref, str)
        }
        changes: list[str] = []
        cited: list[str] = []
        problems: list[str] = []
        for entry in entries:
            step_id = entry.get("id")
            step = by_id.get(step_id) if step_id is not None else None
            label = step_id or "a new step"
            if step_id is not None and step is None:
                problems.append(f"there is no step {step_id!r}; leave out the id to add a new step")
                continue
            status = entry.get("status")
            if step is not None and step.get("status") == "closed":
                if _differs(step, entry):
                    problems.append(f"step {step_id} is closed and cannot change")
                continue
            if step is None and "text" not in entry:
                problems.append("a new step needs its text")
                continue
            if entry.get("evidence") is not None and status != "closed":
                problems.append(f"{label}: evidence is only for closing a step (status 'closed')")
                continue
            refs: list[str] = []
            if status == "closed":
                if ended:
                    problems.append(
                        f"{label}: this session has ended, so no step can be closed in it; record what you found "
                        "with goal_note, and a later session can close the step with its own evidence"
                    )
                    continue
                raw_refs = entry.get("evidence") or []
                if not 1 <= len(raw_refs) <= STEP_EVIDENCE_MAX:
                    problems.append(f"{label}: closing a step needs 1 to {STEP_EVIDENCE_MAX} evidence refs")
                    continue
                for raw in raw_refs:
                    checked = await evidence.check(s, self._scope, raw)
                    if checked.refusal is not None:
                        problems.append(f"{label}: {checked.refusal}")
                    elif checked.ref in used:
                        problems.append(f"{label}: {checked.ref} already closes step {used[checked.ref]}")
                    else:
                        used[checked.ref] = step_id or "new"
                        refs.append(checked.ref)
                if len(refs) != len(raw_refs):
                    continue
            if step is None:
                step = {"id": _next_id(by_id), "text": entry["text"], "status": "open", "evidence": []}
                plan.append(step)
                by_id[step["id"]] = step
                changes.append(f'added {step["id"]}: "{step["text"]}"')
            elif "text" in entry and entry["text"] != step.get("text"):
                step["text"] = entry["text"]
                changes.append(f'changed {step["id"]}: "{step["text"]}"')
            if status is not None and status != step.get("status"):
                if status == "closed":
                    step.update(status="closed", evidence=refs, closed_session=self._ref.session_n)
                    cited += refs
                    changes.append(f"closed {step['id']} with {', '.join(refs)}")
                else:
                    was = step.get("status")
                    step.update(status=status, evidence=[])
                    step.pop("closed_session", None)
                    verb = "dropped" if status == "dropped" else ("reopened" if was == "dropped" else "opened")
                    changes.append(f"{verb} {step['id']}")
        return changes, cited, problems


def _now() -> Any:
    return func.clock_timestamp()


def _differs(step: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Whether an entry for a closed step asks to change its text or its status (evidence sent again with it is
    not read: a closed step keeps the evidence it was closed with)."""
    if "text" in entry and entry["text"] != step.get("text"):
        return True
    return entry.get("status") not in (None, "closed")


def _next_id(by_id: dict[Any, Any]) -> str:
    taken = {int(m.group(1)) for key in by_id if isinstance(key, str) and (m := _STEP_ID.fullmatch(key))}
    k = max(taken, default=0) + 1
    return f"s{k}"


def _entries(steps: Any) -> list[dict[str, Any]]:
    """The steps of a call, each checked for its form (no database). Refused as a whole on the first bad one."""
    if not isinstance(steps, list) or not steps:
        raise _Refused("steps must be a non-empty list of step objects")
    if len(steps) > MAX_STEPS:
        raise _Refused(f"send at most {MAX_STEPS} steps")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            raise _Refused(f"step {n} must be an object with id, text, status or evidence")
        entry: dict[str, Any] = {}
        if raw.get("id") is not None:
            step_id = raw["id"]
            if not isinstance(step_id, str) or not step_id.strip() or len(step_id) > 20:
                raise _Refused(f"step {n}: id must be the id of a plan step, such as s1")
            step_id = step_id.strip()
            if step_id in seen:
                raise _Refused(f"step {step_id} is sent twice")
            seen.add(step_id)
            entry["id"] = step_id
        if raw.get("text") is not None:
            if not isinstance(raw["text"], str) or not _one_line(raw["text"]):
                raise _Refused(f"step {n}: text must be a sentence")
            text = _one_line(raw["text"])
            if len(text) > STEP_TEXT_MAX:
                raise _Refused(f"step {n}: text is {len(text)} characters; a step is at most {STEP_TEXT_MAX}")
            entry["text"] = text
        if raw.get("status") is not None:
            if raw["status"] not in STEP_STATUSES:
                raise _Refused(f"step {n}: status must be one of {', '.join(STEP_STATUSES)}")
            entry["status"] = raw["status"]
        if raw.get("evidence") is not None:
            refs = raw["evidence"]
            if (
                not isinstance(refs, list)
                or len(refs) > STEP_EVIDENCE_MAX
                or not all(isinstance(r, str) and len(r) <= STEP_EVIDENCE_REF_MAX for r in refs)
            ):
                raise _Refused(
                    f"step {n}: evidence is a list of at most {STEP_EVIDENCE_MAX} refs of at most "
                    f"{STEP_EVIDENCE_REF_MAX} characters"
                )
            if len({r.strip() for r in refs}) != len(refs):
                raise _Refused(f"step {n}: a ref is given twice")
            entry["evidence"] = [_plain(r).strip() for r in refs]
        if not entry:
            raise _Refused(f"step {n} says nothing: give an id, a text, a status or evidence")
        entries.append(entry)
    return entries


def make_goal_tools(
    database: Database, settings: Any, store: GoalStore, ref: GoalSessionRef
) -> dict[str, tuple[dict[str, Any], Executor]]:
    """The turn's goal tools in the ``extra_tools`` shape: ``{name: (schema, executor)}``. The F108 ``reason`` the
    runner adds to the offered schema is removed before the call; an executor ignores any argument it does not
    know."""
    tools = GoalTools(database, settings, store, ref)
    return {
        "goal_note": (GOAL_NOTE_SCHEMA, tools.goal_note),
        "goal_plan": (GOAL_PLAN_SCHEMA, tools.goal_plan),
    }
