"""F108 Phase 3: what counts as a repeated or failed-again tool call, one definition for the Patterns endpoint and
the tool-notes detection, plus error-text normalisation and guard classification.

The SQL pieces are fragments over a window of ``nous_system.tool_call_log`` rows that carry
``turn_id, tool_name, input_text, is_error, call_index, id``; ``detect_patterns`` and ``pattern_counts`` read that
table.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.cognitive.ledger_store import REFUSAL_CODES, redact_output

logger = logging.getLogger(__name__)

# A repeated call: two calls of one turn with the same tool and the same input.
REPEAT_KEY = "turn_id, tool_name, input_text"
# prev_error is the is_error of the turn's previous identical call (by call_index), NULL for the first.
PREV_ERROR_EXPR = f"lag(is_error) OVER (PARTITION BY {REPEAT_KEY} ORDER BY call_index, id)"
# A call that has a previous identical call is a repeat; an error after an identical error is a failed retry.
REPEATED_SQL = "prev_error IS NOT NULL"
RETRY_FAILED_SQL = "is_error AND prev_error"

NO_ERROR_TEXT = "(no error text)"
NORMALISED_MAX_CHARS = 160

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?")
_PATH = re.compile(r"(?:\b[a-z]:[\\/][^\s'\"]*|(?<![\w<>])/[^\s'\"]+)")
_HEX = re.compile(r"\b(?=[0-9]*[a-f])[0-9a-f]{8,}\b")  # needs a letter: a long digit run is a number
_DIGITS = re.compile(r"\d+")
_QUOTED = re.compile(r"""(['"])([^'"\n]*?)\1""")  # every pair, so a short value cannot shift the pairing
_QUOTED_MIN_CHARS = 3


def _quoted_to_str(m: re.Match[str]) -> str:
    """``<str>`` for a quoted value of 3 or more characters; a shorter pair is kept as it is."""
    return "<str>" if len(m.group(2)) >= _QUOTED_MIN_CHARS else m.group(0)


_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (_UUID, "<id>"),
    (_ISO_TIME, "<time>"),
    (_PATH, "<path>"),
    (_HEX, "<hex>"),
    (_DIGITS, "<n>"),
    (_QUOTED, _quoted_to_str),
)


def normalise_error(text: str | None) -> str:
    """Error text with its variable parts replaced, so repeats of one mistake compare equal: lowercase, ids, times,
    absolute paths, hex runs, numbers and quoted strings replaced, whitespace collapsed, at most 160 characters."""
    if text is None or not text.strip():
        return NO_ERROR_TEXT
    out = text.lower()
    for pattern, repl in _SUBSTITUTIONS:
        out = pattern.sub(repl, out)
    out = " ".join(out.split())
    return out[:NORMALISED_MAX_CHARS] or NO_ERROR_TEXT


# Wording the guards emit, lowercase. The guards live in handlers that are not edited here, so a test pins their
# sentences to these fragments: a reworded guard fails that test.
GUARD_FRAGMENTS: tuple[str, ...] = (
    "refused",
    "refusing",
    "may only",
    "may not",
    "allowlist",
    "lineage",
    "not authori",
    "unauthori",
    "budget",
    "credential",
)


# Phrases that carry a guard word but are transient network failures, not guards (see nous/cognitive/monitor.py).
# They are blanked before the fragment match.
TRANSIENT_PHRASES: tuple[str, ...] = (
    "connectionrefusederror",
    "connection refused",
)

# ``duplicate`` and ``offered_set`` are model mistakes (a repeated call, a tool not offered), not guards.
GUARD_CODES: frozenset[str] = frozenset(REFUSAL_CODES) - {"duplicate", "offered_set"}


def guard_cause(outcome: str, refused_by: str | None, error_text: str | None) -> str | None:
    """The guard behind a call, or None when it is an ordinary mistake or success.

    A runner refusal is labelled by its code (``duplicate`` and ``offered_set`` are model mistakes and stay
    ordinary), an unlabelled ``refused``/``gated`` outcome by the outcome, and a handler-enforced guard (an
    ``error`` outcome whose text carries one of ``GUARD_FRAGMENTS``) by ``fragment``.
    """
    if refused_by is not None:
        if refused_by in GUARD_CODES:
            return refused_by
    elif outcome in ("refused", "gated"):
        return outcome
    lowered = (error_text or "").lower()
    for phrase in TRANSIENT_PHRASES:
        lowered = lowered.replace(phrase, " ")
    if lowered and any(f in lowered for f in GUARD_FRAGMENTS):
        return "fragment"
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Detection: repeated tool mistakes and their rates, per tool, from nous_system.tool_call_log
# ---------------------------------------------------------------------------------------------------------------------

