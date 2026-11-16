"""F114 goals: the owner's goals over REST, for the dashboard's Goals screen.

Reads: ``GET /goals`` (every goal of the agent, with its spend), ``GET /goals/options`` (the presets and the ranges a
new goal's limits must fit) and ``GET /goals/{ref}`` (one goal in full). Changes: ``POST /goals`` (a new draft) and one
``POST /goals/{ref}/<action>`` per owner action, each calling the one ``GoalStore`` method of that name. Every route
requires the owner's or the proxy's secret (``nous/api/owner_guard.py``), checked before anything else: the flag, the
body and the goal are read only after it.

The actor of every change is the owner: the guard let the call in on the owner's or the proxy's secret, and either is
the owner. A body never names an actor (an unknown field is a 400). A refusal of the store is a 409 carrying the
store's own sentence. A goal of another agent is not found. When the store has made a change and reading the goal
back for the answer fails, the answer is 200 (201 for a new draft) with ``reload: true`` and the goal's state as the
store returned it.

With ``NOUS_GOALS_ENABLED`` off there is no store: the reads answer 200 with ``enabled: false`` and nothing else, and
the changes answer 503 ``GOALS_OFF``. Neither reads a table.

``actions`` in a goal's detail lists exactly the changes the store accepts for the goal as it is now (the store's own
rules, read in one place here; ``tests/test_goal_routes_actions.py`` drives the real store to hold the two equal), and
``resume_blocked`` says why a paused goal cannot be resumed yet.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from sqlalchemy import func, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api.owner_guard import OwnerGuard
from nous.brain.continuation import owner_channel
from nous.goals.envelope import _RANGES, PRESETS
from nous.goals.seed import SPEND_NOTE
from nous.goals.spend import goal_spent
from nous.goals.store import (
    DELIVERED_CHANNEL_KINDS,
    EVIDENCE_MAX_REFS,
    EVIDENCE_REF_MAX_CHARS,
    JOURNAL_MAX_CHARS,
    LIVE_STATES,
    MAX_DEADLINE_DAYS,
    STOP_REF,
    GoalRefused,
    GoalStore,
    clip,
    format_short_id,
)
from nous.storage.models import Goal, GoalJournal, GoalSession

logger = logging.getLogger(__name__)

GOALS_OFF = {"error": "goals are off", "goals_enabled": False}
STATES: tuple[str, ...] = ("draft", "active", "paused", "done", "failed", "cancelled")
FINISHED_STATES: tuple[str, ...] = ("done", "failed", "cancelled")
# The list's filters: a state, or one of these names (``open`` by default, so a poll never re-reads finished goals).
STATE_FILTERS: dict[str, tuple[str, ...] | None] = {
    "open": LIVE_STATES,
    "finished": FINISHED_STATES,
    "all": None,
    **{state: (state,) for state in STATES},
}
LIST_DEFAULT = 100
LIST_MAX = 200
SESSIONS_SHOWN = 50
PAUSE_SENTENCE_CHARS = 300
# At most this many problems of a body are listed; the rest are counted.
PROBLEMS_SHOWN = 5
# The owner's actions on a goal, in the order the screen shows them; each is the last segment of its route.
ACTIONS: tuple[str, ...] = (
    "approve",
    "run-now",
    "resume",
    "confirm-done",
    "pause",
    "stop",
    "budget",
    "deadline",
    "note",
    "cancel",
)
ACTOR = "owner"
JOURNAL_DEFAULT = 50
JOURNAL_MAX = 200
QUERY_NUMBER_DIGITS = 9  # a longer number in a query is refused, never converted
PRESET_MAX_CHARS = 20
ECHO_CHARS = 40  # at most this much of a field name the caller sent is said back
NOTE_MAX_CHARS = JOURNAL_MAX_CHARS
# The text bounds of brain.goals (the store refuses longer text; the body model refuses it first, with the field).
TITLE_MAX_CHARS = 200
OBJECTIVE_MAX_CHARS = 4000
DONE_WHEN_MAX_CHARS = 2000
CHANNEL_MAX_CHARS = 200
REF_ECHO_CHARS = 40
FAILED = "the request could not be processed"

Handler = Callable[[Request], Awaitable[Response]]


# ---- bodies ------------------------------------------------------------------------------------------------------


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


# A text field: a string, never coerced, with no NUL (the database's text cannot hold one).
Text = Annotated[StrictStr, AfterValidator(_no_nul)]


class _Body(BaseModel):
    """A request body: an unknown field is refused, and no value is coerced from another type."""

    model_config = ConfigDict(extra="forbid")


class NoBody(_Body):
    """The body of an action that takes nothing: empty, or ``{}``."""


class Limits(_Body):
    """A new goal's own limits; any left out come from the preset (``envelope_for``)."""

    total_budget: StrictInt | None = None
    day_budget: StrictInt | None = None
    deadline_days: StrictInt | None = None
    sessions_per_day: StrictInt | None = None
    min_interval_seconds: StrictInt | None = None
    jobs_per_day: StrictInt | None = None
    quiet_hours_ok: StrictBool | None = None


