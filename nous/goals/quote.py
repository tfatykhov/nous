"""F114: goal text a session reads as data. Each journal entry and each plan step sits in a tag of its own, and
the text inside cannot open or close one: every ``journal_entry``, ``plan_step`` and ``result_message`` delimiter in
it is escaped, in any case and with any spacing. Attribute values keep only letters, digits and ``_.:-``, so a
stored value cannot leave its quotes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from nous.heart.result_inbox import neutralize_delimiters

# Any case and any whitespace: the model reads `</ JOURNAL_ENTRY >` as a delimiter just as readily as the exact form.
_TAGS = re.compile(r"<(\s*/?\s*(?:journal_entry|plan_step))", re.IGNORECASE)
_ATTR_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_ATTR_MAX = 40
STEP_TEXT_MAX = 300
STEP_EVIDENCE_MAX = 3
STEP_EVIDENCE_REF_MAX = 300


def neutralize(text: str) -> str:
    """``text`` with every opening or closing ``journal_entry``, ``plan_step`` and ``result_message`` escaped."""
    return _TAGS.sub(r"&lt;\1", neutralize_delimiters(text))


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
