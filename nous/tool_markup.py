"""Tool-call markup that leaked into model-written text, and its removal.

A model writing a tool call can end a string parameter with markup instead of ending
the value, for instance ``</content>``, and go on with the next parameters as markup:
``<parameter name="category">technical``, ``<category>technical</category>``,
``</invoke>``, sometimes a whole further call. The API then keeps all of it inside the
string, and a write tool stores it as part of a fact or a decision.

``strip_tool_markup`` removes that spill-over and nothing else. It cuts at the first tag,
outside a code span, that starts it:

* a closing tag that closes nothing the text opened, followed (after whitespace) by:
  the end of the text, when the tag is ``</parameter>``, ``</invoke>`` or
  ``</function_calls>``; a ``<parameter name=…>``, ``<invoke name=…>`` or
  ``<function_calls>`` tag; one of those closing tags; ``<x>`` where ``x`` is one of the
  tool's parameters, when the closing tag is the field's own (``</content><category>``)
  or ``</parameter>``; or a run of plain ``<x>…</x>`` elements that a tool closing tag
  or a new call ends. A following ``</entry>`` or ``<footer>…</footer>`` is prose.
* ``<invoke name=…>`` or ``<function_calls>`` followed by markup: a new call.
* ``<parameter name=…>``, ``<x>`` (a parameter of the tool) or a plain ``<x>`` that starts
  a run of elements ending in a tool closing tag or a new call, or a
  ``<parameter name=…>`` left open to the end of the text (the shape
  ``nous.utils.leaked_markup_start`` finds for the dispatcher).

A run of complete elements with nothing after it (``<parameter name="x">v</parameter>``)
is kept: it is as likely a quoted example. Code spans follow CommonMark: a run of N
backticks opens one that only a run of exactly N backticks closes, and markup inside
one is kept. It also removes a ``<parameter name="...">`` that opens the text. Markup in
the middle of prose is left alone.

The dispatcher's ``_salvage_leaked_args`` (with ``leaked_markup_start``) fires only for a
missing REQUIRED argument, so the write paths call this for every value they store.
"""

from __future__ import annotations

import bisect
import logging
import re
from collections import deque
from collections.abc import Collection

logger = logging.getLogger(__name__)

_NS = r"(?:antml:)?"
# Every tag-shaped token: <name ...>, </name>, <name/>. Possessive repeats, so a '<'
# with no '>' after it fails in one pass instead of retrying every name/attribute split.
_TOKEN = re.compile(rf"<(/?){_NS}([A-Za-z_][\w-]*+)([^<>]*+)>")
_CALL = re.compile(rf"<{_NS}invoke\s+name=\"([^\"<>]*)\"\s*>")
_PARAM_ATTR = re.compile(r"\s+name=\"([^\"<>]*)\"\s*")
_LEADING = re.compile(
    rf"\A\s*(?:<{_NS}function_calls>\s*)?(?:<{_NS}invoke\s+name=\"[^\"<>]*\">\s*)?"
    rf"<{_NS}parameter\s+name=\"[^\"<>]*\">"
)
_BACKTICKS = re.compile(r"`+")
_TOOL_TAGS = frozenset({"parameter", "invoke", "function_calls"})
_NAME_CHARS = re.compile(r"[^\w.-]")

# Token kinds.
_CLOSE_TOOL, _CLOSE, _CALL_OPEN, _PARAM, _PARAM_TAG, _TAG, _OTHER = range(7)
_END, _PROSE = -1, -2


def _code_spans(text: str) -> list[tuple[int, int]]:
    """CommonMark code spans: a run of N backticks to the next run of exactly N. Linear."""
    runs = [(m.start(), m.end()) for m in _BACKTICKS.finditer(text)]
    by_len: dict[int, deque[int]] = {}
    for i, (s, e) in enumerate(runs):
        by_len.setdefault(e - s, deque()).append(i)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(runs):
        same = by_len[runs[i][1] - runs[i][0]]
        while same and same[0] <= i:
            same.popleft()
        if same:
            j = same.popleft()
            spans.append((runs[i][0], runs[j][1]))
            i = j + 1
        else:
            i += 1  # no closing run: literal backticks
    return spans


