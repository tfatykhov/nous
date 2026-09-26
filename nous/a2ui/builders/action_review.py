"""F092: advisory Action Review surface (spec Appendix A2, Q5-advisory).

Nous already acted; the card is a reviewable record. The verb set is
Acknowledge / Course-correct / Make-it-a-rule, plus Revert when the action
is declared compensable and a compensator is registered (harness Phase 2.8).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..dsl import ActionReviewCard, Button, Column, Row, Surface, Text, TextField, event
from ._shared import _validated_trace_id


def action_review(params: dict[str, Any]) -> Any:
    title = params["title"]
    trace_id = _validated_trace_id(params.get("trace_id"))
    compensation = params.get("compensation") or {"revertible": False, "handler": None, "note": ""}

    allowed = ["review.acknowledge", "review.course_correct", "review.make_rule"]

    # Phase 2.8: offer Revert when the action is compensable.
    revertible = compensation.get("revertible", False) and compensation.get("handler")
    if revertible:
        allowed.append("review.revert")

    s = Surface(
        kind="action_review",
        origin="escalation",
        title=title,
        priority=int(params.get("priority", 1)),
        trace_id=trace_id,
        allowed_actions=allowed,
        expires_in=timedelta(days=float(params.get("archive_days", 14))),
    )
    s.data(
        {
            "did": params.get("did", ""),
            "why": params.get("why", ""),
            "cost": params.get("cost", ""),
            "compensation": compensation,
            "correction": "",
        }
    )

    ctx = {"traceId": trace_id} if trace_id else {}
    verbs: list[str] = ["ack", "correct", "rule"]
    components: list[dict] = [
        Button("ack", child="ack_l", variant="primary", action=event("review.acknowledge", ctx)),
        Text("ack_l", "Fine"),
        Button(
            "correct",
            child="correct_l",
            action=event("review.course_correct", {**ctx, "correction": {"path": "/correction"}}),
        ),
        Text("correct_l", "Wrong call — noted below"),
        Button(
            "rule",
            child="rule_l",
            variant="borderless",
            action=event("review.make_rule", {**ctx, "correction": {"path": "/correction"}}),
        ),
        Text("rule_l", "Make this a standing rule"),
    ]

    if revertible:
        verbs.append("revert")
        components.extend([
            Button(
                "revert",
                child="revert_l",
                variant="borderless",
                action=event("review.revert", ctx),
            ),
            Text("revert_l", "Undo this action"),
        ])

    s.add(
        Column("root", children=["card", "correction_field", "acts"], align="stretch"),
        ActionReviewCard(
            "card",
            title=title,
            did={"path": "/did"},
            why={"path": "/why"},
            cost={"path": "/cost"},
            compensation={"path": "/compensation"},
        ),
        TextField(
            "correction_field",
            label="Correction (optional — sent with 'Wrong call')",
            value={"path": "/correction"},
            variant="longText",
        ),
        Row("acts", children=verbs, justify="spaceBetween"),
        *components,
    )
    return s.build()