WINDOW_DAYS = 14
MIN_HITS = 3
# A tool with this many error rows or more in the window is skipped for ``error_repeat``: its hits would be a bound,
# and a bound never becomes a rate.
ERROR_ROWS_CAP = 5000
PATTERN_KINDS: tuple[str, ...] = ("error_repeat", "call_repeat", "retry_failed")
EXAMPLE_MAX_CHARS = 200

# A length bound on the input text read per hit row (the recorder clips inputs at ``tool_input_max_chars``, 2000 by
# default). Only a bound: the example is redacted with ``redact_output`` and then cut to the shown length.
_INPUT_FETCH_CHARS = 2000

_WINDOW_CTE = """
WITH win AS (
    SELECT id, turn_id, call_index, tool_name, input_text, is_error, outcome, refused_by, error_text, started_at
    FROM nous_system.tool_call_log
    WHERE agent_id = :agent AND started_at >= :since AND started_at < :until
      AND (CAST(:tool AS text) IS NULL OR tool_name = :tool)
)
"""

_CALLS_SQL = (
    _WINDOW_CTE
    + "SELECT tool_name, count(*)::int AS calls, (count(*) FILTER (WHERE is_error))::int AS errors "
    + "FROM win GROUP BY tool_name"
)

# The repeat hits: calls with a previous identical call in their turn (turnless calls have no turn to repeat in).
_REPEAT_ROWS_SQL = (
    _WINDOW_CTE
    + f"""
SELECT tool_name, left(input_text, {_INPUT_FETCH_CHARS}) AS input_text, is_error, prev_error, outcome, refused_by,
       error_text
FROM (
    SELECT tool_name, input_text, is_error, outcome, refused_by, error_text, started_at, id,
           {PREV_ERROR_EXPR} AS prev_error
    FROM win
    WHERE turn_id IS NOT NULL
) x
WHERE {REPEATED_SQL}
ORDER BY started_at, id
"""
)

_ERROR_ROWS_SQL = (
    _WINDOW_CTE
    + """
SELECT tool_name, outcome, refused_by, error_text
FROM win
WHERE is_error AND tool_name = ANY(CAST(:tools AS text[]))
ORDER BY started_at, id
"""
)


@dataclass(frozen=True)
class Pattern:
    """One repeated mistake of one tool, with its hits and the tool's calls over the same window."""

    kind: str
    tool_name: str
    key: str
    hits: int
    calls: int
    guard: str | None
    example: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.tool_name}:{hashlib.sha1(self.key.encode()).hexdigest()[:8]}"

    @property
    def rate(self) -> float:
        return self.hits / self.calls if self.calls else 0.0


def _require_aware(**times: datetime) -> None:
    for name, value in times.items():
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")


async def _set_timeout(session: AsyncSession) -> None:
    # SET LOCAL binds the transaction the session autobegan; every statement here runs in that one transaction.
    await session.execute(text("SET LOCAL statement_timeout = '10s'"))


def _params(agent_id: str, since: datetime, until: datetime, tool: str | None) -> dict[str, Any]:
    return {"agent": agent_id, "since": since, "until": until, "tool": tool}


def _guard_of(labels: list[str | None]) -> str | None:
    """The most common guard label when at least half of the hit rows carry one, else None."""
    guarded = Counter(label for label in labels if label)
    if not guarded or 2 * sum(guarded.values()) < len(labels):
        return None
    return min(guarded, key=lambda label: (-guarded[label], label))


def _example(raw: str | None) -> str:
    return redact_output(raw or "")[:EXAMPLE_MAX_CHARS]


class _ErrorKeys:
    """Normalised keys of redacted error text, memoised per distinct text: redaction runs first, so the key (and the
    example and id made from it) never carries a secret half-mangled by the normalisation."""

    def __init__(self) -> None:
        self._memo: dict[str | None, str] = {}

    def key(self, error_text: str | None) -> str:
        key = self._memo.get(error_text)
        if key is None:
            key = self._memo[error_text] = normalise_error(redact_output(error_text) if error_text else error_text)
        return key


