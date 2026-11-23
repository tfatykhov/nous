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

F115 per-goal tools: ``GET /goals/{ref}/tools`` (the tools the goal may use without asking, each with what it does and
whether it can be used now and why not, and the tools it could be allowed, each with why not when it cannot be now),
``PUT /goals/{ref}/tools/{tool}`` (body ``{"confirm": true}``, and for an MCP client tool ``"identity"``, the token of
its server row, URL digest and remote tool name the screen showed) and ``DELETE /goals/{ref}/tools/{tool}``. The proof the guard accepted (``owner`` or
``proxy``) is stored with the grant; the journal's actor is the owner. With ``NOUS_GOAL_TOOL_GRANTS_ENABLED`` off the
read answers ``grants_enabled: false`` and the changes 503 ``GRANTS_OFF``. After a change, and after every action
that can change whether a goal is active, the runner's view of the goal's grants is read again; when that read fails
the answer carries ``reload: true`` and no tool of any goal is used without asking until a later read.

A goal that may run any command (``nous.goals.commands``, with ``NOUS_GOAL_COMMANDS_ENABLED`` on): ``POST /goals`` takes
``commands`` (the hours from approval and the sentence typed, ``commands.confirm_sentence``); ``POST
/goals/{ref}/contained`` returns a goal to contained (the one later change); ``GET /goals/options`` carries the form's
words only with the flag on; ``GET /goals/commands`` lists the goals that may run any command now, for the screen's
banner. A goal's detail says the state of its mode (``commands``) and whether its tasks run any command now. Each
refusal is the store's sentence, as written in ``commands``.

Goal pacing (``nous.goals.pacing``, with ``NOUS_GOALS_PACING_ENABLED`` on): ``POST /goals`` takes ``daily_at``
(``"HH:MM"``), a goal that works once a day from that time of the configured zone; ``POST /goals/{ref}/sessions``
changes a goal's session settings (``GoalStore.change_sessions``); a goal's detail says its ``daily_at`` and offers
``sessions``, and ``GET /goals/options`` carries ``pacing`` (the zone, the stall limit's range, the form's default
time). With the flag off none of these keys is sent, ``sessions`` is not offered, a create that carries ``daily_at``
is answered 503 ``PACING_OFF`` and ``POST /goals/{ref}/sessions`` is refused by the store (409).

What a goal is doing now (``nous.goals.activity``, with ``NOUS_GOAL_ACTIVITY_ENABLED`` on): ``GET
/goals/{ref}/activity`` sends the goal's open session (else its newest), its phase, the current task or workflow (by
its name, redacted and cut), and with the tool-call log on the session's call counts, job launches, newest calls (tool
name, time, outcome) and the session ids the Work screen opens; never a call's input, result, reason or error text.
``GET /goals/options`` carries ``activity: true`` with the flag on. With it off the route answers ``activity_enabled:
false`` and reads no table, and the options have no ``activity`` key.

The default goal prompt (``nous.goals.default_prompt``), which goal sessions read in their first task and their
follow-up turns: ``GET /goals/prompt`` (the text the builders start from, whether it is the built-in one, the built-in
one, the limit, and when and by which proof the owner's text was saved), ``PUT /goals/prompt`` (body ``{"text": ...}``:
normalized as ``normalize_goal_prompt`` says, it must show something, ``goal_prompt_has_text``, and be at most
``default_prompt.MAX_CHARS`` characters; the proof the guard accepted is stored with it) and ``DELETE /goals/prompt``
(back to the built-in text). When a save or a reset was made and reading it back fails, the answer is 200 with what was
written and ``reload: true``. With the goals flag off the read answers ``enabled: false`` and the changes 503
``GOALS_OFF``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from sqlalchemy import func, or_, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nous.api import tool_policy
from nous.api.autonomy_routes import registered_tools, tool_classes_words, tool_group
from nous.api.owner_guard import OwnerGuard
from nous.api.tool_classes import MCP_TOOL_PREFIX, McpIdentity, mcp_tool_background, mcp_tool_identity
from nous.autonomy import tool_guard
from nous.brain.continuation import owner_channel
from nous.goals import activity as goal_activity
from nous.goals import commands as goal_commands
from nous.goals import default_prompt, pacing
from nous.goals.envelope import _RANGES, PRESETS, STALL_RANGE
from nous.goals.grants import CREATE_CONFIRM, identity_token, lead_sentence, never_sentence, not_ready, words_for
from nous.goals.seed import SPEND_NOTE
from nous.goals.spend import goal_spent
from nous.goals.store import (
    DELIVERED_CHANNEL_KINDS,
    EVIDENCE_MAX_REFS,
    EVIDENCE_REF_MAX_CHARS,
    GRANTS_MAX,
    JOURNAL_MAX_CHARS,
    LIVE_STATES,
    MAX_DEADLINE_DAYS,
    STOP_REF,
    GoalRefused,
    GoalStore,
    clip,
    format_short_id,
    grant_identity,
)
from nous.identity.manager import GoalPromptChanged, IdentityManager, goal_prompt_has_text, normalize_goal_prompt
from nous.storage.models import Goal, GoalJournal, GoalSession, GoalToolGrant

logger = logging.getLogger(__name__)