class CreateGoal(_Body):
    """``POST /goals``: a new draft. ``origin_channel`` is ``telegram:<chat id>``, or none for the owner's default."""

    title: Text = Field(max_length=TITLE_MAX_CHARS)
    objective: Text = Field(max_length=OBJECTIVE_MAX_CHARS)
    done_when: Text = Field(max_length=DONE_WHEN_MAX_CHARS)
    preset: Text = Field(default="small", max_length=PRESET_MAX_CHARS)
    limits: Limits | None = None
    origin_channel: Text | None = Field(default=None, max_length=CHANNEL_MAX_CHARS)


class BudgetChange(_Body):
    """``POST /goals/{ref}/budget``: a new total and/or day budget, in tokens."""

    total: StrictInt | None = None
    day: StrictInt | None = None


class DeadlineChange(_Body):
    """``POST /goals/{ref}/deadline``: this many more days."""

    days: StrictInt


class NoteBody(_Body):
    """``POST /goals/{ref}/note``: a note the next session reads."""

    text: Text = Field(max_length=NOTE_MAX_CHARS)


class _BadRequest(Exception):
    """A body or a query the route refuses (400); the text names the field and the rule, never the value."""


def _field(loc: tuple[Any, ...]) -> str:
    """The field an error names, as ``limits.total_budget``: each part cut to ``ECHO_CHARS`` (an unknown field's
    name is the caller's text)."""
    return ".".join(clip(str(part), ECHO_CHARS) for part in loc) or "body"


def _whole_number(raw: str) -> int | None:
    """A query's whole number of at most ``QUERY_NUMBER_DIGITS`` digits, or None."""
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= QUERY_NUMBER_DIGITS else None


async def _parse(request: Request, model: type[_Body]) -> Any:
    """The body as ``model``: an empty body is ``{}``. Raises ``_BadRequest``."""
    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):
        raise _BadRequest("the body must be JSON") from None
    if not isinstance(data, dict):
        raise _BadRequest("the body must be a JSON object")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = [f"{_field(error['loc'])}: {error['msg']}" for error in exc.errors()]
        more = len(problems) - PROBLEMS_SHOWN
        listed = "; ".join(problems[:PROBLEMS_SHOWN])
        raise _BadRequest(f"{listed}; and {more} more" if more > 0 else listed) from None


# ---- what a goal offers ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tally:
    """A goal's sessions in numbers: tokens spent in all and today, as ``nous.goals.spend.goal_spent`` counts them,
    and the number of its open session."""

    spent_total: int = 0
    spent_today: int = 0
    open_session: int | None = None


def resume_blocked(goal: Goal, *, spent: int, now: datetime) -> str | None:
    """Why ``GoalStore.resume`` refuses a paused goal now: ``"budget"`` while its total is spent, ``"deadline"`` while
    its deadline has passed (each only when it was paused for that), else None."""
    if goal.state != "paused":
        return None
    if goal.pause_reason == "budget" and spent >= goal.total_budget:
        return "budget"
    if goal.pause_reason == "deadline" and goal.deadline_at is not None and goal.deadline_at <= now:
        return "deadline"
    return None


def pause_sentence_query(agent_id: str, goal_id: UUID, reason: str) -> Any:
    """The newest ``state`` row the harness wrote for ``goal_id`` with the evidence ref of a pause for ``reason``
    (``stop:<reason>``): the goal driver writes one with each pause it makes (a stop condition, or failed starts), in
    the transaction that pauses the goal. Every other harness row (a session's close, a chain being stopped) has no
    such ref, so none written after the pause takes its place."""
    return (
        select(GoalJournal.text)
        .where(
            GoalJournal.agent_id == agent_id,
            GoalJournal.goal_id == goal_id,
            GoalJournal.kind == "state",
            GoalJournal.actor == "harness",
            GoalJournal.evidence.contains([f"{STOP_REF}{reason}"]),
        )
        .order_by(GoalJournal.seq.desc())
        .limit(1)
    )


