"""The tool-notes guard card on the companion (F108 Phase 3).

A repeated tool pattern that a guard caused (a context-policy refusal, the lineage-shell check, the email allowlist)
never reaches the model and never becomes a note. The owner gets this card. It is built by the server from
``Pattern`` values only, with fixed wording: the tool, the guard label, the hits and calls, and the normalised error
text, redacted. It holds no model output and no advice on the guard: the owner decides. There is no button. The
sleep review pushes it under ``DEDUP_KEY`` with ``notify=False`` and ``telegram_ping=False``, so it replaces itself in
place and pages nobody.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from nous.cognitive.ledger_store import redact_output
from nous.observability.tool_patterns import WINDOW_DAYS, Pattern

from ..dsl import BuiltSurface, Column, Divider, Surface, Text

KIND = "tool_notes_guard"
ORIGIN = "heartbeat"
DEDUP_KEY = "tool_notes_guard"
EXPIRES_IN = timedelta(days=14)
MAX_PER_CARD = 10  # patterns on one card; the rest are reported on a later sleep


def _what(p: Pattern) -> str:
    """The normalised error of an ``error_repeat`` pattern, else the redacted example, in one line."""
    return redact_output(" ".join((p.key or p.example).split()))


def tool_notes_guard_card(findings: Sequence[Pattern]) -> BuiltSurface:
    """The card for guard-caused ``findings``. Raises ``ValueError`` when the list is empty."""
    findings = list(findings)[:MAX_PER_CARD]  # the caller sends the window's patterns, most hits first
    if not findings:
        raise ValueError("a tool-notes guard card needs at least one pattern")
    title = f"Tool guards: {len(findings)} repeated pattern{'s' if len(findings) != 1 else ''}"
    s = Surface(kind=KIND, origin=ORIGIN, title=title, priority=0, expires_in=EXPIRES_IN)
    s.data({"patterns": [p.id for p in findings]})
    children: list[str] = ["header", "intro"]
    components: list[dict] = [
        Text("header", f"## {title}"),
        Text(
            "intro",
            f"A guard stopped these tool calls again and again in the last {WINDOW_DAYS} days. No note was written "
            "for them and nothing was changed. The owner decides.",
            variant="caption",
        ),
    ]
    for i, p in enumerate(findings):
        base = f"p{i}"
        children.append(base)
        components += [
            Column(base, children=[f"{base}_h", f"{base}_d", f"{base}_e"]),
            Text(f"{base}_h", f"**{p.tool_name}**"),
            Text(f"{base}_d", f"Guard: {p.guard}. {p.hits} of {p.calls} calls.", variant="caption"),
            Text(f"{base}_e", f"{'Error' if p.key else 'Example'}: {_what(p)}"),
        ]
        if i < len(findings) - 1:
            children.append(f"{base}_div")
            components.append(Divider(f"{base}_div"))
    s.add(Column("root", children=children, align="stretch"), *components)
    return s.build()