GOALS_OFF = {"error": "goals are off", "goals_enabled": False}
GRANTS_OFF = {"error": "Tool grants are off on this server."}
PACING_OFF = {"error": pacing.CREATE_OFF}
DEFAULT_AT = "09:00"  # the time the create form offers for a goal that works once a day
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
    "sessions",
    "deadline",
    "note",
    "cancel",
    "contained",
)
ACTOR = "owner"
# The actions that can change whether a goal is active, or whether it may run any command: after each, its tool grants
# are read again (F115).
STATE_ACTIONS: frozenset[str] = frozenset({"approve", "pause", "resume", "stop", "cancel", "confirm-done", "contained"})
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
# The default goal prompt's body: a longer text is refused by the body model; up to it, the route counts it.
PROMPT_BODY_MAX_CHARS = 20_000
PROMPT_EMPTY = "The text is blank. To go back to the built-in text, use reset."
PROMPT_TOO_LONG = "The text is {n} characters; at most {max} are allowed."
PROMPT_CHANGED = "The prompt was changed a moment ago, so this text was not saved. Save it again to replace that."

Handler = Callable[[Request], Awaitable[Response]]


# ---- bodies ------------------------------------------------------------------------------------------------------


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


# A text field: a string, never coerced, with no NUL (the database's text cannot hold one).
Text = Annotated[StrictStr, AfterValidator(_no_nul)]


def _time_of_day(value: str | None) -> str | None:
    """A time of day as the owner writes it, ``"HH:MM"`` from 00:00 to 23:59."""
    if value is not None:
        try:
            pacing.parse_at(value)
        except ValueError:
            raise ValueError("must be a time of day written HH:MM, from 00:00 to 23:59") from None
    return value


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


class GrantItem(_Body):
    """One tool a new goal may use without asking (F115): its name, the owner's confirmation, and for an MCP client
    tool the ``identity`` the screen showed (``grants.identity_token``). The shape of
    ``PUT /goals/{ref}/tools/{tool}``'s body, with the tool's name."""

    tool: Text = Field(max_length=200)
    confirm: StrictBool | None = None
    identity: Text | None = Field(default=None, max_length=64)


class CommandsBody(_Body):
    """A new goal that may run any command: the hours from its approval, and the sentence the owner typed."""

    hours: StrictInt
    confirm: Text = Field(max_length=200)


class CreateGoal(_Body):
    """``POST /goals``: a new draft. ``origin_channel`` is ``telegram:<chat id>``, or none for the owner's default.
    ``tools`` (F115 per-goal tools): the tools it may use without asking, granted with it or not at all. ``commands``:
    its tasks may run any command for that many hours from its approval."""

    title: Text = Field(max_length=TITLE_MAX_CHARS)
    objective: Text = Field(max_length=OBJECTIVE_MAX_CHARS)
    done_when: Text = Field(max_length=DONE_WHEN_MAX_CHARS)
    preset: Text = Field(default="small", max_length=PRESET_MAX_CHARS)
    limits: Limits | None = None
    origin_channel: Text | None = Field(default=None, max_length=CHANNEL_MAX_CHARS)
    tools: list[GrantItem] | None = None
    commands: CommandsBody | None = None
    daily_at: Annotated[Text | None, AfterValidator(_time_of_day)] = Field(default=None, max_length=5)


class BudgetChange(_Body):
    """``POST /goals/{ref}/budget``: a new total and/or day budget, in tokens."""

    total: StrictInt | None = None
    day: StrictInt | None = None


class SessionsChange(_Body):
    """``POST /goals/{ref}/sessions``: new session settings; any left out (or null) stay as they are. ``daily: true``
    with ``daily_at`` (``"HH:MM"``) makes the goal work once a day from that time; ``daily: false`` makes it run as
    often as its limits allow."""

    sessions_per_day: StrictInt | None = None
    min_interval_seconds: StrictInt | None = None
    jobs_per_day: StrictInt | None = None
    quiet_hours_ok: StrictBool | None = None
    stall_limit: StrictInt | None = None
    daily: StrictBool | None = None
    daily_at: Annotated[Text | None, AfterValidator(_time_of_day)] = Field(
        default=None, max_length=5, validate_default=True
    )

    @field_validator("daily_at")
    @classmethod
    def _with_daily(cls, value: str | None, info: ValidationInfo) -> str | None:
        """A time goes with ``daily: true``, and ``daily: true`` needs one."""
        daily = info.data.get("daily")
        if value is not None and daily is not True:
            raise ValueError("a time of day goes with daily: true")
        if value is None and daily is True:
            raise ValueError("daily: true needs a time of day")
        return value

    def changes(self) -> dict[str, Any]:
        """The store's ``changes``: each setting given, and ``daily_at_minute`` when ``daily`` is."""
        given = self.model_dump(exclude={"daily", "daily_at"}, exclude_none=True)
        if self.daily is not None:
            given["daily_at_minute"] = pacing.parse_at(self.daily_at) if self.daily else None
        return given


class DeadlineChange(_Body):
    """``POST /goals/{ref}/deadline``: this many more days."""

    days: StrictInt


class NoteBody(_Body):
    """``POST /goals/{ref}/note``: a note the next session reads."""

    text: Text = Field(max_length=NOTE_MAX_CHARS)


class GrantBody(_Body):
    """``PUT /goals/{ref}/tools/{tool}``: the owner's confirmation, and for an MCP client tool the ``identity`` the
    screen showed (``grants.identity_token``: its server row, URL digest and remote tool name)."""

    confirm: StrictBool | None = None
    identity: Text | None = Field(default=None, max_length=64)