def _can_extend(goal: Goal, now: datetime) -> bool:
    """Whether ``extend_deadline`` accepts one more day: a draft's length stays within the 90 days, an approved goal's
    new deadline within 90 days from now."""
    if goal.deadline_at is None:
        return goal.deadline_days + 1 <= MAX_DEADLINE_DAYS
    return max(goal.deadline_at, now) + timedelta(days=1) <= now + timedelta(days=MAX_DEADLINE_DAYS)


def available_actions(goal: Goal, *, tally: Tally, now: datetime, settings: Any) -> list[str]:
    """The owner's actions ``GoalStore`` accepts for ``goal`` now, in ``ACTIONS`` order. A finished goal has none.
    ``budget`` and ``note`` are offered for any open goal: the store then refuses only a value that breaks a rule."""
    if goal.state not in LIVE_STATES:
        return []
    session_open = tally.open_session is not None
    offered = {"cancel", "budget", "note"}
    if goal.state == "draft" and owner_channel(settings, goal.origin_channel) is not None:
        offered.add("approve")
    if goal.state == "active":
        offered.add("pause")
        offered.add("stop")
        if not session_open:
            offered.add("run-now")
    if goal.state == "paused":
        if resume_blocked(goal, spent=tally.spent_total, now=now) is None:
            offered.add("resume")
        if goal.pause_reason == "waiting_for_owner":
            offered.add("confirm-done")
        if session_open:
            offered.add("stop")
    if _can_extend(goal, now):
        offered.add("deadline")
    return [action for action in ACTIONS if action in offered]


# ---- the answers -------------------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def masked_channel(channel: str | None) -> str | None:
    """``telegram:****4242``: the channel's kind and the last four characters of its id, never the whole id."""
    if not channel:
        return None
    kind, _colon, chat = channel.partition(":")
    tail = chat[-4:] if len(chat) > 4 else ""
    return f"{kind}:****{tail}"


def _plan_steps(plan: Any) -> list[dict[str, Any]]:
    """The plan as stored, read defensively: entries that are not objects are dropped, and of each step only the
    known keys of the right type are kept (``text`` cut to the journal's bound, at most ten evidence refs)."""
    if not isinstance(plan, list):
        return []
    steps: list[dict[str, Any]] = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        step: dict[str, Any] = {}
        if isinstance(entry.get("text"), str):
            step["text"] = clip(entry["text"], JOURNAL_MAX_CHARS)
        if isinstance(entry.get("status"), str):
            step["status"] = clip(entry["status"], 40)
        evidence = entry.get("evidence")
        if isinstance(evidence, list):
            refs = [clip(ref, EVIDENCE_REF_MAX_CHARS) for ref in evidence if isinstance(ref, str)]
            step["evidence"] = refs[:EVIDENCE_MAX_REFS]
        closed = entry.get("closed_session")
        if isinstance(closed, int) and not isinstance(closed, bool):
            step["closed_session"] = closed
        steps.append(step)
    return steps


def goal_row(goal: Goal, tally: Tally) -> dict[str, Any]:
    """A goal as the list shows it."""
    return {
        "id": str(goal.id),
        "ref": format_short_id(goal.short_id),
        "title": goal.title,
        "state": goal.state,
        "pause_reason": goal.pause_reason,
        "preset": goal.preset,
        "proposed_by": goal.proposed_by,
        "total_budget": goal.total_budget,
        "day_budget": goal.day_budget,
        "spent_total": tally.spent_total,
        "spent_today": tally.spent_today,
        "deadline_at": _iso(goal.deadline_at),
        "next_wake_at": _iso(goal.next_wake_at),
        "open_session": tally.open_session,
        "pending_request": goal.pending_request,
        "mode": goal.mode,
        "updated_at": _iso(goal.updated_at),
    }


