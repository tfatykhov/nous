"""A goal whose tasks may run any command until an end time (``brain.goals.mode = 'commands'``, migration 118).

The owner chooses it only when creating the goal, with a number of hours and a typed sentence (``confirm_sentence``);
the end time is set when the goal is approved (``GoalStore.approve``), at most ``NOUS_GOAL_COMMANDS_MAX_HOURS`` hours
and at most the goal's deadline. The only later change is ``GoalStore.return_to_contained``. A driver pause, Stop now,
a cancel and the owner's confirmation that the goal is done end it; the owner's own pause does not.

While it holds, a bash call in a turn of ``tool_policy.COMMANDS_KINDS`` of the goal's open session's lineage is not
held to the lineage allowlist (``tool_policy`` takes ``commands``). ``decide_commands`` says which turn; the first
failing step wins:

0. the tool guard is installed (``tool_guard.current()``);
1. the turn is internal_only, of a kind in ``tool_policy.COMMANDS_KINDS``, and names a root (``ctx.root_intention_id``,
   from the lineage stamp; never the turn's own intention);
2. the view is fresh by its monotonic clock and by the wall clock (``GoalToolGrants.commands_fresh``: its last
   full read within the per-goal tools view's age by both, so after a host suspend nothing is lifted until the next
   full read);
3. the view holds an entry for the turn's root (an open session of an active goal in the mode, as last read) and the
   view's monotonic clock is before the entry's end.

``decide_commands`` never raises: an error is None, logged by its class. The words the screen shows are the tables
here (``WORDS``, the refusals, ``ENDED``), which the routes return as they are.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from nous.api import tool_policy
from nous.api.execution_context import ExecutionContext
from nous.autonomy import tool_guard
from nous.brain.intentions import AUTHORITY_INTERNAL

logger = logging.getLogger(__name__)

MODE_CONTAINED = "contained"
MODE_COMMANDS = "commands"
# At most this many goals may run any command at a time: an approval that would make one more is refused.
COMMANDS_MAX_GOALS = 1
# The longest a goal may run any command, whatever the host allows: the 90 days of a goal's longest deadline.
HOURS_LIMIT = 2160
# Evidence refs of the journal rows of a change of mode (``mode:<state>``, ``why:<reason>``, ``via:<proof>``).
MODE_REF = "mode:"
WHY_REF = "why:"
VIA_REF = "via:"

# ---- the words --------------------------------------------------------------------------------------------------

DESCRIPTION = (
    "This goal's tasks can run any command on the server Nous runs on, as the same user Nous runs as, and can start "
    "coding jobs. Nous does not pass its database login, model keys or chat tokens to those commands, but a command "
    "can read them from the running server, and can read and change Nous's files, settings and data. The code-host "
    "token is passed to them. Nous checks the end time before each command. A command already running at that "
    "time, anything it left running, and anything it changed stay as they are."
)
WORDS: Mapping[str, str] = MappingProxyType(
    {
        "checkbox": "Let this goal's tasks run any command until a time I set.",
        "hours": "Hours from approval",
        "description": DESCRIPTION,
        "heading": "May run any command",
        "off": "Off",
        "set": "Set: {hours} from approval.",
        "on": "On until {until} UTC.",
        "ended": "Ended: {reason}",
        "return": "Return to contained",
        "return_note": (
            "Takes effect at the next tool call. A command already running, anything it left running, and anything "
            "it changed stay as they are."
        ),
        # After return_note, only where Stop now is offered: a draft, or a paused goal with no open session, has none.
        "return_stop": "Use Stop now for the session.",
        "stops": "A lock or Not allowed on bash under Limits and tools stops its commands.",
        "approve": "Approving starts its time: this goal's tasks may then run any command for {hours}.",
        "banner": "{ref} may run any command until {until} UTC.",
    }
)
NOT_ALLOWED = "Running any command is not allowed on this server."
CONFIRM = "Type this sentence to allow it."
HOURS_RANGE = "Hours must be a whole number from 1 to {n}."
ONE_AT_A_TIME = "Another goal may run any command already. Only one can at a time."
PROPOSER = "Only you can allow a goal to run any command."
# Why a goal's tasks do not run any command now, though its mode is on (``not_usable``).
NOT_USABLE: Mapping[str, str] = MappingProxyType(
    {
        "no_guard": "The tool settings are not loaded on this server, so no goal runs any command.",
        tool_guard.LOCK: "bash is locked by the server.",
        tool_guard.NOT_READ: "Held until the saved tool settings can be read.",
        tool_guard.OWNER: "bash is set to Not allowed for background work under Limits and tools.",
        "draft": "The goal is a draft: its time starts when you approve it.",
        "paused": "The goal is paused.",
        "finished": "The goal is finished.",
        "ended": "Its end time has passed.",
        "flag_off": NOT_ALLOWED,
    }
)
# The journal row of each end of the mode, by its ``why:`` ref.
ENDED: Mapping[str, str] = MappingProxyType(
    {
        "returned": "Returned to contained.",
        "expired": "Its end time passed.",
        "finished": "The goal finished.",
        "paused": "The goal was paused, so its tasks no longer run any command.",
        "stopped": "You stopped it, so its tasks no longer run any command.",
        "cancelled": "The goal was cancelled.",
        "done": "The goal is done.",
        "cleared": "It had no end time, so its tasks no longer run any command.",
        "flag_off": "Running any command is not allowed on this server now.",
    }
)


# The sentence the owner types, and the hours in it (one, or more): the screen builds it from these.
CONFIRM_TEMPLATE = "allow this goal to run any command for {hours}"
HOUR_ONE = "{n} hour"
HOURS_MANY = "{n} hours"


def hours_words(hours: int) -> str:
    """``1 hour``, ``5 hours``."""
    return (HOUR_ONE if hours == 1 else HOURS_MANY).format(n=hours)


def confirm_sentence(hours: int) -> str:
    """The sentence the owner types to create a goal that may run any command for ``hours`` hours."""
    return CONFIRM_TEMPLATE.format(hours=hours_words(hours))


def armed_text(hours: int) -> str:
    """The creation's journal row."""
    return f"This goal may run any command for {hours_words(hours)}, starting when it is approved."