class PromptBody(_Body):
    """``PUT /goals/prompt``: the owner's goal prompt."""

    text: Text = Field(max_length=PROMPT_BODY_MAX_CHARS)


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
    (``stop:<reason>``): the goal driver writes one with each pause it makes for a reason other than the owner's (a
    stop condition, or failed starts), in the transaction that pauses the goal. Every other harness row (a session's
    close, a chain being stopped) has no such ref, so none written after the pause takes its place."""
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
    ``budget`` and ``note`` are offered for any open goal, and so is ``sessions`` with goal pacing on: the store then
    refuses only a value that breaks a rule."""
    if goal.state not in LIVE_STATES:
        return []
    session_open = tally.open_session is not None
    offered = {"cancel", "budget", "note"}
    if pacing.zone(settings) is not None:
        offered.add("sessions")
    if goal.state == "draft" and owner_channel(settings, goal.origin_channel) is not None:
        offered.add("approve")
    if goal.state == "active":
        offered.add("pause")
        offered.add("stop")
        if not session_open:
            offered.add("run-now")
    if goal.mode != goal_commands.MODE_CONTAINED:
        offered.add("contained")
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


def _at(minute: int | None) -> str | None:
    return pacing.format_at(minute) if minute is not None else None


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


def commands_state(goal: Goal, *, now: datetime, settings: Any, ended: str | None) -> dict[str, Any]:
    """Whether ``goal`` may run any command, as its page shows it: ``state`` (``off``; ``set``, a draft's choice;
    ``on``, with its end; ``ended``, with the text of the row that ended it), the sentence it ``says``
    (``commands.WORDS``), whether its tasks run any command now (``usable``) and why not, and for an armed draft what
    approving it starts (``approve``)."""
    words = goal_commands.WORDS
    if goal.mode == goal_commands.MODE_CONTAINED:
        state, says = ("ended", words["ended"].format(reason=ended)) if ended else ("off", words["off"])
        return {"state": state, "says": says, "usable": False, "not_usable_why": None, "approve": None}
    hours = goal_commands.hours_words(int(goal.mode_hours or 0))
    if goal.mode_until is None:
        state, says = "set", words["set"].format(hours=hours)
    else:
        state, says = "on", words["on"].format(until=f"{goal.mode_until:%Y-%m-%d %H:%M}")
    why = goal_commands.not_usable(goal, now=now, settings=settings)
    approve = words["approve"].format(hours=hours) if goal.state == "draft" else None
    return {"state": state, "says": says, "usable": why is None, "not_usable_why": why, "approve": approve}


def mode_row_query(agent_id: str, goal_id: UUID) -> Any:
    """The newest row of a change of ``goal_id``'s mode (owner, host or harness, with a ``mode:`` ref): its text and
    evidence."""
    refs = [f"{goal_commands.MODE_REF}{state}" for state in ("armed", "on", "off")]
    return (
        select(GoalJournal.text, GoalJournal.evidence)
        .where(
            GoalJournal.agent_id == agent_id,
            GoalJournal.goal_id == goal_id,
            GoalJournal.kind == "state",
            GoalJournal.actor.in_(("owner", "host", "harness")),
            or_(*(GoalJournal.evidence.contains([ref]) for ref in refs)),
        )
        .order_by(GoalJournal.seq.desc())
        .limit(1)
    )


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
    mode_ended: str | None = None,
) -> dict[str, Any]:
    """A goal in full: its row, charter, limits, plan, its newest sessions (the oldest of them first) and how many it
    has in all, journal (newest first), why the harness paused it, the actions the store accepts now, the
    sentence of what its spend does not count, and whether it may run any command (``commands_state``; ``mode_ended``
    is the text of the row that ended its mode, when that is its newest change of mode)."""
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
        # Goal pacing, with its flag on: the time of day of a goal that works once a day, or None.
        **({"daily_at": _at(goal.daily_at_minute)} if pacing.zone(settings) is not None else {}),
        "mode_until": _iso(goal.mode_until),
        "mode_hours": goal.mode_hours,
        "commands": commands_state(goal, now=now, settings=settings, ended=mode_ended),
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


# ---- the tools a goal may use without asking (F115) ------------------------------------------------------------

TOOL_ECHO_CHARS = 64
NOT_READ_SENTENCE = "The saved tool settings could not be read, so nothing can be changed until they can."
SERVER_CHANGED_SENTENCE = "That server changed since you looked. Look again."
CONFIRM_SENTENCE = "Allowing a tool needs your confirmation."


@dataclass(frozen=True)
class GrantRefusal:
    """Why the owner cannot allow a goal a tool now: the answer's status, a code for the screen and the sentence."""

    status: int
    code: str
    sentence: str