def _kind(m: re.Match[str], params: Collection[str]) -> int:
    closing, name, attrs = m.group(1) == "/", m.group(2), m.group(3)
    if closing:
        return _CLOSE_TOOL if name in _TOOL_TAGS else _CLOSE
    if name == "invoke":
        return _CALL_OPEN if _CALL.fullmatch(m.group(0)) else _OTHER
    if name == "function_calls":
        return _CALL_OPEN if not attrs.strip() else _OTHER
    if name == "parameter":
        return _PARAM if _PARAM_ATTR.fullmatch(attrs) else _OTHER
    if attrs.strip():
        return _OTHER
    return _PARAM_TAG if name in params else _TAG


class _Scan:
    """The tags of one text, with what follows each, for one forward pass."""

    def __init__(self, text: str, params: Collection[str]) -> None:
        self.text = text
        self.tokens = list(_TOKEN.finditer(text))
        self.kinds = [_kind(m, params) for m in self.tokens]
        self.names = [m.group(2) for m in self.tokens]
        self._runs: dict[int, bool] = {}
        # Closing-tag indices per name, for a binary search instead of a scan per element.
        self._closers: dict[str, list[int]] = {}
        for i, kind in enumerate(self.kinds):
            if kind in (_CLOSE, _CLOSE_TOOL):
                self._closers.setdefault(self.names[i], []).append(i)
        self._last_lt = text.rfind("<")

    def _closer_after(self, j: int, name: str) -> int | None:
        indices = self._closers.get(name, [])
        k = bisect.bisect_right(indices, j)
        return indices[k] if k < len(indices) else None

    def next_of(self, i: int) -> int:
        """The token after token ``i`` when only whitespace is between, else _END or _PROSE."""
        end = self.tokens[i].end()
        if i + 1 < len(self.tokens):
            gap = self.text[end : self.tokens[i + 1].start()]
            return i + 1 if not gap.strip() else _PROSE
        return _END if not self.text[end:].strip() else _PROSE

    def run_ends_in_markup(self, j: int) -> bool:
        """Whether the run of elements from token ``j`` ends in a tool closing tag, a new call,
        or a ``<parameter name=…>`` left open to the end. Memoised per element, so every token
        is read once across all the runs a text holds."""
        visited: list[int] = []
        result = False
        while True:
            if j in self._runs:
                result = self._runs[j]
                break
            visited.append(j)
            c = self._closer_after(j, "parameter" if self.kinds[j] == _PARAM else self.names[j])
            if c is None:
                # Left open to the end with no '<' in its value, as leaked_markup_start requires.
                result = self.kinds[j] == _PARAM and self._last_lt < self.tokens[j].end()
                break
            nxt = self.next_of(c)
            if nxt < 0:
                break
            if self.kinds[nxt] in (_CLOSE_TOOL, _CALL_OPEN):
                result = True
                break
            if self.kinds[nxt] not in (_PARAM, _PARAM_TAG, _TAG):
                break
            j = nxt
        for v in visited:
            self._runs[v] = result
        return result


def _tail_start(text: str, field: str | None, params: Collection[str]) -> int | None:
    """Where the spill-over starts, or None. One forward pass; see the module docstring."""
    scan = _Scan(text, params)
    spans = _code_spans(text)
    span_i = 0
    opened: dict[str, int] = {}
    closed: dict[str, int] = {}
    for i, m in enumerate(scan.tokens):
        start, kind, name = m.start(), scan.kinds[i], scan.names[i]
        while span_i < len(spans) and spans[span_i][1] <= start:
            span_i += 1
        quoted = span_i < len(spans) and spans[span_i][0] <= start
        if not quoted and _starts_spill(scan, i, kind, name, field, opened, closed):
            return start
        if kind in (_CLOSE, _CLOSE_TOOL):
            closed[name] = closed.get(name, 0) + 1
        elif not m.group(3).rstrip().endswith("/"):
            opened[name] = opened.get(name, 0) + 1
    return None


