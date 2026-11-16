"""F115: the tool guard: the tools that background work may not use.

A ``ToolGuard`` is an immutable snapshot: the tools the owner switched off (``denied``) and the tools the host locked
(``NOUS_AUTONOMY_LOCKED_TOOLS``). One snapshot is installed for the whole process (``install``, ``current``), so the
runner and every fork of it read the same one; the owner's route installs a new snapshot before it answers, so the
next call sees it. With the guard off nothing is installed and the runner changes nothing.

The runner asks ``denies(kind, session_id, tool)`` in two places: ``_offered_tools`` leaves a denied tool out of a
background turn's tools, and ``_authorize_tool_call`` refuses a call to one, in every enforcement mode. A turn is held
by the guard unless it is a conversation (its kind is ``interactive`` or ``mcp`` and its session is not one a
background path made) or one call the owner approved (``approved_action``). The harness's own per-turn tools (a
chain's decision tools, a subtask's final report) are never held: a snapshot drops them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from nous.api.execution_context import FOREGROUND_KINDS, is_background_session
from nous.api.tool_policy import INTERNAL_ONLY_EXTRA_TOOLS

logger = logging.getLogger(__name__)

__all__ = [
    "HARNESS_TOOLS",
    "ToolGuard",
    "current",
    "guarded_turn",
    "install",
    "parse_locked_tools",
    "refusal_text",
]

# Tools the harness adds to one turn (never registered with the dispatcher): a chain's decision tools and a hardened
# subtask's final report. A turn that may not call them cannot end, so no setting holds them.
HARNESS_TOOLS: frozenset[str] = INTERNAL_ONLY_EXTRA_TOOLS | frozenset({"submit_final_report"})

_REFUSAL = "Tool error: '{tool}' is switched off for background work."


def guarded_turn(kind: str, session_id: str | None) -> bool:
    """Whether the guard holds a turn of this kind in this session: never one call the owner approved
    (``approved_action``); always a background kind; a conversation kind only in a session a background path made."""
    if kind == "approved_action":
        return False
    return kind not in FOREGROUND_KINDS or is_background_session(session_id)


@dataclass(frozen=True)
class ToolGuard:
    """The tools background work may not use: the owner's (``denied``) and the host's (``locked``)."""

    denied: frozenset[str] = field(default_factory=frozenset)
    locked: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "denied", frozenset(self.denied) - HARNESS_TOOLS)
        object.__setattr__(self, "locked", frozenset(self.locked) - HARNESS_TOOLS)

    def switched_off(self, tool: str) -> bool:
        """Whether ``tool`` is switched off for background work, by the owner or the host."""
        return tool in self.denied or tool in self.locked

    def denies(self, kind: str, session_id: str | None, tool: str) -> bool:
        """Whether a turn of ``kind`` in ``session_id`` may not use ``tool``."""
        return self.switched_off(tool) and guarded_turn(kind, session_id)


_current: ToolGuard | None = None


def install(guard: ToolGuard | None) -> None:
    """Make ``guard`` the snapshot every runner reads (None: no guard)."""
    global _current
    _current = guard


def current() -> ToolGuard | None:
    """The installed snapshot, or None when the guard is off."""
    return _current


def refusal_text(tool: str) -> str:
    """The tool error a refused call returns to the model."""
    return _REFUSAL.format(tool=tool)


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