def grant_refusal_for(tool: str, *, registered: bool, settings: Any) -> GrantRefusal | None:
    """Why the owner cannot allow a goal to use ``tool`` now, or None: from ``tool_policy.grant_refusal``, whether
    Nous has the tool now, the tool guard (a host lock, the owner's setting, settings not read or no guard), an MCP
    client tool's server switch, and the setting the tool needs first (``grants.not_ready``)."""
    name = clip(tool, TOOL_ECHO_CHARS)
    code = tool_policy.grant_refusal(tool)
    if code in ("never", "harness"):
        return GrantRefusal(400, "never", f"{name} can never be given to a goal.")
    if code == "spawns":
        return GrantRefusal(400, "spawns", f"{name} starts other work and can never be given to a goal.")
    if code == "not_outward":
        return GrantRefusal(400, "not_outward", f"{name} needs no permission: a goal can use it already, or never can.")
    mcp = tool.startswith(MCP_TOOL_PREFIX)
    identity = mcp_tool_identity(tool) if mcp else None
    if code is not None or not registered or (mcp and (mcp_tool_background(tool) is None or identity is None)):
        return GrantRefusal(400, "not_registered", f"{name} is not a tool Nous has now.")
    guard = tool_guard.current()
    held = guard.reason("continuation", None, tool) if guard is not None else tool_guard.NOT_READ
    if held == tool_guard.NOT_READ:
        return GrantRefusal(503, "not_read", NOT_READ_SENTENCE)
    if held == tool_guard.LOCK:
        return GrantRefusal(409, "locked", f"{name} is locked by the server and cannot be given to a goal.")
    if held == tool_guard.OWNER:
        return GrantRefusal(
            409,
            "switched_off",
            f"{name} is switched off for background work. Set it to Allowed under Limits and tools first.",
        )
    if mcp and identity is not None and not mcp_tool_background(tool):
        return GrantRefusal(
            409,
            "server_off",
            f"{identity.server_name} is not available to background work. Turn that on for the server on the "
            "Integrations screen first.",
        )
    ready = not_ready(tool, settings)
    if ready is not None:
        return GrantRefusal(409, "not_ready", ready)
    return None


def goal_not_running(goal: Goal) -> str | None:
    """Why no grant of ``goal`` is used now because of the goal's own state, or None for an active goal: grants
    reach only the open session of an active goal."""
    if goal.state == "active":
        return None
    if goal.state == "draft":
        return "The goal is a draft: nothing runs until you approve it."
    if goal.state == "paused":
        return "The goal is paused."
    return "The goal is finished."


def grant_not_usable(tool: str, identity: McpIdentity | None, *, registered: bool, settings: Any) -> str | None:
    """Why a goal's grant of ``tool`` is not used now, in words, from the checks the runner makes; None when it is
    (while the goal is active and a session is open)."""
    guard = tool_guard.current()
    if guard is None:
        return "The tool settings are not loaded on this server, so no goal's tools are used without asking."
    held = guard.reason("continuation", None, tool)
    if held == tool_guard.LOCK:
        return "Locked by the server."
    if held == tool_guard.NOT_READ:
        return "Held until the saved tool settings can be read."
    if held == tool_guard.OWNER:
        return "Switched off for background work under Limits and tools."
    if tool.startswith(MCP_TOOL_PREFIX):
        background = mcp_tool_background(tool)
        if background is None:
            return "Its server is not connected now."
        if mcp_tool_identity(tool) != identity:
            return "Its server changed since you allowed it. Remove it, look at the server, and allow it again."
        if not background:
            return "Its server is not available to background work."
    elif not registered:
        return "Nous does not have this tool now."
    return not_ready(tool, settings)


def tool_words(tool: str, identity: McpIdentity | None, ref: str) -> dict[str, Any]:
    """What a tool does when a goal may use it, as the screen shows it: ``does`` and ``note`` (``grants.words_for``),
    ``says`` (both as sentences) and ``confirm`` (the question asked before allowing it)."""
    does, note = words_for(tool, identity)
    tail = f" {note}" if note else ""
    return {
        "label": identity.remote_tool if identity is not None else tool,
        "group": tool_group(tool),
        "classes": tool_classes_words(tool),
        "does": does,
        "note": note,
        "says": f"{does[:1].upper()}{does[1:]}.{tail}",
        "confirm": f"Allow {ref} to {does} without asking you each time?{tail}",
        "server_name": identity.server_name if identity is not None else None,
        "server_host": identity.server_host if identity is not None else None,
        "server_id": str(identity.server_id) if identity is not None else None,
        "identity": identity_token(identity) if identity is not None else None,
    }


# ---- the routes --------------------------------------------------------------------------------------------------