def on_text(until: Any) -> str:
    """The approval's journal row."""
    return f"May run any command until {until:%Y-%m-%d %H:%M} UTC."


def clamped_text(until: Any) -> str:
    """The row of an end time moved to the host's longest since it was set."""
    return f"Its end time moved to {until:%Y-%m-%d %H:%M} UTC, the longest this server now allows."


def mode_refs(state: str, *, why: str | None = None, via: str | None = None) -> list[str]:
    """The evidence refs of a journal row of a change of mode: ``mode:<state>``, then ``why:`` and ``via:``."""
    return [f"{MODE_REF}{state}", *([f"{WHY_REF}{why}"] if why else []), *([f"{VIA_REF}{via}"] if via else [])]


# The values that return a goal to contained, in the UPDATE of the change that ends its mode.
CONTAINED_VALUES: Mapping[str, Any] = MappingProxyType({"mode": MODE_CONTAINED, "mode_hours": None, "mode_until": None})


def max_hours(settings: Any) -> int:
    """The host's ceiling (``NOUS_GOAL_COMMANDS_MAX_HOURS``), within ``HOURS_LIMIT``."""
    return max(1, min(int(getattr(settings, "goal_commands_max_hours", 24)), HOURS_LIMIT))


def enabled(settings: Any) -> bool:
    """Whether a goal may be allowed to run any command on this server (``NOUS_GOAL_COMMANDS_ENABLED``, which its
    validator forces off unless everything it needs is on)."""
    return getattr(settings, "goal_commands_enabled", False) is True


@dataclass(frozen=True)
class CommandsRequest:
    """What the owner chose on the create form: the hours from approval, and the sentence typed."""

    hours: int
    confirm: str


# ---- the decision -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandsEntry:
    """An open session's root whose goal may run any command, as the view holds it: the goal, the session's number
    and the end, on the view's monotonic clock."""

    goal_id: UUID
    session_n: int
    expires_at: float


class CommandsView(Protocol):
    def commands_fresh(self) -> bool: ...

    def commands_entry(self, root_id: UUID) -> CommandsEntry | None: ...

    def now(self) -> float: ...


def decide_commands(view: CommandsView, ctx: ExecutionContext) -> CommandsEntry | None:
    """The entry that lets a turn of ``ctx`` run any command with bash, or None (see the module's steps). Never
    raises."""
    try:
        if tool_guard.current() is None:
            return None
        if ctx.authority != AUTHORITY_INTERNAL or ctx.kind not in tool_policy.COMMANDS_KINDS:
            return None
        root = ctx.root_intention_id
        if root is None or not view.commands_fresh():
            return None
        entry = view.commands_entry(root)
        if entry is None or not view.now() < entry.expires_at:
            return None
        return entry
    except Exception as exc:
        logger.warning("A goal's commands could not be decided; none run (%s)", type(exc).__name__)
        return None


def charter_until(goal: Any, *, now: Any, settings: Any) -> Any:
    """The end a goal's session task and decision turns are told of (``charter.commands_line``): while the flag is
    on and the goal is active in the mode with an end after ``now``; else None."""
    if not enabled(settings) or goal.mode != MODE_COMMANDS or goal.state != "active":
        return None
    if goal.mode_until is None or goal.mode_until <= now:
        return None
    return goal.mode_until


def not_usable(goal: Any, *, now: Any, settings: Any) -> str | None:
    """Why a goal in the mode does not run any command now, in words (``NOT_USABLE``), or None when its tasks do:
    the flag, the goal's state, its end, then the tool guard's hold on bash for each kind of turn the mode reaches
    (the first that holds)."""
    if not enabled(settings):
        return NOT_USABLE["flag_off"]
    if goal.state == "draft":
        return NOT_USABLE["draft"]
    if goal.state == "paused":
        return NOT_USABLE["paused"]
    if goal.state != "active":
        return NOT_USABLE["finished"]
    if goal.mode_until is None or goal.mode_until <= now:
        return NOT_USABLE["ended"]
    guard = tool_guard.current()
    if guard is None:
        return NOT_USABLE["no_guard"]
    for kind in sorted(tool_policy.COMMANDS_KINDS, reverse=True):  # subtask, then dag_node
        held = guard.reason(kind, None, tool_policy.COMMANDS_TOOL)
        if held is not None:
            return NOT_USABLE[held]
    return None
