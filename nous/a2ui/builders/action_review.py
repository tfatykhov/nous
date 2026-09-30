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


def _is_compensable_tool(name: str) -> bool:
    from nous.api.tool_classes import TOOL_CLASSES

    tc = TOOL_CLASSES.get(name)
    return tc is not None and tc.compensable


def action_review(params: dict[str, Any]) -> Any:
    title = params["title"]
    trace_id = _validated_trace_id(params.get("trace_id"))
    compensation = params.get("compensation") or {"revertible": False, "handler": None, "note": ""}
    # codex P2 on #652: compensation cards have trace_id=ledger_entry_id, not a
    # decision ID. course_correct and make_rule call brain.review(trace_id) which
    # would fail for a ledger UUID, so hide those verbs for compensation cards.
    is_compensation_card = bool(params.get("compensation_card"))

    allowed = ["review.acknowledge"]
    if not is_compensation_card:
        allowed.extend(["review.course_correct", "review.make_rule"])

    # Phase 2.8: offer Revert only for a block the server derived
    # (a2ui.tools._server_compensation, from the snapshot store + registry):
    # revertible must be literally True, the handler a tool declared
    # compensable, and the card linked to a ledger row. A caller-invented
    # handler string never yields the button.
    handler = compensation.get("handler")
    revertible = (
        compensation.get("revertible") is True
        and isinstance(handler, str)
        and _is_compensable_tool(handler)
        and bool(trace_id)
    )
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
    verbs: list[str] = ["ack"]
    components: list[dict] = [
        Button("ack", child="ack_l", variant="primary", action=event("review.acknowledge", ctx)),
        Text("ack_l", "Fine"),
    ]
    # codex P2 on #652: skip course_correct/make_rule for compensation cards
    if not is_compensation_card:
        verbs.extend(["correct", "rule"])
        components.extend(
            [
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
        )

    if revertible:
        verbs.append("revert")
        components.extend(
            [
                Button(
                    "revert",
                    child="revert_l",
                    variant="borderless",
                    action=event("review.revert", ctx),
                ),
                Text("revert_l", "Undo this action"),
            ]
        )

    # codex P2 on #652: only show correction field if course_correct is available
    root_children = ["card"]
    if not is_compensation_card:
        root_children.append("correction_field")
    root_children.append("acts")

    s.add(
        Column("root", children=root_children, align="stretch"),
        ActionReviewCard(
            "card",
            title=title,
            did={"path": "/did"},
            why={"path": "/why"},
            cost={"path": "/cost"},
            compensation={"path": "/compensation"},
        ),
    )
    if not is_compensation_card:
        s.add(
            TextField(
                "correction_field",
                label="Correction (optional — sent with 'Wrong call')",
                value={"path": "/correction"},
                variant="longText",
            ),
        )
    s.add(
        Row("acts", children=verbs, justify="spaceBetween"),
        *components,
    )
    return s.build()
