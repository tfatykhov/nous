"""F115: the limits of background work the owner may change on the dashboard.

Each entry names a ``Settings`` field that every reader looks up when it uses the value, so a change set in place on
the shared ``Settings`` is what the next use resolves, with no restart. The range is the hard bound of an owner's
value; the host may set a lower ceiling per key in ``NOUS_AUTONOMY_CEILINGS`` (``key=value`` entries separated by
commas). A value the host set in the environment is never range-checked here: with no stored value the setting stays
what the host started with.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "LIMITS",
    "Limit",
    "by_key",
    "ceiling_problem",
    "cross_problem",
    "parse_ceilings",
    "range_problem",
    "whole_number",
]


@dataclass(frozen=True)
class Limit:
    """One limit: the ``Settings`` field it sets (``key``), its words on the screen, its hard range and when a change
    takes effect (``applies``, one sentence)."""

    key: str
    label: str
    unit: str
    low: int
    high: int
    applies: str


_CHAIN_NOW = "At once. A chain that is already running and is above the new value stops at its next step."

LIMITS: tuple[Limit, ...] = (
    Limit(
        "max_turns",
        "Tool-loop iterations per turn",
        "iterations",
        5,
        1000,
        "From the next turn. This applies to every turn, chat included.",
    ),
    Limit(
        "subtask_tool_call_limit",
        "Tool calls per background turn",
        "tool calls",
        1,
        200,
        "From the next background turn.",
    ),
    Limit("continuation_max_turns_per_root", "Decisions per chain", "decisions", 1, 20, _CHAIN_NOW),
    Limit("continuation_max_spawns_per_root", "Work items one chain may start", "work items", 1, 30, _CHAIN_NOW),
    Limit("continuation_max_depth", "Depth of a chain", "levels", 1, 6, _CHAIN_NOW),
    Limit("continuation_max_tokens_per_root", "Tokens per chain", "tokens", 10_000, 1_000_000, _CHAIN_NOW),
    Limit("continuation_stall_limit", "Chain stall limit", "steps without progress", 1, 5, _CHAIN_NOW),
    Limit(
        "intention_root_ttl_hours",
        "Lifetime of a chain",
        "hours",
        1,
        168,
        "For chains that start after the change. A running chain keeps the end it was given.",
    ),
    Limit(
        "intention_proposal_ttl_hours",
        "How long a question or proposal waits",
        "hours",
        1,
        72,
        "For questions and proposals made after the change. Some checks of the ones already waiting use the new value.",
    ),
    Limit(
        "subtask_default_timeout",
        "Subtask time limit, default",
        "seconds",
        60,
        6000,
        "For subtasks that start after the change.",
    ),
    Limit(
        "subtask_max_timeout",
        "Subtask time limit, largest",
        "seconds",
        60,
        10_000,
        "For subtasks that start after the change. It also sets how long the server waits before it marks an "
        "unfinished tool call as unknown.",
    ),
)

_BY_KEY: dict[str, Limit] = {limit.key: limit for limit in LIMITS}
# The cross rule: (lower, upper) pairs that must hold as lower <= upper after any change.
_ORDERED: tuple[tuple[str, str], ...] = (("subtask_default_timeout", "subtask_max_timeout"),)


def by_key(key: str) -> Limit | None:
    """The registry entry of ``key``, or None for a key the owner may not change."""
    return _BY_KEY.get(key)


def whole_number(value: Any) -> bool:
    """Whether ``value`` is a whole number: an ``int`` and not a ``bool`` (a float, even ``50.0``, is not)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _n(value: float) -> str:
    return f"{value:,}" if float(value).is_integer() else f"{value:,.2f}"


def range_problem(limit: Limit, value: Any) -> str | None:
    """Why ``value`` cannot be the owner's value of ``limit`` (a whole number inside the range), else None."""
    if whole_number(value) and limit.low <= value <= limit.high:
        return None
    return f"{limit.label} must be a whole number from {_n(limit.low)} to {_n(limit.high)} ({limit.unit})."


def ceiling_problem(limit: Limit, value: int, ceiling: int | None) -> str | None:
    """Why ``value`` is above the host's ceiling for ``limit``, else None."""
    if ceiling is None or value <= ceiling:
        return None
    return f"The server allows at most {_n(ceiling)} for {limit.label.lower()}."


def cross_problem(values: Mapping[str, float], changed: Iterable[str] | None = None) -> str | None:
    """Why the effective values in ``values`` break the cross rule (a default subtask time limit above the largest),
    else None. A pair with either key missing is not checked, and with ``changed`` given, neither is a pair that
    holds none of those keys (the host's own values are the host's)."""
    keys = None if changed is None else set(changed)
    for lower, upper in _ORDERED:
        if keys is not None and not keys & {lower, upper}:
            continue
        if lower in values and upper in values and values[lower] > values[upper]:
            return (
                f"{_BY_KEY[lower].label} ({_n(values[lower])} {_BY_KEY[lower].unit}) cannot be above "
                f"{_BY_KEY[upper].label.lower()} ({_n(values[upper])} {_BY_KEY[upper].unit})."
            )
    return None


def parse_ceilings(raw: str) -> dict[str, int]:
    """``NOUS_AUTONOMY_CEILINGS`` as ``{key: ceiling}``. An entry that is not ``key=whole number`` for a registry key
    with the number inside the key's range is dropped, and one warning counts the dropped entries. A later entry for
    the same key replaces an earlier one. Empty: no ceiling."""
    ceilings: dict[str, int] = {}
    dropped = 0
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, number = (part.strip() for part in entry.partition("="))
        limit = _BY_KEY.get(key)
        if not sep or limit is None or not number.isascii() or not number.isdigit():
            dropped += 1
            continue
        value = int(number)
        if range_problem(limit, value) is not None:
            dropped += 1
            continue
        ceilings[key] = value
    if dropped:
        logger.warning(
            "NOUS_AUTONOMY_CEILINGS: %d entr%s dropped (each must be key=whole number for a limit the owner may "
            "change, inside that limit's range)",
            dropped,
            "y" if dropped == 1 else "ies",
        )
    return ceilings