def goal_detail(
    goal: Goal,
    tally: Tally,
    *,
    now: datetime,
    sessions: list[GoalSession],
    sessions_total: int,
    journal: list[GoalJournal],
    pause_sentence: str | None,
    settings: Any,
) -> dict[str, Any]:
    """A goal in full: its row, charter, limits, plan, its newest sessions (the oldest of them first) and how many it
    has in all, journal (newest first), why the harness paused it, the actions the store accepts now and the
    sentence of what its spend does not count."""
    return {
        "enabled": True,
        **goal_row(goal, tally),
        "objective": goal.objective,
        "done_when": goal.done_when,
        "origin_channel": masked_channel(goal.origin_channel),
        "deadline_days": goal.deadline_days,
        "sessions_per_day": goal.sessions_per_day,
        "min_interval_seconds": goal.min_interval_seconds,
        "jobs_per_day": goal.jobs_per_day,
        "quiet_hours_ok": goal.quiet_hours_ok,
        "stall_limit": goal.stall_limit,
        "mode_until": _iso(goal.mode_until),
        "approved_at": _iso(goal.approved_at),
        "finished_at": _iso(goal.finished_at),
        "created_at": _iso(goal.created_at),
        "plan": _plan_steps(goal.plan),
        "sessions": [
            {
                "n": row.n,
                "trigger": row.trigger,
                "started_at": _iso(row.started_at),
                "ended_at": _iso(row.ended_at),
                "outcome": row.outcome,
                "spent": row.spent,
                "steps_closed": row.steps_closed,
                "summary": row.summary,
            }
            for row in sessions
        ],
        "sessions_total": sessions_total,
        "journal": [
            {
                "seq": row.seq,
                "session_n": row.session_n,
                "kind": row.kind,
                "actor": row.actor,
                "text": row.text,
                "evidence": [ref for ref in (row.evidence or []) if isinstance(ref, str)],
                "created_at": _iso(row.created_at),
            }
            for row in journal
        ],
        "pause_sentence": pause_sentence,
        "actions": available_actions(goal, tally=tally, now=now, settings=settings),
        "resume_blocked": resume_blocked(goal, spent=tally.spent_total, now=now),
        "spend_note": SPEND_NOTE,
    }


def made_only(goal: Goal) -> dict[str, Any]:
    """What a change answers when it was made and the goal could not be read back in full: the goal's state as the
    store returned it, and ``reload: true`` (read ``GET /goals/{ref}`` for the rest)."""
    return {
        "enabled": True,
        "reload": True,
        "id": str(goal.id),
        "ref": format_short_id(goal.short_id),
        "title": goal.title,
        "state": goal.state,
        "pause_reason": goal.pause_reason,
        "pending_request": goal.pending_request,
    }


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _no_goal(ref: str) -> JSONResponse:
    return _error(404, f"no goal {clip(ref, REF_ECHO_CHARS)}")


# ---- the routes --------------------------------------------------------------------------------------------------


