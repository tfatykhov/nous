"""F099 Phase 3a: the owner's proposal and question cards (contract sections 4.3 to 4.5).

Built by the server from rows the publisher read, NEVER from a model's call: these builders are not in
``TEMPLATES``, so ``push_surface`` cannot produce them, and their dedup keys are reserved
(``nous/a2ui/intention_keys.py``). The one rule of the text: everything a model wrote (the intent, the rationale, the
call's arguments, the arrival's note, the question) reaches a card ONLY through ``continuation.card_text`` and sits
inside ONE fenced block held by a data-model field the components bind to. Components, titles, button labels and
``allowed_actions`` are fixed words, so they are byte-identical whatever the text says. The owner approves what is
shown, so the rationale and the call are refused (the card is not built, the Telegram message still carries the
proposal) rather than clipped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from nous.brain import continuation

from ..dsl import BuiltSurface, Button, Column, Row, Surface, Text, TextField, event

# What a card shows of each model-authored string, in UTF-16 units (contract section 4.3).
INTENT_CHARS = 300
RATIONALE_CHARS = 2000
ARGUMENTS_CHARS = 4000
NOTE_CHARS = 300
QUESTION_CHARS = 4000
STATUS_PENDING = "Pending: waiting for you"
STATUS_QUESTION = "Waiting for your answer"
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,100}")  # a registered tool name; anything else is not shown as one


def fence(shown: str) -> str:
    """``shown`` (already ``card_text``) as ONE fenced block: the single string a Text component binds to."""
    return f"```\n{shown}\n```"


class CardNotBuilt(ValueError):
    """The card is not built, for a ``reason`` that is a fixed word (``tool_name``, ``rationale``, ``call``,
    ``deadline``): the publisher logs it, never the text. The Telegram message still carries the proposal."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _whole(text: str, limit: int, what: str) -> str:
    if continuation.utf16_units(continuation.render_text(text)) > limit:
        raise CardNotBuilt(what, f"the {what} does not fit the card whole; it is not clipped")
    return continuation.card_text(text, limit)


def _until(deadline: datetime, now: datetime) -> timedelta:
    remaining = deadline - now
    if remaining <= timedelta(0):
        raise CardNotBuilt("deadline", "the deadline has passed; no card is built")
    return remaining


