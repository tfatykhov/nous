"""Tool-call markup that leaked into model-written text, and its removal.

A model writing a tool call can end a string parameter with markup instead of ending
the value, for instance ``</content>``, and go on with the next parameters as markup:
``<parameter name="category">technical``, ``<category>technical</category>``,
``</invoke>``, sometimes a whole further call. The API then keeps all of it inside the
string, and a write tool stores it as part of a fact or a decision.

``strip_tool_markup`` removes that spill-over and nothing else. It cuts at the first tag,
outside code, that starts it:

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
  ``<parameter name=…>`` that starts its line and is left open to the end of the text
  (the shape ``nous.utils.leaked_markup_start`` finds for the dispatcher).

Kept, as likely quoted: a run of complete elements with nothing after it
(``<parameter name="x">v</parameter>``); an open ``<parameter name=…>`` after other words
on its line (prose about the format); an open plain ``<x>`` of a tool parameter at the
end with no tool tag after it (``f.\\n<category>t</category>\\n<subject>s``). Residual: an
example of the format on a line of its own (``Like this:\\n<parameter name="content">``)
is the leak shape itself and is cut.

Code is kept: fenced blocks (a line of up to 3 spaces, then 3 or more backticks or
tildes, closed by a line of the same character at least as long, or by the end of the
text) and, outside them, inline code spans (a run of N backticks to the next run of
exactly N), both as CommonMark has them. It also removes a ``<parameter name="...">``
that opens the text.

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
# An opening that carries a value: <parameter name="x"> or <x>.
_VALUE_OPEN = re.compile(rf"<{_NS}(?:parameter\s+name=\"([^\"<>]*)\"|([A-Za-z_][\w-]*+))\s*>")
_BACKTICKS = re.compile(r"`+")
_FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})([^\n]*)")
_FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t\r]*")
_SPACE = re.compile(r"\s*")
_INDENT = re.compile(r"[^\S\n]*")
_TOOL_TAGS = frozenset({"parameter", "invoke", "function_calls"})
_NAME_CHARS = re.compile(r"[^\w.-]")

# Token kinds.
_CLOSE_TOOL, _CLOSE, _CALL_OPEN, _PARAM, _PARAM_TAG, _TAG, _OTHER = range(7)
_END, _PROSE = -1, -2


class Cut(str):
    """The markup ``strip_tool_markup`` removed, as one string: a leading
    ``<invoke …><parameter …>`` (``lead``, the call that ran) and the spill-over after the
    value (``tail``). ``calls_in``, ``params_in`` and ``leaked_values`` read the tail only."""

    lead: str
    tail: str

    def __new__(cls, lead: str, tail: str) -> Cut:
        cut = super().__new__(cls, lead + tail)
        cut.lead, cut.tail = lead, tail
        return cut


def _tail(removed: str) -> str:
    return removed.tail if isinstance(removed, Cut) else removed


def _fences(text: str) -> list[tuple[int, int]]:
    """CommonMark fenced code blocks, line by line in one pass; an unclosed one runs to the end."""
    spans: list[tuple[int, int]] = []
    opened_at, char, length = -1, "", 0
    pos = 0
    while pos <= len(text):
        eol = text.find("\n", pos)
        line_end = len(text) if eol == -1 else eol
        if opened_at < 0:
            m = _FENCE_OPEN.match(text, pos, line_end)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):  # a backtick info string has none
                opened_at, char, length = pos, m.group(1)[0], len(m.group(1))
        else:
            m = _FENCE_CLOSE.match(text, pos, line_end)
            if m and m.end() == line_end and m.group(1)[0] == char and len(m.group(1)) >= length:
                spans.append((opened_at, line_end))
                opened_at = -1
        if eol == -1:
            break
        pos = eol + 1
    if opened_at >= 0:
        spans.append((opened_at, len(text)))
    return spans


def _inline_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """CommonMark code spans in ``text[start:end]``: a run of N backticks to the next run of exactly N."""
    runs = [(m.start(), m.end()) for m in _BACKTICKS.finditer(text, start, end)]
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


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Fenced blocks, and the inline code spans between them, in text order. Linear."""
    spans: list[tuple[int, int]] = []
    start = 0
    for fence_start, fence_end in _fences(text):
        spans += _inline_spans(text, start, fence_start)
        spans.append((fence_start, fence_end))
        start = fence_end
    return spans + _inline_spans(text, start, len(text))


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
    """The tags of one text, with what follows each, for one forward pass.

    Everything a candidate asks is precomputed or memoised, so no question rescans the
    text: the token after each token (each gap read once), the closing tags per name (for a
    binary search), the line starts and the run results.
    """

    def __init__(self, text: str, params: Collection[str]) -> None:
        self.text = text
        self.tokens = list(_TOKEN.finditer(text))
        self.kinds = [_kind(m, params) for m in self.tokens]
        self.names = [m.group(2) for m in self.tokens]
        self._runs: dict[int, bool] = {}
        self._closers: dict[str, list[int]] = {}
        for i, kind in enumerate(self.kinds):
            if kind in (_CLOSE, _CLOSE_TOOL):
                self._closers.setdefault(self.names[i], []).append(i)
        # The token after each one when only whitespace is between, else _END or _PROSE.
        self._next: list[int] = []
        for i, m in enumerate(self.tokens):
            space_end = _SPACE.match(text, m.end()).end()  # a run of whitespace stops at the next '<'
            if i + 1 < len(self.tokens):
                self._next.append(i + 1 if space_end >= self.tokens[i + 1].start() else _PROSE)
            else:
                self._next.append(_END if space_end == len(text) else _PROSE)
        self._last_lt = text.rfind("<")
        self._newlines: list[int] | None = None

    def _closer_after(self, j: int, name: str) -> int | None:
        indices = self._closers.get(name, [])
        k = bisect.bisect_right(indices, j)
        return indices[k] if k < len(indices) else None

    def next_of(self, i: int) -> int:
        """The token after token ``i`` when only whitespace is between, else _END or _PROSE."""
        return self._next[i]

    def starts_line(self, i: int) -> bool:
        """Whether only spaces or tabs are between the line's start and token ``i``."""
        if self._newlines is None:
            self._newlines = [m.start() for m in re.finditer("\n", self.text)]
        start = self.tokens[i].start()
        k = bisect.bisect_left(self._newlines, start)
        line_start = self._newlines[k - 1] + 1 if k else 0
        return _INDENT.match(self.text, line_start).end() == start

    def run_ends_in_markup(self, j: int) -> bool:
        """Whether the run of elements from token ``j`` ends in a tool closing tag, a new call,
        or a ``<parameter name=…>`` that starts its line and is left open to the end.
        Memoised per element, so every token is read once across all the runs a text holds."""
        visited: list[int] = []
        result = False
        while True:
            if j in self._runs:
                result = self._runs[j]
                break
            visited.append(j)
            c = self._closer_after(j, "parameter" if self.kinds[j] == _PARAM else self.names[j])
            if c is None:
                # Left open to the end with no '<' in its value, as leaked_markup_start requires,
                # and on a line of its own: after other words it is prose about the format.
                result = self.kinds[j] == _PARAM and self._last_lt < self.tokens[j].end() and self.starts_line(j)
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
    ``category`` is one of them. The markup removed is a ``Cut``, which keeps a leading
    ``<invoke …><parameter …>`` (the call that ran) apart from the spill-over. Returns
    ``(text, "")`` when nothing leaked.
    """
    if not isinstance(text, str) or "<" not in text:
        return text, ""
    lead = _LEADING.match(text)
    lead_text = lead.group(0) if lead else ""
    text = text[len(lead_text) :]
    start = _tail_start(text, field, params)
    tail_text = text[start:] if start is not None else ""
    if start is not None:
        text = text[:start]
    if not (lead_text or tail_text):
        return text, ""
    return text.strip(), Cut(lead_text, tail_text)


def calls_in(removed: str) -> list[str]:
    """Names of the tool calls the removed spill-over held: calls that never ran."""
    return _CALL.findall(_tail(removed))


def own_call_part(removed: str) -> str:
    """The spill-over up to the first new call: the part that belongs to the call it closed."""
    tail = _tail(removed)
    first_call = _CALL.search(tail)
    return tail[: first_call.start()] if first_call else tail


def params_in(removed: str) -> list[str]:
    """Names the removed spill-over gave values to in the call it closed (not in calls after it).

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


