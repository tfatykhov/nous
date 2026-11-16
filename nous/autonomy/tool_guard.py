"""F115: the tool guard: the tools that background work may not use.

A ``ToolGuard`` is an immutable snapshot: the tools the owner switched off (``denied``) and the tools the host locked
(``NOUS_AUTONOMY_LOCKED_TOOLS``), or, while the owner's saved settings could not be read, every tool (``hold_all``).
One snapshot is installed for the whole process (``install``, ``current``), so the runner and every fork of it read the
same one; the owner's route installs a new snapshot before it answers, so the next call sees it. With the guard off
nothing is installed and the runner changes nothing.

The runner asks ``denies(kind, session_id, tool)`` in two places: ``_offered_tools`` leaves a denied tool out of a
background turn's tools, and ``_authorize_tool_call`` refuses a call to one, in every enforcement mode. Which turns the
guard holds is ``guarded_turn``: every kind of background work in ``HELD_KINDS``, and a conversation kind
(``interactive``, ``mcp``) in a session a background path made; never a conversation, one call the owner approved
(``approved_action``) or a companion tap (``agent_action``), which are the owner's own acts. The harness's own per-turn
tools (a chain's decision tools, a subtask's final report) are never held: a snapshot drops them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType

from nous.api.execution_context import CONTEXT_KINDS, FOREGROUND_KINDS, is_background_session
from nous.api.tool_policy import INTERNAL_ONLY_EXTRA_TOOLS

logger = logging.getLogger(__name__)

__all__ = [
    "HARNESS_TOOLS",
    "HELD_KINDS",
    "OWNER_KINDS",
    "REFUSAL_EVENT",
    "ToolGuard",
    "current",
    "guarded_turn",
    "held_places",
    "install",
    "parse_locked_tools",
]

# Tools the harness adds to one turn (never registered with the dispatcher): a chain's decision tools and a hardened
# subtask's final report. A turn that may not call them cannot end, so no setting holds them.
HARNESS_TOOLS: frozenset[str] = INTERNAL_ONLY_EXTRA_TOOLS | frozenset({"submit_final_report"})

# The kinds that are the owner's own act although no conversation is open: one approved call, a companion tap.
OWNER_KINDS: frozenset[str] = frozenset({"approved_action", "agent_action"})

# Every kind the guard holds whatever its session, in words, in the order the screen lists them. A test holds the keys
# equal to the kinds ``guarded_turn`` holds.
HELD_KINDS: MappingProxyType[str, str] = MappingProxyType(
    {
        "subtask": "background tasks",
        "dag_node": "workflow steps",
        "scheduled": "scheduled tasks",
        "continuation": "chain decisions",
        "heartbeat_check": "heartbeat checks",
        "heartbeat_callback": "heartbeat check callbacks",
        "heartbeat_triage": "heartbeat triage",
        "dag_summary": "workflow summaries",
        "background": "other background turns",
    }
)
_IN_A_BACKGROUND_SESSION = "a chat or MCP turn in a session background work started"

# The harness event a guard refusal writes (its own name, so it is not counted as a context-policy violation).
REFUSAL_EVENT = "harness_tool_guard_refusal"

_REFUSAL = "Tool error: '{tool}' is switched off for background work."
_REFUSAL_NOT_READ = (
    "Tool error: the saved tool settings could not be read, so '{tool}' is held from background work until they can."
)


def guarded_turn(kind: str, session_id: str | None) -> bool:
    """Whether the guard holds a turn of this kind in this session: never the owner's own act (``OWNER_KINDS``); always
    a background kind; a conversation kind only in a session a background path made."""
    if kind in OWNER_KINDS:
        return False
    return kind not in FOREGROUND_KINDS or is_background_session(session_id)


def held_places() -> list[str]:
    """Where "Not allowed" applies, in words: each kind in ``HELD_KINDS`` that ``guarded_turn`` holds, then a
    conversation kind in a background session."""
    places = [words for kind, words in HELD_KINDS.items() if kind in CONTEXT_KINDS and guarded_turn(kind, None)]
    return [*places, _IN_A_BACKGROUND_SESSION]


@dataclass(frozen=True)
class ToolGuard:
    """The tools background work may not use: the owner's (``denied``) and the host's (``locked``); with ``hold_all``
    (the owner's saved settings could not be read) every tool but the harness's own."""

    denied: frozenset[str] = field(default_factory=frozenset)
    locked: frozenset[str] = field(default_factory=frozenset)
    hold_all: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "denied", frozenset(self.denied) - HARNESS_TOOLS)
        object.__setattr__(self, "locked", frozenset(self.locked) - HARNESS_TOOLS)

    def switched_off(self, tool: str) -> bool:
        """Whether ``tool`` is switched off for background work, by the owner or the host, or held because the
        owner's settings could not be read."""
        if self.hold_all:
            return tool not in HARNESS_TOOLS
        return tool in self.denied or tool in self.locked

    def denies(self, kind: str, session_id: str | None, tool: str) -> bool:
        """Whether a turn of ``kind`` in ``session_id`` may not use ``tool``."""
        return self.switched_off(tool) and guarded_turn(kind, session_id)

    def refusal(self, tool: str) -> str:
        """The tool error a refused call returns to the model."""
        return (_REFUSAL_NOT_READ if self.hold_all else _REFUSAL).format(tool=tool)

    def held_by(self, tool: str) -> str:
        """Why ``tool`` is held, for the refusal's event: ``not_read``, ``server`` or ``owner``."""
        if self.hold_all:
            return "not_read"
        return "server" if tool in self.locked else "owner"


_current: ToolGuard | None = None


def install(guard: ToolGuard | None) -> None:
    """Make ``guard`` the snapshot every runner reads (None: no guard)."""
    global _current
    _current = guard


def current() -> ToolGuard | None:
    """The installed snapshot, or None when the guard is off."""
    return _current


def parse_locked_tools(raw: str) -> frozenset[str]:
    """``NOUS_AUTONOMY_LOCKED_TOOLS`` as a set of tool names (comma list, blanks ignored). The harness's own tools are
    dropped with one warning."""
    names = {entry.strip() for entry in (raw or "").split(",")} - {""}
    harness = names & HARNESS_TOOLS
    if harness:
        logger.warning(
            "NOUS_AUTONOMY_LOCKED_TOOLS: %d harness tool name(s) dropped (a turn needs them to end)", len(harness)
        )
    return frozenset(names - harness)