def build_goal_routes(*, database: Any, settings: Any, goals: GoalStore | None, guard: OwnerGuard) -> list[Route]:
    """The goal routes, every one behind ``guard``. ``goals`` is None with the flag off. Literal paths come before
    ``/goals/{ref}``."""
    agent_id = settings.agent_id

    async def tallies(goal_ids: list[UUID]) -> tuple[datetime, dict[UUID, Tally]]:
        """The database's clock, and each goal's ``Tally``: its spend from ``goal_spent`` (one query a goal) and its
        open session (one query for all)."""
        async with database.session() as session:
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if not goal_ids:
                return now, {}
            open_rows = await session.execute(
                select(GoalSession.goal_id, GoalSession.n).where(
                    GoalSession.agent_id == agent_id,
                    GoalSession.goal_id.in_(goal_ids),
                    GoalSession.ended_at.is_(None),
                )
            )
            open_by_goal = {goal_id: n for goal_id, n in open_rows.all()}
            found: dict[UUID, Tally] = {}
            for goal_id in goal_ids:
                total, today = await goal_spent(session, agent_id, goal_id)
                found[goal_id] = Tally(total, today, open_by_goal.get(goal_id))
        return now, found

    async def newest_sessions(goal_id: UUID) -> tuple[list[GoalSession], int]:
        """The goal's newest ``SESSIONS_SHOWN`` sessions, the oldest of them first, and how many it has in all."""
        mine = (GoalSession.agent_id == agent_id, GoalSession.goal_id == goal_id)
        async with database.session() as session:
            total = await session.scalar(select(func.count()).select_from(GoalSession).where(*mine))
            rows = await session.execute(
                select(GoalSession).where(*mine).order_by(GoalSession.n.desc()).limit(SESSIONS_SHOWN)
            )
            newest = list(rows.scalars())
        return newest[::-1], int(total or 0)

    async def harness_pause_sentence(goal: Goal) -> str | None:
        """Why the harness paused ``goal``, in its own words (``pause_sentence_query``), cut to
        ``PAUSE_SENTENCE_CHARS``; None for a goal that is not paused, or paused by the owner."""
        if goal.state != "paused" or goal.pause_reason in (None, "owner"):
            return None
        async with database.session() as session:
            found = await session.scalar(pause_sentence_query(agent_id, goal.id, goal.pause_reason))
        return clip(found, PAUSE_SENTENCE_CHARS) if found is not None else None

    async def detail(goal: Goal, journal_limit: int = JOURNAL_DEFAULT) -> dict[str, Any]:
        assert goals is not None
        now, tally = await tallies([goal.id])
        sessions, sessions_total = await newest_sessions(goal.id)
        return goal_detail(
            goal,
            tally.get(goal.id, Tally()),
            now=now,
            sessions=sessions,
            sessions_total=sessions_total,
            journal=await goals.journal(goal.id, limit=journal_limit),
            pause_sentence=await harness_pause_sentence(goal),
            settings=settings,
        )

    async def changed_answer(changed: Goal, what: str, status: int = 200) -> JSONResponse:
        """The answer of a change the store made: the goal in full. When reading it back fails, the change still
        stands, so the answer is ``status`` with what the store returned and ``reload: true``."""
        try:
            return JSONResponse(await detail(changed), status_code=status)
        except Exception as exc:
            logger.warning("F114: %s was made, and reading the goal back failed (%s)", what, type(exc).__name__)
            return JSONResponse(made_only(changed), status_code=status)

    def failed(what: str, exc: Exception) -> JSONResponse:
        # The class only: an error's text can repeat what it was given.
        logger.warning("F114: %s failed (%s)", what, type(exc).__name__)
        return _error(500, FAILED)

    async def listed_goals(states: tuple[str, ...] | None, limit: int) -> tuple[list[Goal], int]:
        """The newest ``limit`` goals in ``states`` (every state when None), by short id with the newest last, and how
        many there are in all."""
        mine = [Goal.agent_id == agent_id] + ([Goal.state.in_(states)] if states is not None else [])
        async with database.session() as session:
            total = await session.scalar(select(func.count()).select_from(Goal).where(*mine))
            rows = await session.execute(select(Goal).where(*mine).order_by(Goal.short_id.desc()).limit(limit))
            newest = list(rows.scalars())
        return newest[::-1], int(total or 0)

    async def list_goals(request: Request) -> Response:
        """GET /goals?state=open&limit=100"""
        if not goals:
            return JSONResponse({"enabled": False, "goals": []})
        wanted = request.query_params.get("state", "open")
        if wanted not in STATE_FILTERS:
            return _error(400, f"state must be one of {', '.join(STATES)}, open, finished or all")
        limit = _whole_number(request.query_params.get("limit", str(LIST_DEFAULT)))
        if limit is None or limit < 1:
            return _error(400, f"limit must be a whole number from 1 (at most {LIST_MAX} are sent)")
        try:
            listed, total = await listed_goals(STATE_FILTERS[wanted], min(limit, LIST_MAX))
            _now, by_goal = await tallies([g.id for g in listed])
        except Exception as exc:
            return failed("listing goals", exc)
        rows = [goal_row(g, by_goal.get(g.id, Tally())) for g in listed]
        return JSONResponse({"enabled": True, "goals": rows, "total": total})

    async def options(request: Request) -> Response:
        """GET /goals/options"""
        if not goals:
            return JSONResponse({"enabled": False})
        return JSONResponse(
            {
                "enabled": True,
                "presets": {name: asdict(limits) for name, limits in PRESETS.items()},
                "ranges": [{"field": f, "low": low, "high": high, "words": w} for f, low, high, w in _RANGES],
                # Whether a goal with no origin channel has somewhere to ask the owner: a boolean, never the chat id.
                "has_default_channel": owner_channel(settings, None) is not None,
                "channel_kinds": list(DELIVERED_CHANNEL_KINDS),
                "bounds": {
                    "title": TITLE_MAX_CHARS,
                    "objective": OBJECTIVE_MAX_CHARS,
                    "done_when": DONE_WHEN_MAX_CHARS,
                    "channel": CHANNEL_MAX_CHARS,
                    "note": NOTE_MAX_CHARS,
                    "deadline_days": MAX_DEADLINE_DAYS,
                    "journal": JOURNAL_MAX,
                },
                # No driver is started by the app yet: rows change, and no session follows.
                "driver_running": False,
                "spend_note": SPEND_NOTE,
            }
        )

    async def get_goal(request: Request) -> Response:
        """GET /goals/{ref}?journal=50"""
        if not goals:
            return JSONResponse({"enabled": False})
        journal = _whole_number(request.query_params.get("journal", str(JOURNAL_DEFAULT)))
        if journal is None or journal < 1:
            return _error(400, f"journal must be a whole number from 1 (at most {JOURNAL_MAX} are sent)")
        ref = request.path_params["ref"]
        try:
            goal = await goals.get(ref)
            if goal is None:
                return _no_goal(ref)
            return JSONResponse(await detail(goal, min(journal, JOURNAL_MAX)))
        except Exception as exc:
            return failed("reading a goal", exc)

    async def create(request: Request) -> Response:
        """POST /goals"""
        if not goals:
            return JSONResponse(GOALS_OFF, status_code=503)
        try:
            body: CreateGoal = await _parse(request, CreateGoal)
        except _BadRequest as exc:
            return _error(400, str(exc))
        try:
            made = await goals.create(
                title=body.title,
                objective=body.objective,
                done_when=body.done_when,
                preset=body.preset,
                overrides=body.limits.model_dump(exclude_none=True) if body.limits else None,
                proposed_by=ACTOR,
                origin_channel=body.origin_channel,
            )
        except GoalRefused as exc:
            return _error(409, str(exc))
        except Exception as exc:
            return failed("creating a goal", exc)
        return await changed_answer(made, "creating a goal", status=201)

    def action(name: str, model: type[_Body], call: Callable[[GoalStore, UUID, Any], Awaitable[Goal]]) -> Route:
        """``POST /goals/{ref}/<name>``: the body as ``model``, the goal looked up (404), then ``call`` (409 on a
        refusal), answered with the goal's detail (``changed_answer``)."""

        async def run(request: Request) -> Response:
            if not goals:
                return JSONResponse(GOALS_OFF, status_code=503)
            try:
                body = await _parse(request, model)
            except _BadRequest as exc:
                return _error(400, str(exc))
            ref = request.path_params["ref"]
            try:
                goal = await goals.get(ref)
                if goal is None:
                    return _no_goal(ref)
                changed = await call(goals, goal.id, body)
            except GoalRefused as exc:
                return _error(409, str(exc))
            except Exception as exc:
                return failed(name, exc)
            return await changed_answer(changed, name)

        run.__name__ = f"goal_{name.replace('-', '_')}"
        path = f"/goals/{{ref}}/{name}"
        return guard.route(path, run, methods=["POST"])

    def budget(store: GoalStore, goal_id: UUID, body: BudgetChange) -> Awaitable[Goal]:
        return store.raise_budget(goal_id, total=body.total, day=body.day, actor=ACTOR)

    return [
        guard.route("/goals", list_goals, methods=["GET"]),
        guard.route("/goals", create, methods=["POST"]),
        guard.route("/goals/options", options, methods=["GET"]),
        guard.route("/goals/{ref}", get_goal, methods=["GET"]),
        action("approve", NoBody, lambda store, gid, _b: store.approve(gid, actor=ACTOR)),
        action("pause", NoBody, lambda store, gid, _b: store.pause(gid, actor=ACTOR)),
        action("resume", NoBody, lambda store, gid, _b: store.resume(gid, actor=ACTOR)),
        action("confirm-done", NoBody, lambda store, gid, _b: store.confirm_done(gid, actor=ACTOR)),
        action("stop", NoBody, lambda store, gid, _b: store.stop(gid, actor=ACTOR)),
        action("cancel", NoBody, lambda store, gid, _b: store.cancel(gid, actor=ACTOR)),
        action("run-now", NoBody, lambda store, gid, _b: store.request_run_now(gid, actor=ACTOR)),
        action("budget", BudgetChange, budget),
        action("deadline", DeadlineChange, lambda store, gid, b: store.extend_deadline(gid, days=b.days, actor=ACTOR)),
        action("note", NoteBody, lambda store, gid, b: store.add_note(gid, b.text, actor=ACTOR)),
    ]