def leaked_values(removed: str) -> list[tuple[str, str | None]]:
    """The ``(name, value)`` pairs the spill-over gave the call it closed, in order.

    A value runs to its matching closer (``</parameter>``, or ``</x>`` for ``<x>``), so a
    literal '<' inside it is kept and a tag inside it starts no pair of its own. A value
    with no closer is the rest of the cut when nothing tag-shaped follows it, else ``None``:
    where it ends cannot be told, so it is not recovered.
    """
    head = own_call_part(removed)
    pairs: list[tuple[str, str | None]] = []
    pos = 0
    while (m := _VALUE_OPEN.search(head, pos)) is not None:
        name = m.group(1) if m.group(1) is not None else m.group(2)
        if m.group(1) is None and name in _TOOL_TAGS:
            pos = m.end()
            continue
        closer_name = "parameter" if m.group(1) is not None else re.escape(name)
        closer = re.compile(rf"</{_NS}{closer_name}\s*>").search(head, m.end())
        if closer is not None:
            pairs.append((name, head[m.end() : closer.start()]))
            pos = closer.end()
            continue
        rest = head[m.end() :]
        pairs.append((name, None if _TOKEN.search(rest) else rest))
        break
    return pairs


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
    if not any(removed):
        return ""
    note = "\nNote: tool-call markup at the end of the text was removed before storing it."
    calls = [call for cut in removed for call in calls_in(cut)]
    if calls:
        note += (
            f" It held tool calls that did not run ({', '.join(calls)}); "
            "if you meant to run them, make them again as separate calls."
        )
    return note