async def _tool_calls(session: AsyncSession, params: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """``{tool: (calls, errors)}`` of the window."""
    rows = (await session.execute(text(_CALLS_SQL), params)).mappings()
    return {r["tool_name"]: (r["calls"], r["errors"]) for r in rows}


async def detect_patterns(
    session: AsyncSession,
    agent_id: str,
    *,
    now: datetime,
    days: int = WINDOW_DAYS,
    min_hits: int = MIN_HITS,
) -> tuple[list[Pattern], int]:
    """The repeated tool mistakes of the ``days`` days before ``now`` with at least ``min_hits`` hits, most hits
    first (ties by id), and the number of tools skipped for ``error_repeat`` because their error rows reached
    ``ERROR_ROWS_CAP``."""
    _require_aware(now=now)
    await _set_timeout(session)
    params = _params(agent_id, now - timedelta(days=days), now, None)
    # A tool can appear between the statements (READ COMMITTED): .get() below tolerates it, counting 0 calls.
    tools = await _tool_calls(session, params)
    patterns: list[Pattern] = []

    # call_repeat / retry_failed: the hit rows of each tool; the example is the first hit's input.
    repeat: dict[str, list[Any]] = {}
    retry: dict[str, list[Any]] = {}
    for row in (await session.execute(text(_REPEAT_ROWS_SQL), params)).mappings():
        repeat.setdefault(row["tool_name"], []).append(row)
        if row["is_error"] and row["prev_error"]:
            retry.setdefault(row["tool_name"], []).append(row)
    for kind, by_tool in (("call_repeat", repeat), ("retry_failed", retry)):
        for tool, rows in by_tool.items():
            if len(rows) >= min_hits:
                labels = [guard_cause(r["outcome"], r["refused_by"], r["error_text"]) for r in rows]
                example = _example(rows[0]["input_text"])
                patterns.append(
                    Pattern(kind, tool, "", len(rows), tools.get(tool, (0, 0))[0], _guard_of(labels), example)
                )

    # error_repeat: group the error rows in Python per tool, unless the tool's error rows reach the cap.
    wanted = [t for t, (_, errors) in tools.items() if errors > 0]
    capped = [t for t in wanted if tools[t][1] >= ERROR_ROWS_CAP]
    for tool in capped:
        logger.warning("tool patterns: %s has %d error rows in the window, error_repeat skipped", tool, tools[tool][1])
    readable = [t for t in wanted if t not in capped]
    if readable:
        keys = _ErrorKeys()
        groups: dict[tuple[str, str], list[str | None]] = {}
        for row in (await session.execute(text(_ERROR_ROWS_SQL), {**params, "tools": readable})).mappings():
            label = guard_cause(row["outcome"], row["refused_by"], row["error_text"])
            groups.setdefault((row["tool_name"], keys.key(row["error_text"])), []).append(label)
        for (tool, key), labels in groups.items():
            if len(labels) >= min_hits:
                example = _example(key)
                patterns.append(
                    Pattern(
                        "error_repeat", tool, key, len(labels), tools.get(tool, (0, 0))[0], _guard_of(labels), example
                    )
                )

    patterns.sort(key=lambda p: (-p.hits, p.id))
    return patterns, len(capped)


async def pattern_counts(
    session: AsyncSession,
    agent_id: str,
    tool_name: str,
    kind: str,
    key: str,
    *,
    since: datetime,
    until: datetime,
) -> tuple[int, int] | None:
    """``(hits, calls)`` of one pattern of one tool over ``[since, until)``, or None when an ``error_repeat`` count
    would reach ``ERROR_ROWS_CAP`` (a bound never becomes a rate)."""
    if kind not in PATTERN_KINDS:
        raise ValueError(f"unknown pattern kind: {kind!r}")
    _require_aware(since=since, until=until)
    await _set_timeout(session)
    params = _params(agent_id, since, until, tool_name)
    calls, errors = (await _tool_calls(session, params)).get(tool_name, (0, 0))
    if kind == "error_repeat":
        if errors >= ERROR_ROWS_CAP:
            return None
        if errors == 0:
            return 0, calls
        keys = _ErrorKeys()
        rows = (await session.execute(text(_ERROR_ROWS_SQL), {**params, "tools": [tool_name]})).mappings()
        return sum(1 for r in rows if keys.key(r["error_text"]) == key), calls
    rows = (await session.execute(text(_REPEAT_ROWS_SQL), params)).mappings()
    if kind == "call_repeat":
        return sum(1 for _ in rows), calls
    return sum(1 for r in rows if r["is_error"] and r["prev_error"]), calls
