"""F115: the tool guard: the tools that background work may not use.

A ``ToolGuard`` is an immutable snapshot of three sources of a hold: the tools the owner switched off (``denied``),
the tools the host locked (``NOUS_AUTONOMY_LOCKED_TOOLS``, ``locked``), and, while the owner's saved settings could not
be read, every tool (``hold_all``). One snapshot is installed for the whole process (``install``, ``current``), so the
runner and every fork of it read the same one; the owner's route installs a new snapshot before it answers, so the next
call sees it. With the guard off nothing is installed and the runner changes nothing.

Which kinds of turn each source holds is one table, ``KINDS`` (every execution-context kind, its words on the screen and
its sources), read by ``held_sources`` and by the screen's sentences alike:

- the owner's setting (``OWNER``) holds every kind of background work, never chat, an inbound MCP call, a tap on a
  companion card or an action the owner approved (the last two are the owner's own acts);
- a host lock (``LOCK``) holds the same, and also a companion tap and an approved action: what the server locks stays
  off whatever is set or tapped;
- the hold while the settings could not be read (``NOT_READ``) stands in for the owner's settings and the host's: it
  holds the kinds of the owner's setting and a companion tap.

A chat or inbound MCP turn in a session a background path made is held by all three. The runner asks
``reason(kind, session_id, tool)`` in two places: ``_offered_tools`` leaves a held tool out of the turn's tools, and
``_authorize_tool_call`` refuses a call to one, in every enforcement mode. The harness's own per-turn tools (a chain's
decision tools, a subtask's final report) are never held: a snapshot drops them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType

from nous.api.execution_context import CONTEXT_KINDS, FOREGROUND_KINDS, is_background_session
from nous.api.tool_policy import INTERNAL_ONLY_EXTRA_TOOLS

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_SOURCES",
    "HARNESS_TOOLS",
    "KINDS",
    "LOCK",
    "NOT_READ",
    "OWNER",
    "REFUSAL_EVENT",
    "ToolGuard",
    "also_held",
    "current",
    "held_sources",
    "install",
    "list_words",
    "parse_locked_tools",
    "places",
]

# Tools the harness adds to one turn (never registered with the dispatcher): a chain's decision tools and a hardened
# subtask's final report. A turn that may not call them cannot end, so no setting holds them.
HARNESS_TOOLS: frozenset[str] = INTERNAL_ONLY_EXTRA_TOOLS | frozenset({"submit_final_report"})

# The three sources of a hold, as the refusal's event names them.
OWNER = "owner"
LOCK = "server"
NOT_READ = "not_read"
ALL_SOURCES: frozenset[str] = frozenset({OWNER, LOCK, NOT_READ})
_OWNER_ACT: frozenset[str] = frozenset({LOCK, NOT_READ})

# Every execution-context kind: its words on the screen, and the sources that hold it, in the order the screen lists
# them. A test holds the keys equal to CONTEXT_KINDS.
KINDS: MappingProxyType[str, tuple[str, frozenset[str]]] = MappingProxyType(
    {
        "subtask": ("background tasks", ALL_SOURCES),
        "dag_node": ("workflow steps", ALL_SOURCES),
        "scheduled": ("scheduled tasks", ALL_SOURCES),
        "continuation": ("chain decisions", ALL_SOURCES),
        "heartbeat_check": ("heartbeat checks", ALL_SOURCES),
        "heartbeat_callback": ("heartbeat check callbacks", ALL_SOURCES),
        "heartbeat_triage": ("heartbeat triage", ALL_SOURCES),
        "dag_summary": ("workflow summaries", ALL_SOURCES),
        "background": ("other background turns", ALL_SOURCES),
        "interactive": ("chat in an ordinary session", frozenset()),
        "mcp": ("an MCP call from another agent in an ordinary session", frozenset()),
        "agent_action": ("a tap on a companion card", _OWNER_ACT),
        "approved_action": ("an action you approved", frozenset({LOCK})),
    }
)
# A conversation kind in a session a background path made, and the sources that hold it.
IN_A_BACKGROUND_SESSION = ("a chat or MCP turn in a session background work started", ALL_SOURCES)

# The harness event a guard refusal writes (its own name, so it is not counted as a context-policy violation).
REFUSAL_EVENT = "harness_tool_guard_refusal"

_REFUSAL = {
    OWNER: "Tool error: '{tool}' is switched off for background work.",
    LOCK: "Tool error: '{tool}' is locked by the server.",
    NOT_READ: "Tool error: the saved tool settings could not be read, so '{tool}' is held until they can.",
}


def held_sources(kind: str, session_id: str | None) -> frozenset[str]:
    """The sources that hold a turn of this kind in this session (``KINDS``); a conversation kind in a session a
    background path made is held by all three, and a kind the table does not name by all three."""
    if kind in FOREGROUND_KINDS and is_background_session(session_id):
        return IN_A_BACKGROUND_SESSION[1]
    entry = KINDS.get(kind)
    return entry[1] if entry is not None else ALL_SOURCES


def places(source: str, *, held: bool = True) -> list[str]:
    """The kinds ``source`` holds (``held``) or does not hold, in words, in the table's order; a conversation kind in a
    background session is listed last among the held."""
    words = [text for kind, (text, sources) in KINDS.items() if kind in CONTEXT_KINDS and (source in sources) is held]
    if held and source in IN_A_BACKGROUND_SESSION[1]:
        words.append(IN_A_BACKGROUND_SESSION[0])
    return words


def also_held(source: str) -> list[str]:
    """The kinds ``source`` holds beyond the owner's setting, in words."""
    beyond = places(OWNER)
    return [words for words in places(source) if words not in beyond]


def list_words(items: list[str]) -> str:
    """``a``, ``a and b``, ``a, b, and c``."""
    if len(items) <= 2:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


@dataclass(frozen=True)
class ToolGuard:
    """The three sources of a hold: the owner's (``denied``), the host's (``locked``) and, while the owner's saved
    settings could not be read, every tool but the harness's own (``hold_all``)."""

    denied: frozenset[str] = field(default_factory=frozenset)
    locked: frozenset[str] = field(default_factory=frozenset)
    hold_all: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "denied", frozenset(self.denied) - HARNESS_TOOLS)
        object.__setattr__(self, "locked", frozenset(self.locked) - HARNESS_TOOLS)

    def reason(self, kind: str, session_id: str | None, tool: str) -> str | None:
        """Which source holds ``tool`` from a turn of ``kind`` in ``session_id``: ``server``, ``not_read`` or
        ``owner``, in that order; None when none does."""
        if tool in HARNESS_TOOLS:
            return None
        sources = held_sources(kind, session_id)
        if LOCK in sources and tool in self.locked:
            return LOCK
        if NOT_READ in sources and self.hold_all:
            return NOT_READ
        if OWNER in sources and tool in self.denied:
            return OWNER
        return None

    def denies(self, kind: str, session_id: str | None, tool: str) -> bool:
        """Whether a turn of ``kind`` in ``session_id`` may not use ``tool``."""
        return self.reason(kind, session_id, tool) is not None

    @staticmethod
    def refusal(tool: str, reason: str) -> str:
        """The tool error a call refused for ``reason`` returns to the model."""
        return _REFUSAL[reason].format(tool=tool)


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