def build_goal_routes(
    *,
    database: Any,
    settings: Any,
    goals: GoalStore | None,
    guard: OwnerGuard,
    driver: Any = None,
    dispatcher: Any = None,
    grants: Any = None,
) -> list[Route]:
    """The goal routes, every one behind ``guard``. ``goals`` is None with the flag off. ``driver`` is the app's
    goal driver component (main.py passes its lazy proxy, falsy until the lifespan built one), read at each request;
    so are ``dispatcher`` (the tool dispatcher) and ``grants`` (the view of the goals' tool grants, F115 per-goal
    tools; falsy with that flag off). Literal paths come before ``/goals/{ref}``."""
    agent_id = settings.agent_id

    async def refresh_grants(goal_id: UUID) -> bool:
        """Read the goal's tool grants into the runner's view again; False when that failed (the view is then empty
        until a later read). Nothing to read with the flag off."""
        if not grants:
            return True
        try:
            return bool(await grants.refresh_goal(goal_id))
        except Exception as exc:
            logger.warning("F115: the goal's tool grants were not read again (%s)", type(exc).__name__)
            return False

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

    async def mode_ended(goal: Goal) -> str | None:
        """The text of the row that ended ``goal``'s mode, when its newest change of mode is an end."""
        async with database.session() as session:
            found = (await session.execute(mode_row_query(agent_id, goal.id))).first()
        if found is None or f"{goal_commands.MODE_REF}off" not in (found.evidence or []):
            return None
        return clip(found.text, PAUSE_SENTENCE_CHARS)

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
            mode_ended=await mode_ended(goal),
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
                # Whether the app's goal driver runs its loop now: without it, rows change and no session follows.
                "driver_running": bool(driver) and driver.running is True,
                "spend_note": SPEND_NOTE,
                # F115 per-goal tools, with that flag on: what a new goal may be allowed when it is created.
                **({"tools": new_goal_tools()} if grants_on() else {}),
                # With goals that may run any command: the form's words, the sentence to type and the ceiling.
                **({"commands": commands_options()} if goal_commands.enabled(settings) else {}),
                # Goal pacing, with its flag on: the zone of the days and times, and the forms' bounds and default.
                **({"pacing": pacing_options()} if pacing.zone(settings) is not None else {}),
                # What a goal is doing now and the folding page, with that flag on.
                **({"activity": True} if activity_on() else {}),
            }
        )

    def activity_on() -> bool:
        return getattr(settings, "goal_activity_enabled", False) is True

    def pacing_options() -> dict[str, Any]:
        tz = pacing.zone(settings)
        assert tz is not None
        low, high = STALL_RANGE
        return {"zone": tz.key, "stall_limit": {"low": low, "high": high}, "default_at": DEFAULT_AT}

    def commands_options() -> dict[str, Any]:
        return {
            "max_hours": goal_commands.max_hours(settings),
            "sentence": goal_commands.CONFIRM_TEMPLATE,
            "hour_one": goal_commands.HOUR_ONE,
            "hours_many": goal_commands.HOURS_MANY,
            "words": dict(goal_commands.WORDS),
            "refusals": {"hours_range": goal_commands.HOURS_RANGE, "confirm": goal_commands.CONFIRM},
        }

    async def commands_now(request: Request) -> Response:
        """GET /goals/commands: the goals whose tasks may run any command now, for the screen's banner."""
        if not goals:
            return JSONResponse({"enabled": False, "allowed": False, "active": []})
        if not goal_commands.enabled(settings):
            return JSONResponse({"enabled": True, "allowed": False, "active": []})
        query = (
            select(Goal)
            .where(
                Goal.agent_id == agent_id,
                Goal.state == "active",
                Goal.mode == goal_commands.MODE_COMMANDS,
                Goal.mode_until.is_not(None),
                Goal.mode_until > func.clock_timestamp(),
            )
            .order_by(Goal.short_id)
        )
        try:
            async with database.session() as session:
                rows = list((await session.execute(query)).scalars())
        except Exception as exc:
            return failed("listing the goals that may run any command", exc)
        active = []
        for goal in rows:
            ref, until = format_short_id(goal.short_id), f"{goal.mode_until:%Y-%m-%d %H:%M}"
            says = goal_commands.WORDS["banner"].format(ref=ref, until=until)
            active.append({"ref": ref, "title": goal.title, "until": _iso(goal.mode_until), "says": says})
        return JSONResponse({"enabled": True, "allowed": True, "active": active})

    # ---- the default goal prompt ---------------------------------------------------------------------------------

    def prompt_shape(
        text: str, is_default: bool, updated_at: Any = None, updated_by: str | None = None
    ) -> dict[str, Any]:
        return {
            "enabled": True,
            "text": text,
            "is_default": is_default,
            "default": default_prompt.DEFAULT_GOAL_PROMPT,
            "max_chars": default_prompt.MAX_CHARS,
            "updated_at": _iso(updated_at),
            "updated_by": updated_by,
        }

    async def prompt_answer() -> dict[str, Any]:
        """The goal prompt the builders start from (``default_prompt.effective``), the built-in text and the limit;
        when the owner's text is the one, when and by which proof it was saved."""
        row = await IdentityManager(database, agent_id).get_goal_prompt()
        text, is_default = default_prompt.effective(row.text if row is not None else None)
        if row is None or is_default:
            return prompt_shape(text, is_default)
        return prompt_shape(text, False, row.updated_at, row.updated_by)

    async def changed_prompt(what: str, written: dict[str, Any]) -> JSONResponse:
        """The answer of a save or a reset that was made: the read again, or, when that read fails, what was written
        with ``reload: true`` (the change stands)."""
        try:
            return JSONResponse(await prompt_answer())
        except Exception as exc:
            logger.warning("F114: %s was made, and reading it back failed (%s)", what, type(exc).__name__)
            return JSONResponse({**written, "reload": True})

    async def read_prompt(request: Request) -> Response:
        """GET /goals/prompt"""
        if not goals:
            return JSONResponse({"enabled": False})
        try:
            return JSONResponse(await prompt_answer())
        except Exception as exc:
            return failed("reading the default goal prompt", exc)

    async def save_prompt(request: Request) -> Response:
        """PUT /goals/prompt"""
        if not goals:
            return JSONResponse(GOALS_OFF, status_code=503)
        try:
            body: PromptBody = await _parse(request, PromptBody)
        except _BadRequest as exc:
            return _error(400, str(exc))
        text = normalize_goal_prompt(body.text)
        if not goal_prompt_has_text(text):
            return _error(400, PROMPT_EMPTY)
        if len(text) > default_prompt.MAX_CHARS:
            return _error(400, PROMPT_TOO_LONG.format(n=len(text), max=default_prompt.MAX_CHARS))
        proof = guard.proof(request)
        assert proof is not None  # the guard let the call in on one
        try:
            await IdentityManager(database, agent_id).set_goal_prompt(text, proof)
        except GoalPromptChanged:
            return _error(409, PROMPT_CHANGED)
        except Exception as exc:
            return failed("saving the default goal prompt", exc)
        return await changed_prompt("a save of the default goal prompt", prompt_shape(text, False, None, proof))

    async def reset_prompt(request: Request) -> Response:
        """DELETE /goals/prompt: back to the built-in text (nothing saved is not an error)."""
        if not goals:
            return JSONResponse(GOALS_OFF, status_code=503)
        try:
            await _parse(request, NoBody)
        except _BadRequest as exc:
            return _error(400, str(exc))
        try:
            await IdentityManager(database, agent_id).clear_goal_prompt()
        except Exception as exc:
            return failed("resetting the default goal prompt", exc)
        written = prompt_shape(default_prompt.DEFAULT_GOAL_PROMPT, True)
        return await changed_prompt("a reset of the default goal prompt", written)

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
        tools: list[tuple[str, McpIdentity | None]] = []
        if body.tools is not None:
            if not grants_on():
                return JSONResponse(GRANTS_OFF, status_code=503)
            checked = new_goal_grants(body.tools)
            if isinstance(checked, JSONResponse):
                return checked
            tools = checked
        extra: dict[str, Any] = {"tools": tools} if tools else {}
        paced: dict[str, Any] = {}
        if body.daily_at is not None:
            if pacing.zone(settings) is None:
                return JSONResponse(PACING_OFF, status_code=503)
            paced["daily_at_minute"] = pacing.parse_at(body.daily_at)
        if body.commands is not None:
            extra["commands"] = goal_commands.CommandsRequest(body.commands.hours, body.commands.confirm)
        if extra:
            extra["proof"] = guard.proof(request)
        try:
            made = await goals.create(
                title=body.title,
                objective=body.objective,
                done_when=body.done_when,
                preset=body.preset,
                overrides=body.limits.model_dump(exclude_none=True) if body.limits else None,
                proposed_by=ACTOR,
                origin_channel=body.origin_channel,
                **paced,
                **extra,
            )
        except GoalRefused as exc:
            return _error(409, str(exc))
        except Exception as exc:
            return failed("creating a goal", exc)
        return await changed_answer(made, "creating a goal", status=201)

    def action(
        name: str, model: type[_Body], call: Callable[[GoalStore, UUID, Any, str | None], Awaitable[Goal]]
    ) -> Route:
        """``POST /goals/{ref}/<name>``: the body as ``model``, the goal looked up (404), then ``call`` with the proof
        the guard accepted (409 on a refusal), answered with the goal's detail (``changed_answer``)."""

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
                changed = await call(goals, goal.id, body, guard.proof(request))
            except GoalRefused as exc:
                return _error(409, str(exc))
            except Exception as exc:
                return failed(name, exc)
            if name in STATE_ACTIONS:  # a pause reaches the goal's open session now, not at the next tick
                await refresh_grants(changed.id)
            return await changed_answer(changed, name)

        run.__name__ = f"goal_{name.replace('-', '_')}"
        path = f"/goals/{{ref}}/{name}"
        return guard.route(path, run, methods=["POST"])

    def budget(store: GoalStore, goal_id: UUID, body: BudgetChange, _proof: str | None) -> Awaitable[Goal]:
        return store.raise_budget(goal_id, total=body.total, day=body.day, actor=ACTOR)

    def sessions(store: GoalStore, goal_id: UUID, body: SessionsChange, _proof: str | None) -> Awaitable[Goal]:
        return store.change_sessions(goal_id, changes=body.changes(), actor=ACTOR)

    # ---- the tools a goal may use without asking (F115) ----------------------------------------------------------

    def grants_on() -> bool:
        return bool(settings.goal_tool_grants_enabled and grants)

    def listed() -> dict[str, str]:
        return registered_tools(dispatcher) if dispatcher else {}

    def grant_row(goal: Goal, row: GoalToolGrant, tools: dict[str, str]) -> dict[str, Any]:
        identity = grant_identity(row)
        why = goal_not_running(goal) or grant_not_usable(
            row.tool, identity, registered=row.tool in tools, settings=settings
        )
        return {
            "tool": row.tool,
            **tool_words(row.tool, identity, format_short_id(goal.short_id)),
            "granted_at": _iso(row.granted_at),
            "usable": why is None,
            "not_usable_why": why,
        }

    def available_row(ref: str, tool: str, *, full: bool) -> dict[str, Any]:
        identity = mcp_tool_identity(tool) if tool.startswith(MCP_TOOL_PREFIX) else None
        refusal = grant_refusal_for(tool, registered=True, settings=settings)
        if refusal is None and full:
            refusal = GrantRefusal(409, "cap", f"A goal can have at most {GRANTS_MAX} tools. Remove one first.")
        return {
            "tool": tool,
            **tool_words(tool, identity, ref),
            "blocked": None if refusal is None else {"code": refusal.code, "sentence": refusal.sentence},
        }

    def grantable(tools: dict[str, str], granted: set[str]) -> list[str]:
        return sorted(name for name in tools if tool_policy.grant_refusal(name) is None and name not in granted)

    def new_goal_tools() -> dict[str, Any]:
        """What ``GET /goals/options`` says a new goal may be allowed: the same rows as a goal's own, for "this
        goal", and the one question asked before it is created with any."""
        return {
            "applies_in": lead_sentence(),
            "never": never_sentence(settings),
            "limit": GRANTS_MAX,
            "confirm": CREATE_CONFIRM,
            "available": [available_row("this goal", name, full=False) for name in grantable(listed(), set())],
        }

    def new_goal_grants(items: list[GrantItem]) -> list[tuple[str, McpIdentity | None]] | JSONResponse:
        """The tools of a new goal, each checked as ``PUT /goals/{ref}/tools/{tool}`` checks it. The first one
        refused is the answer, naming the tool with its sentence, and then nothing is created."""
        if len(items) > GRANTS_MAX:
            return _error(409, f"A goal can have at most {GRANTS_MAX} tools. Remove one first.")
        tools = listed()
        found: list[tuple[str, McpIdentity | None]] = []
        for item in items:
            refusal = new_goal_refusal(item, tools, [tool for tool, _identity in found])
            if isinstance(refusal, GrantRefusal):
                name = clip(item.tool, TOOL_ECHO_CHARS)
                return JSONResponse({"error": refusal.sentence, "tool": name}, status_code=refusal.status)
            found.append((item.tool, refusal))
        return found

    def new_goal_refusal(
        item: GrantItem, tools: dict[str, str], earlier: list[str]
    ) -> GrantRefusal | McpIdentity | None:
        """Why one tool of a new goal cannot be allowed (a ``GrantRefusal``), else its MCP identity (None for a
        built-in tool)."""
        if item.confirm is not True:
            return GrantRefusal(400, "confirm", CONFIRM_SENTENCE)
        if item.tool in earlier:
            return GrantRefusal(400, "twice", "A tool is listed twice.")
        refusal = grant_refusal_for(item.tool, registered=item.tool in tools, settings=settings)
        if refusal is not None:
            return refusal
        identity = mcp_tool_identity(item.tool) if item.tool.startswith(MCP_TOOL_PREFIX) else None
        if (identity is None) != (item.identity is None):
            return GrantRefusal(400, "identity", "Not accepted: tools.identity.")
        if identity is not None and item.identity != identity_token(identity):
            return GrantRefusal(409, "server_changed", SERVER_CHANGED_SENTENCE)
        return identity

    async def tools_payload(goal: Goal) -> dict[str, Any]:
        rows = await goals.tool_grants(goal.id) if goals else []
        tools = listed()
        granted = {row.tool for row in rows}
        full = len(rows) >= GRANTS_MAX
        return {
            "enabled": True,
            "grants_enabled": True,
            "can_edit": goal.state in LIVE_STATES,
            "limit": GRANTS_MAX,
            "applies_in": lead_sentence(),
            "never": never_sentence(settings),
            "grants": [grant_row(goal, row, tools) for row in rows],
            "available": [
                available_row(format_short_id(goal.short_id), name, full=full) for name in grantable(tools, granted)
            ],
        }

    async def tools_answer(goal: Goal, changed: bool) -> JSONResponse:
        """The answer of a grant or a revoke the store made (or found made): the runner's view read again, then the
        goal's tools. ``reload: true`` when either read failed."""
        read = await refresh_grants(goal.id)
        try:
            payload = await tools_payload(goal)
        except Exception as exc:
            logger.warning("F115: a goal's tools were changed, and reading them back failed (%s)", type(exc).__name__)
            payload = {"enabled": True, "grants_enabled": True, "reload": True}
        if not read:
            payload["reload"] = True
        return JSONResponse({**payload, "changed": changed})

    async def parse_grant(request: Request, model: type[_Body]) -> Any:
        """The body as ``model``; a 400 names the fields only: ``Not accepted: <fields>.``"""
        raw = await request.body()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except (ValueError, RecursionError):
            raise _BadRequest("Not accepted: body.") from None
        if not isinstance(data, dict):
            raise _BadRequest("Not accepted: body.")
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            fields = sorted({clip(str(error["loc"][0]), ECHO_CHARS) for error in exc.errors() if error["loc"]})
            raise _BadRequest(f"Not accepted: {', '.join(fields) or 'body'}.") from None

    async def tools_of(request: Request) -> Response:
        """GET /goals/{ref}/tools"""
        if not goals:
            return JSONResponse({"enabled": False})
        if not grants_on():
            return JSONResponse({"enabled": True, "grants_enabled": False})
        ref = request.path_params["ref"]
        try:
            goal = await goals.get(ref)
            if goal is None:
                return _no_goal(ref)
            return JSONResponse(await tools_payload(goal))
        except Exception as exc:
            return failed("reading a goal's tools", exc)

    async def activity_of(request: Request) -> Response:
        """GET /goals/{ref}/activity"""
        if not goals:
            return JSONResponse({"enabled": False})
        if not activity_on():
            return JSONResponse({"enabled": True, "activity_enabled": False})
        ref = request.path_params["ref"]
        try:
            goal = await goals.get(ref)
            if goal is None:
                return _no_goal(ref)
            log_on = getattr(settings, "tool_call_log_enabled", False) is True
            async with database.session() as session:
                found = await goal_activity.read_activity(session, agent_id, goal.id, log_enabled=log_on)
        except Exception as exc:
            return failed("reading a goal's activity", exc)
        return JSONResponse({"enabled": True, "activity_enabled": True, **found})

    async def allow_tool(request: Request) -> Response:
        """PUT /goals/{ref}/tools/{tool}"""
        if not goals:
            return JSONResponse(GOALS_OFF, status_code=503)
        if not grants_on():
            return JSONResponse(GRANTS_OFF, status_code=503)
        try:
            body: GrantBody = await parse_grant(request, GrantBody)
        except _BadRequest as exc:
            return _error(400, str(exc))
        if body.confirm is not True:
            return _error(400, CONFIRM_SENTENCE)
        ref, tool = request.path_params["ref"], request.path_params["tool"]
        try:
            goal = await goals.get(ref)
            if goal is None:
                return _no_goal(ref)
            if goal.state not in LIVE_STATES:
                return _error(409, f"{format_short_id(goal.short_id)} is finished; its tools can no longer be changed.")
            refusal = grant_refusal_for(tool, registered=tool in listed(), settings=settings)
            if refusal is not None:
                return _error(refusal.status, refusal.sentence)
            identity = mcp_tool_identity(tool) if tool.startswith(MCP_TOOL_PREFIX) else None
            if (identity is None) != (body.identity is None):
                return _error(400, "Not accepted: identity.")
            if identity is not None and body.identity != identity_token(identity):
                return _error(409, SERVER_CHANGED_SENTENCE)
            proof = guard.proof(request)
            assert proof is not None  # the guard let the call in on one
            changed = await goals.grant_tool(goal.id, tool, identity, actor=ACTOR, proof=proof)
        except GoalRefused as exc:
            return _error(409, str(exc))
        except Exception as exc:
            return failed("allowing a goal a tool", exc)
        return await tools_answer(goal, changed)

    async def remove_tool(request: Request) -> Response:
        """DELETE /goals/{ref}/tools/{tool}"""
        if not goals:
            return JSONResponse(GOALS_OFF, status_code=503)
        if not grants_on():
            return JSONResponse(GRANTS_OFF, status_code=503)
        try:
            await parse_grant(request, NoBody)
        except _BadRequest as exc:
            return _error(400, str(exc))
        ref, tool = request.path_params["ref"], request.path_params["tool"]
        try:
            goal = await goals.get(ref)
            if goal is None:
                return _no_goal(ref)
            proof = guard.proof(request)
            assert proof is not None  # the guard let the call in on one
            changed = await goals.revoke_tool(goal.id, tool, actor=ACTOR, proof=proof)
        except GoalRefused as exc:
            return _error(409, str(exc))
        except Exception as exc:
            return failed("removing a goal's tool", exc)
        return await tools_answer(goal, changed)

    return [
        guard.route("/goals", list_goals, methods=["GET"]),
        guard.route("/goals", create, methods=["POST"]),
        guard.route("/goals/options", options, methods=["GET"]),
        guard.route("/goals/commands", commands_now, methods=["GET"]),
        guard.route("/goals/prompt", read_prompt, methods=["GET"]),
        guard.route("/goals/prompt", save_prompt, methods=["PUT"]),
        guard.route("/goals/prompt", reset_prompt, methods=["DELETE"]),
        guard.route("/goals/{ref}", get_goal, methods=["GET"]),
        guard.route("/goals/{ref}/tools", tools_of, methods=["GET"]),
        guard.route("/goals/{ref}/activity", activity_of, methods=["GET"]),
        guard.route("/goals/{ref}/tools/{tool}", allow_tool, methods=["PUT"]),
        guard.route("/goals/{ref}/tools/{tool}", remove_tool, methods=["DELETE"]),
        action("approve", NoBody, lambda store, gid, _b, proof: store.approve(gid, actor=ACTOR, proof=proof)),
        action("pause", NoBody, lambda store, gid, _b, _p: store.pause(gid, actor=ACTOR)),
        action("resume", NoBody, lambda store, gid, _b, _p: store.resume(gid, actor=ACTOR)),
        action("confirm-done", NoBody, lambda store, gid, _b, _p: store.confirm_done(gid, actor=ACTOR)),
        action("stop", NoBody, lambda store, gid, _b, _p: store.stop(gid, actor=ACTOR)),
        action("cancel", NoBody, lambda store, gid, _b, _p: store.cancel(gid, actor=ACTOR)),
        action("run-now", NoBody, lambda store, gid, _b, _p: store.request_run_now(gid, actor=ACTOR)),
        action("budget", BudgetChange, budget),
        action("sessions", SessionsChange, sessions),
        action(
            "deadline", DeadlineChange, lambda store, gid, b, _p: store.extend_deadline(gid, days=b.days, actor=ACTOR)
        ),
        action("note", NoteBody, lambda store, gid, b, _p: store.add_note(gid, b.text, actor=ACTOR)),
        action(
            "contained",
            NoBody,
            lambda store, gid, _b, proof: store.return_to_contained(gid, actor=ACTOR, proof=proof),
        ),
    ]
