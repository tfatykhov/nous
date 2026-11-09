"""F108 Phase 3: what counts as a repeated or failed-again tool call, one definition for the Patterns endpoint and
the tool-notes detection, plus error-text normalisation and guard classification.

Pure: no database. The SQL pieces are fragments over a window of ``nous_system.tool_call_log`` rows that carry
``turn_id, tool_name, input_text, is_error, call_index, id``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from nous.cognitive.ledger_store import REFUSAL_CODES

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


# Phrases that carry a guard word but are transient failures, not guards: a network error (see
# nous/cognitive/monitor.py) and the runner's "execution ledger could not record this send" (runner.py), which is
# retryable. They are blanked before the fragment match.
TRANSIENT_PHRASES: tuple[str, ...] = (
    "connectionrefusederror",
    "connection refused",
    "send refused: the execution ledger could not record this send",
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