def intention_proposal(
    *,
    proposal_id: UUID,
    root_id: UUID,
    arrival_id: UUID | None,
    tool: str,
    deadline: datetime,
    intent: str,
    rationale: str,
    arguments: Mapping[str, Any],
    note: str | None,
    trace_id: str | None = None,
    now: datetime | None = None,
) -> BuiltSurface:
    """The proposal card: what the owner is asked to approve, exactly as it will run, with Approve and Reject.
    ``arguments`` are the STORED arguments (``render_arguments`` shows the stored key order). Raises ``ValueError``
    for a tool name that is not a plain identifier, a rationale or call that would not fit whole, or a deadline that
    has passed (the publisher catches it: the card is skipped, Telegram carries the proposal)."""
    if not _TOOL_NAME.fullmatch(tool):
        raise CardNotBuilt("tool_name", "the tool name is not a plain identifier; no card is built")
    now = now or datetime.now(UTC)
    remaining = _until(deadline, now)
    short = continuation.short_id(proposal_id)
    shown_note = continuation.card_text(" ".join((note or "").split()), NOTE_CHARS)
    s = Surface(
        kind="intention_proposal",
        origin="continuation",
        title=f"Approval needed {short}",
        priority=2,
        trace_id=trace_id,
        allowed_actions=["intention.approve", "intention.reject"],
        expires_in=remaining,
    )
    s.data(
        {
            "proposal_id": str(proposal_id),
            "short_id": short,
            "root_id": str(root_id),
            "arrival_id": str(arrival_id) if arrival_id else "",
            "tool": tool,
            "deadline": deadline.astimezone(UTC).isoformat(),
            "intent_text": fence(continuation.card_text(intent, INTENT_CHARS)),
            "rationale_text": fence(_whole(rationale, RATIONALE_CHARS, "rationale")),
            "arguments_text": fence(_whole(continuation.render_arguments(arguments), ARGUMENTS_CHARS, "call")),
            "note_text": fence(shown_note) if shown_note else "",
            "status": STATUS_PENDING,
        }
    )
    body = ["header", "tool", "why_h", "why", "call_h", "call"]
    if shown_note:
        body += ["note_h", "note"]
    body += ["intent_h", "intent", "deadline", "status", "acts"]
    s.add(
        Column("root", children=body, align="stretch"),
        Text("header", f"## Approval needed {short}"),
        Text("tool", f"Tool: `{tool}`", variant="caption"),
        Text("why_h", "**Why**"),
        Text("why", {"path": "/rationale_text"}),
        Text("call_h", "**Call, exactly as it will run**"),
        Text("call", {"path": "/arguments_text"}),
    )
    if shown_note:
        s.add(Text("note_h", "**Nous says**"), Text("note", {"path": "/note_text"}))
    s.add(
        Text("intent_h", "For:", variant="caption"),
        Text("intent", {"path": "/intent_text"}),
        Text(
            "deadline",
            f"Expires {deadline.astimezone(UTC):%Y-%m-%d %H:%M} UTC. If you do nothing, it is rejected.",
            variant="caption",
        ),
        Text("status", {"path": "/status"}, variant="caption"),
        Row("acts", children=["approve", "reject"], justify="spaceBetween"),
        Button(
            "approve",
            child="approve_l",
            variant="primary",
            action=event("intention.approve", {"proposalId": str(proposal_id)}),
        ),
        Text("approve_l", "Approve"),
        Button(
            "reject",
            child="reject_l",
            variant="borderless",
            action=event("intention.reject", {"proposalId": str(proposal_id)}),
        ),
        Text("reject_l", "Reject"),
    )
    return s.build()


def intention_question(
    *,
    question_id: UUID,
    root_id: UUID,
    arrival_id: UUID | None,
    question: str,
    intent: str,
    deadline: datetime,
    trace_id: str | None = None,
    now: datetime | None = None,
) -> BuiltSurface:
    """The question card: the question, a free-text field bound to ``/answer`` (which MUST pre-exist as a string in
    the data model: the router checks a submitted model against the server copy), and Send. ``deadline`` is the end
    of the answer window the store enforces."""
    now = now or datetime.now(UTC)
    remaining = _until(deadline, now)
    s = Surface(
        kind="intention_question",
        origin="continuation",
        title="Question from Nous",
        priority=2,
        trace_id=trace_id,
        allowed_actions=["intention.answer"],
        expires_in=remaining,
    )
    s.data(
        {
            "question_id": str(question_id),
            "root_id": str(root_id),
            "arrival_id": str(arrival_id) if arrival_id else "",
            "intent_text": fence(continuation.card_text(intent, INTENT_CHARS)),
            "question_text": fence(continuation.card_text(question, QUESTION_CHARS)),
            "deadline": deadline.astimezone(UTC).isoformat(),
            "answer": "",
            "status": STATUS_QUESTION,
        }
    )
    s.add(
        Column(
            "root",
            children=["header", "q", "intent_h", "intent", "field", "send", "status", "deadline"],
            align="stretch",
        ),
        Text("header", "## Question from Nous"),
        Text("q", {"path": "/question_text"}),
        Text("intent_h", "For:", variant="caption"),
        Text("intent", {"path": "/intent_text"}),
        TextField("field", label="Your answer", value={"path": "/answer"}, variant="longText"),
        Button(
            "send",
            child="send_l",
            variant="primary",
            action=event("intention.answer", {"questionId": str(question_id), "text": {"path": "/answer"}}),
        ),
        Text("send_l", "Send answer"),
        Text("status", {"path": "/status"}, variant="caption"),
        Text(
            "deadline",
            f"You can answer until {deadline.astimezone(UTC):%Y-%m-%d %H:%M} UTC.",
            variant="caption",
        ),
    )
    return s.build()
