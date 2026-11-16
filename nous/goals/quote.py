"""F114: goal text a session reads as data. Each journal entry, each plan step and each file name the work chose
sits in a tag of its own, and the text inside cannot open or close one: every ``journal_entry``, ``plan_step``,
``file_ref`` and ``result_message`` delimiter in it is escaped, in any case and with any spacing, behind a fullwidth
or small angle bracket or slash too. Invisible format characters (zero-width spaces and joiners, direction marks) and
control characters other than newline and tab are removed first, so none can hide a delimiter. Attribute values keep
only letters, digits and ``_.:-``, so a stored value cannot leave its quotes."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from nous.heart.result_inbox import neutralize_delimiters

# Any case and any whitespace: the model reads `</ JOURNAL_ENTRY >` as a delimiter just as readily as the exact form,
# and `\uff1c/journal_entry\uff1e` (fullwidth) or `\ufe64/plan_step\ufe65` (small form) as well.
_OPENERS = "<\uff1c\ufe64"
_SLASHES = "/\uff0f"
_TAGS = re.compile(
    rf"[{_OPENERS}](\s*[{_SLASHES}]?\s*(?:journal_entry|plan_step|file_ref|result_message))", re.IGNORECASE
)
_KEPT_CONTROLS = frozenset("\n\t")
_ATTR_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_ATTR_MAX = 40
STEP_TEXT_MAX = 300
STEP_EVIDENCE_MAX = 3
STEP_EVIDENCE_REF_MAX = 300


def _visible(text: str) -> str:
    """``text`` without format characters (Unicode category Cf, which a zero-width joiner in an emoji sequence also
    is) or control characters (Cc) other than newline and tab."""
    return "".join(ch for ch in text if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in ("Cc", "Cf"))


def neutralize(text: str) -> str:
    """``text``, its invisible and control characters removed, with every opening or closing ``journal_entry``,
    ``plan_step``, ``file_ref`` and ``result_message`` escaped (the bracket becomes ``&lt;``)."""
    return _TAGS.sub(r"&lt;\1", neutralize_delimiters(_visible(text)))


def _attr(value: Any) -> str:
    return _ATTR_UNSAFE.sub("", str(value))[:_ATTR_MAX] or "-"


def quote_entry(kind: str, actor: str, session_n: int | None, text: str) -> str:
    """One journal row as data: ``<journal_entry kind=".." actor=".." session="..">`` around its escaped text."""
    session = "-" if session_n is None else session_n
    return (
        f'<journal_entry kind="{_attr(kind)}" actor="{_attr(actor)}" session="{_attr(session)}">\n'
        f"{neutralize(str(text))}\n</journal_entry>"
    )


def quote_step(step: Mapping[str, Any]) -> str:
    """One plan step as data: ``<plan_step id=".." status="..">`` around its escaped text (at most 300 characters)
    and its evidence refs (at most three)."""
    body = neutralize(str(step.get("text", ""))[:STEP_TEXT_MAX])
    evidence = [str(ref)[:STEP_EVIDENCE_REF_MAX] for ref in list(step.get("evidence") or [])[:STEP_EVIDENCE_MAX]]
    if evidence:
        body += "\nEvidence: " + ", ".join(neutralize(ref) for ref in evidence)
    step_id, status = _attr(step.get("id", "")), _attr(step.get("status", ""))
    return f'<plan_step id="{step_id}" status="{status}">\n{body}\n</plan_step>'


def quote_ref(ref: str) -> str:
    """One evidence ref whose name the work chose (a file's path), as data: ``<file_ref>`` around its escaped text
    (at most 300 characters)."""
    return f"<file_ref>\n{neutralize(str(ref)[:STEP_EVIDENCE_REF_MAX])}\n</file_ref>"