def _starts_spill(
    scan: _Scan, i: int, kind: int, name: str, field: str | None, opened: dict[str, int], closed: dict[str, int]
) -> bool:
    if kind in (_CLOSE, _CLOSE_TOOL):
        if opened.get(name, 0) > closed.get(name, 0):
            return False  # it closes an element the text opened
        nxt = scan.next_of(i)
        if nxt == _END:
            return kind == _CLOSE_TOOL
        if nxt == _PROSE:
            return False
        after = scan.kinds[nxt]
        if after in (_PARAM, _CALL_OPEN, _CLOSE_TOOL):
            return True
        if after == _PARAM_TAG and (name == field or kind == _CLOSE_TOOL):
            return True
        return after in (_PARAM_TAG, _TAG) and scan.run_ends_in_markup(nxt)
    if kind == _CALL_OPEN:
        return scan.next_of(i) != _PROSE
    if kind in (_PARAM, _PARAM_TAG, _TAG):
        return scan.run_ends_in_markup(i)
    return False


def strip_tool_markup(text: str, field: str | None = None, params: Collection[str] = ()) -> tuple[str, str]:
    """Return ``(text without the leaked markup, the markup removed)``.

    ``field`` is the tool parameter the text came from (``content`` for ``learn_fact``)
    and ``params`` the tool's parameter names: ``<category>`` counts as markup only when
    ``category`` is one of them. Returns ``(text, "")`` when nothing leaked.
    """
    if not isinstance(text, str) or "<" not in text:
        return text, ""
    removed: list[str] = []
    lead = _LEADING.match(text)
    if lead:
        removed.append(lead.group(0))
        text = text[lead.end() :]
    start = _tail_start(text, field, params)
    if start is not None:
        removed.append(text[start:])
        text = text[:start]
    if not removed:
        return text, ""
    return text.strip(), "".join(removed)


def calls_in(removed: str) -> list[str]:
    """Names of the tool calls the removed markup held: calls that never ran."""
    return _CALL.findall(removed)


def own_call_part(removed: str) -> str:
    """The removed markup up to the first new call: the part that belongs to the call it closed."""
    first_call = _CALL.search(removed)
    return removed[: first_call.start()] if first_call else removed


def params_in(removed: str) -> list[str]:
    """Names the removed markup gave values to in the call it closed (not in calls after it).

    Those values were meant as the call's other arguments. Each name is cut to 64
    characters of ``[\\w.-]``, since it is printed.
    """
    names: list[str] = []
    for m in _TOKEN.finditer(own_call_part(removed)):
        if m.group(1):
            continue
        name: str | None = m.group(2)
        if name == "parameter":
            attr = _PARAM_ATTR.fullmatch(m.group(3))
            name = attr.group(1) if attr else None
        elif name in ("invoke", "function_calls"):
            name = None
        name = _NAME_CHARS.sub("", name or "")[:64]
        if name and name not in names:
            names.append(name)
    return names


def clean_model_text(
    text: str | None, *, tool: str, field: str, params: Collection[str] = ()
) -> tuple[str | None, str]:
    """``strip_tool_markup`` for a write path, with a warning naming the tool when it strips.

    The warning carries sizes and the names of any calls dropped, never the text.
    """
    if not isinstance(text, str) or not text:
        return text, ""
    clean, removed = strip_tool_markup(text, field, params)
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
        note += (
            f" It held tool calls that did not run ({', '.join(calls)}); "
            "if you meant to run them, make them again as separate calls."
        )
    return note
