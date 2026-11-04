"""Tool-call markup that leaked into model-written text, and its removal.

A model writing a tool call can end a string parameter with markup instead of ending
the value, for instance ``</content>``, and go on with the next parameters as markup:
``<parameter name="category">technical``, ``<category>technical</category>``,
``</invoke>``, sometimes a whole further call. The API then keeps all of it inside the
string, and a write tool stores it as part of a fact or a decision.

``strip_tool_markup`` removes that spill-over and nothing else. It cuts at the first
tag that closes the value (or opens a new call) when all of these hold:

* only markup follows the tag (whitespace, then another tag, or the end of the text);
* the tag closes no element the text itself opened (``<content>...</content>`` in a
  fact about Atom feeds stays);
* it is not inside a code span (an odd number of backticks before it means quoted);
* what follows is tool-call markup: it holds a ``parameter``, ``invoke`` or
  ``function_calls`` tag, or the tag closes the field's own parameter and more tags
  follow (``</content><category>…``).

It also removes a ``<parameter name="...">`` that opens the text. Markup in the middle
of prose is left alone: it cannot be told from a fact that quotes the format.

``nous.utils.leaked_markup_start`` is the dispatcher's narrower locator: it finds only a
run that ends in an unterminated ``<parameter>`` tag, to salvage a REQUIRED argument
from it. Most stored leaks end otherwise (``</invoke>``, ``<category>…</category>``),
and an optional argument is never salvaged, so the write paths use this function.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_NS = r"(?:antml:)?"
# Every tag-shaped token: <name ...>, </name>, <name/>. Led by a literal and bounded by
# the next '<' or '>', so one pass over the text is linear.
_TOKEN = re.compile(rf"<(/?){_NS}([A-Za-z_][\w-]*)([^<>]*)>")
# A tag of the tool-call format, which nothing else uses.
_TOOL_TAG = re.compile(rf"</?{_NS}(?:parameter|invoke|function_calls)\b[^<>]*>")
_CALL = re.compile(rf"<{_NS}invoke\s+name=\"([^\"<>]*)\"\s*>")
_LEADING = re.compile(
    rf"\A\s*(?:<{_NS}function_calls>\s*)?(?:<{_NS}invoke\s+name=\"[^\"<>]*\">\s*)?"
    rf"<{_NS}parameter\s+name=\"[^\"<>]*\">"
)
# After the first tag of the spill-over: only whitespace, then another tag or the end.
_MARKUP_FOLLOWS = re.compile(r"\s*(?:<|\Z)")


def _tail_start(text: str, field: str | None) -> int | None:
    """Where the spill-over starts, or None.

    One forward pass over the tags. Open and close counts per name, the backtick count
    and the position of the last tool-call tag are kept as it goes, so no candidate
    rescans the text: model text reaches this on the event loop, and a rescan per tag
    is quadratic in the tag count (see ``nous.utils.leaked_markup_start``).
    """
    tokens = list(_TOKEN.finditer(text))
    last_tool_tag = max((m.start() for m in _TOOL_TAG.finditer(text)), default=-1)
    opened: dict[str, int] = {}
    closed: dict[str, int] = {}
    backticks, counted_to = 0, 0
    for i, m in enumerate(tokens):
        start = m.start()
        backticks += text.count("`", counted_to, start)
        counted_to = start
        closing, name, attrs = m.group(1) == "/", m.group(2), m.group(3)
        starts_call = not closing and (
            (name == "function_calls" and not attrs.strip()) or (name == "invoke" and _CALL.fullmatch(m.group(0)))
        )
        if (
            (closing or starts_call)
            and backticks % 2 == 0  # an odd count: inside a code span, quoted
            and _MARKUP_FOLLOWS.match(text, m.end())
            and (not closing or opened.get(name, 0) <= closed.get(name, 0))  # closes nothing the text opened
        ):
            own_field = closing and name == field and len(tokens) - i >= 2
            if last_tool_tag >= start or own_field:
                return start
        if closing:
            closed[name] = closed.get(name, 0) + 1
        elif not attrs.rstrip().endswith("/"):
            opened[name] = opened.get(name, 0) + 1
    return None


def strip_tool_markup(text: str, field: str | None = None) -> tuple[str, str]:
    """Return ``(text without the leaked markup, the markup removed)``.

    ``field`` is the name of the tool parameter the text came from (``content`` for
    ``learn_fact``); a closing tag of that name counts as markup even when no
    ``parameter``/``invoke`` tag follows. Returns ``(text, "")`` when nothing leaked.
    """
    if not text or "<" not in text:
        return text, ""
    removed: list[str] = []
    lead = _LEADING.match(text)
    if lead:
        removed.append(lead.group(0))
        text = text[lead.end() :]
    start = _tail_start(text, field)
    if start is not None:
        removed.append(text[start:])
        text = text[:start]
    if not removed:
        return text, ""
    return text.strip(), "".join(removed)


def calls_in(removed: str) -> list[str]:
    """Names of the tool calls the removed markup held: calls that never ran."""
    return _CALL.findall(removed)


def params_in(removed: str) -> list[str]:
    """Names the removed markup gave values to in the call it closed (not in calls after it).

    Those values were meant as the call's other arguments and are not stored anywhere else.
    """
    first_call = _CALL.search(removed)
    head = removed[: first_call.start()] if first_call else removed
    names: list[str] = []
    for m in _TOKEN.finditer(head):
        if m.group(1):
            continue
        name: str | None = m.group(2)
        if name == "parameter":
            attr = re.search(r'name="([^"<>]*)"', m.group(3))
            name = attr.group(1) if attr else None
        elif name in ("invoke", "function_calls"):
            name = None
        if name and name not in names:
            names.append(name)
    return names


def clean_model_text(text: str | None, *, tool: str, field: str) -> tuple[str | None, str]:
    """``strip_tool_markup`` for a write path, with a warning naming the tool when it strips.

    The warning carries sizes and the names of any calls dropped, never the text.
    """
    if not text:
        return text, ""
    clean, removed = strip_tool_markup(text, field)
    if removed:
        logger.warning(
            "%s: removed %d chars of tool-call markup from %s before storing it (calls that did not run: %s)",
            tool,
            len(removed),
            field,
            ", ".join(calls_in(removed)) or "none",
        )
    return clean, removed


def markup_note(*removed: str) -> str:
    """One line for a tool result when markup was removed, else ``""``."""
    removed_text = "".join(removed)
    if not removed_text:
        return ""
    note = "\nNote: tool-call markup at the end of the text was removed before storing it."
    calls = calls_in(removed_text)
    if calls:
        note += f" It held tool calls that did not run ({', '.join(calls)}); make them again as separate calls."
    return note
