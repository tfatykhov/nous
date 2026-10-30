"""Renamed and retired agent tool names, declared once.

A leaf module: no imports from the rest of ``nous``, so the heartbeat checks,
the skill parser and the procedure learner can read it as well as the tool
dispatcher. ``ToolDispatcher`` builds its alias registrations from
``CHANGED_TOOLS`` (``register_nous_tools``), and every stored tool list is
read through ``canonical_tool_names``, so a name changes in exactly one place.

An alias is never offered to the model and never reaches the ledger: the
runner rewrites a call to it into a call to its target before any check runs.
A retired name returns its message and runs nothing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolAlias:
    """What an old tool name became. ``target`` is None for a retired name."""

    target: str | None
    # An alias: the first line of the call's result. A retired name: the whole result.
    note: str
    # The old call's arguments as the target takes them (aliases only).
    translate: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # How the recall-time note names the change.
    hint: str = ""


def _recall_recent_args(args: dict[str, Any]) -> dict[str, Any]:
    # recall_recent with no arguments listed the last 48 hours; recall_deep needs hours to list at all.
    out = dict(args)
    if out.get("hours") is None:
        out["hours"] = 48
    return out


_RESOLUTION_FIELDS = ("decision_id", "outcome", "resolution_note", "superseded_by")


def _resolve_decision_args(args: dict[str, Any]) -> dict[str, Any]:
    return {"resolutions": [{key: args[key] for key in _RESOLUTION_FIELDS if key in args}]}


CHANGED_TOOLS: Mapping[str, ToolAlias] = MappingProxyType(
    {
        "recall_recent": ToolAlias(
            "recall_deep",
            "recall_recent is now recall_deep(hours=N): pass hours to list recent episodes.",
            _recall_recent_args,
            # A memory may mean the run_python helper of the same name, which did not change.
            "recall_recent (tool) → recall_deep(hours=…), while run_python's recall_recent() helper is unchanged",
        ),
        "resolve_decision": ToolAlias(
            "resolve_decisions",
            "resolve_decision is now resolve_decisions with one item.",
            _resolve_decision_args,
            "resolve_decision → resolve_decisions",
        ),
        "recall_hubs": ToolAlias(
            None,
            "recall_hubs was retired (unused). To explore a topic's connected memories, call recall_deep with "
            "the topic.",
            hint="recall_hubs was retired (use recall_deep)",
        ),
    }
)

_CHANGED_NAME = re.compile(r"\b(" + "|".join(map(re.escape, CHANGED_TOOLS)) + r")\b")


def canonical_tool_name(name: str) -> str | None:
    """The name a tool has now: an alias becomes its target, a retired name None (logged). Anything that is not
    a string is returned as it is, for the caller's own validation."""
    change = CHANGED_TOOLS.get(name) if isinstance(name, str) else None
    if change is None:
        return name
    if change.target is None:
        logger.warning("Dropped the retired tool name %r from a stored tool list", name)
    return change.target


def canonical_tool_names(names: Iterable[str]) -> list[str]:
    """``names`` with each one canonical, retired names dropped and repeats removed, in order."""
    out: list[str] = []
    for name in names:
        canonical = canonical_tool_name(name)
        if canonical is not None and canonical not in out:
            out.append(canonical)
    return out


def changed_tools_note(text: str) -> str | None:
    """One line naming the changed tools that ``text`` mentions (whole words only), or None."""
    found = set(_CHANGED_NAME.findall(text or ""))
    if not found:
        return None
    hints = [change.hint for name, change in CHANGED_TOOLS.items() if name in found]
    return "Note: some memories name tools that changed: " + "; ".join(hints) + "."
