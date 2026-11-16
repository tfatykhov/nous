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
    """One limit: the ``Settings`` field it sets (``key``), its words on the screen (``label``, ``unit``, and
    ``description``, one plain sentence of what it limits), its hard range and when a change takes effect
    (``applies``)."""

    key: str
    label: str
    unit: str
    low: int
    high: int
    description: str
    applies: str


# A chain: a background task, what Nous decides when it finishes, and the follow-up tasks that decision starts.
_CHAIN = "A chain is a background task, what Nous decides when it finishes, and the follow-up tasks it starts."
_CHAIN_NOW = "At once. A chain that is already running and is above the new value stops at its next step."
_STOPS = "Nous then stops the chain and reports to you."

LIMITS: tuple[Limit, ...] = (
    Limit(
        "max_turns",
        "Rounds of tool use per turn",
        "rounds",
        5,
        1000,
        "How many rounds of tool calls one turn may take before Nous must answer, in chat and in background work "
        "alike.",
        "From the next turn.",
    ),
    Limit(
        "subtask_tool_call_limit",
        "Tool calls per background task",
        "tool calls",
        1,
        200,
        "How many tool calls one run of a background task may make: a spawned task, a workflow step, a scheduled "
        "task, a companion action, or one decision in a chain. Heartbeat triage, heartbeat checks and their callbacks "
        "and workflow summaries are not limited by it.",
        "From the next task that starts.",
    ),
    Limit(
        "continuation_max_turns_per_root",
        "Decisions per chain",
        "decisions",
        1,
        20,
        f"{_CHAIN} This is how many times Nous may decide what to do next in one chain. {_STOPS}",
        _CHAIN_NOW,
    ),
    Limit(
        "continuation_max_spawns_per_root",
        "Follow-up tasks per chain",
        "tasks",
        1,
        30,
        f"How many follow-up tasks one chain may start in all. {_STOPS}",
        _CHAIN_NOW,
    ),
    Limit(
        "continuation_max_depth",
        "Levels of follow-up per chain",
        "levels",
        1,
        6,
        f"How deep a chain may go: a task that starts a task that starts a task is three levels. {_STOPS}",
        _CHAIN_NOW,
    ),
    Limit(
        "continuation_max_tokens_per_root",
        "Tokens per chain",
        "tokens",
        10_000,
        1_000_000,
        f"How many model tokens one chain may spend in all, its tasks and its decisions together. {_STOPS}",
        _CHAIN_NOW,
    ),
    Limit(
        "continuation_stall_limit",
        "Decisions in a row without progress",
        "decisions",
        1,
        5,
        f"How many decisions in a row of one chain may report no progress. {_STOPS}",
        _CHAIN_NOW,
    ),
    Limit(
        "intention_root_ttl_hours",
        "Lifetime of a chain",
        "hours",
        1,
        168,
        "How long a chain may run, counted from its first task, before it ends.",
        "For chains that start after the change. A running chain keeps the end it was given, and a task it starts "
        "after the change ends no later than the new value after its start.",
    ),
    Limit(
        "intention_proposal_ttl_hours",
        "Time to answer a question or proposal",
        "hours",
        1,
        72,
        "How long a question Nous asks you, or an action it proposes, waits for your answer before it lapses.",
        "For proposals made after the change: a proposal already waiting keeps its deadline. A proposal you "
        "approved that has not run yet is still run only if you approved it within the new value. A question already "
        "waiting is judged by the new value at once: lowering it can close the question, and raising it can reopen "
        "one that had lapsed.",
    ),
    Limit(
        "subtask_default_timeout",
        "Time limit of a spawned task that names none",
        "seconds",
        60,
        6000,
        "How long a task spawned to run in the background, a scheduled task or the first task of a goal session may "
        "run when it gives no time limit of its own. A task the turn waits for has its own, shorter default.",
        "For tasks and schedules created after the change. A task or schedule already made keeps the time limit it "
        "was given.",
    ),
    Limit(
        "subtask_max_timeout",
        "Longest time limit of a spawned task",
        "seconds",
        60,
        10_000,
        "The longest a spawned task or the first task of a goal session may run, whatever time limit it asks for. A "
        "tool call left unfinished is not marked unknown before this limit plus 10 minutes have passed.",
        "When a task starts: a spawned task or the first task of a goal session that is still waiting runs no longer "
        "than the new value. A running task keeps the time limit it started with, and a raised value never lengthens "
        "a task already made. A workflow step, a scheduled task and a companion action keep their own time limit.",
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
    with the number inside the key's range is dropped, and one warning names the key of each dropped entry (never its
    number). A later entry for the same key replaces an earlier one. Empty: no ceiling."""
    ceilings: dict[str, int] = {}
    dropped: list[str] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, number = (part.strip() for part in entry.partition("="))
        limit = _BY_KEY.get(key)
        if not sep or limit is None or not number.isascii() or not number.isdigit():
            dropped.append(key[:40])
            continue
        value = int(number)
        if range_problem(limit, value) is not None:
            dropped.append(key)
            continue
        ceilings[key] = value
    if dropped:
        logger.warning(
            "NOUS_AUTONOMY_CEILINGS: the entries for %s were dropped (each must be key=whole number for a limit the "
            "owner may change, inside that limit's range)",
            ", ".join(dropped),
        )
    return ceilings
