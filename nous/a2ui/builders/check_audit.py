"""The owner's weekly spot check of completion checks the agent wrote itself (``nous/audits/check_audit.py``).

Built by the server from rows the sampler read, NEVER from a model's call: this builder is not in ``TEMPLATES``, so
``push_surface`` cannot produce it, and the sampler pushes it without a dedup key, so no other push can replace it in
place. Each item shows what the subtask was asked to do, the check command the agent wrote for it, and the subtask's
result, labelled as the agent's own report. All three are model-authored: each reaches the card only through
``continuation.card_text`` (clipped, control characters escaped) inside one fenced block. Titles, labels and buttons
are fixed words. One Right and one Wrong button per item call ``audit.verdict``; the data model is
``{"items": {audit_id: "pending"}}`` and the handler patches an item to its verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from nous.audits import AUDIT_VERDICT_ACTION
from nous.brain import continuation

from ..dsl import BuiltSurface, Button, Column, Divider, Row, Surface, Text, event
from .heartbeat_findings import _escape_pointer
from .intention_cards import fence

KIND = "check_audit"
ORIGIN = "audit"
EXPIRES_IN = timedelta(days=7)
# What the card shows of each model-authored string, in UTF-16 units.
TASK_CHARS = 600
CHECK_CHARS = 300
RESULT_CHARS = 1500
RESULT_LABEL = "The agent's own report"
PENDING = "pending"
_BUTTONS = (("right", "Right", "primary"), ("wrong", "Wrong", "borderless"))


def check_audit(items: Sequence[Mapping[str, Any]]) -> BuiltSurface:
    """The card for ``items``, in order, each ``{"audit_id", "task", "check", "result"}`` (``audit_id`` is the
    ``check_audits`` row id as a string). Raises ``ValueError`` for an empty list: there is nothing to ask."""
    if not items:
        raise ValueError("a check audit card needs at least one item")
    title = f"Spot check: {len(items)} finished task{'s' if len(items) != 1 else ''}"
    s = Surface(
        kind=KIND,
        origin=ORIGIN,
        title=title,
        priority=1,
        allowed_actions=[AUDIT_VERDICT_ACTION],
        expires_in=EXPIRES_IN,
    )
    s.data({"items": {str(item["audit_id"]): PENDING for item in items}})

    children: list[str] = ["header", "intro"]
    components: list[dict] = [
        Text("header", f"## {title}"),
        Text(
            "intro",
            "The agent marked each of these done, and a check it wrote for itself passed. Was the work really done "
            "as asked? Tap Right or Wrong. The boxes hold the agent's own text.",
            variant="caption",
        ),
    ]
    for i, item in enumerate(items):
        audit_id = str(item["audit_id"])
        base = f"a{i}"
        children.append(base)
        button_ids = [f"{base}_{verdict}" for verdict, _, _ in _BUTTONS]
        components += [
            Column(
                base,
                children=[
                    f"{base}_task_h",
                    f"{base}_task",
                    f"{base}_check_h",
                    f"{base}_check",
                    f"{base}_result_h",
                    f"{base}_result",
                    f"{base}_status",
                    f"{base}_acts",
                ],
            ),
            Text(f"{base}_task_h", f"**Task {i + 1} of {len(items)}**"),
            Text(f"{base}_task", fence(continuation.card_text(str(item.get("task") or ""), TASK_CHARS))),
            Text(f"{base}_check_h", "Its completion check", variant="caption"),
            Text(f"{base}_check", fence(continuation.card_text(str(item.get("check") or ""), CHECK_CHARS))),
            Text(f"{base}_result_h", RESULT_LABEL, variant="caption"),
            Text(f"{base}_result", fence(continuation.card_text(str(item.get("result") or ""), RESULT_CHARS))),
            Text(f"{base}_status", {"path": f"/items/{_escape_pointer(audit_id)}"}, variant="caption"),
            Row(f"{base}_acts", children=button_ids),
        ]
        for (verdict, label, variant), bid in zip(_BUTTONS, button_ids, strict=True):
            components += [
                Button(
                    bid,
                    child=f"{bid}_l",
                    variant=variant,
                    action=event(AUDIT_VERDICT_ACTION, {"auditId": audit_id, "verdict": verdict}),
                ),
                Text(f"{bid}_l", label),
            ]
        if i < len(items) - 1:
            children.append(f"{base}_div")
            components.append(Divider(f"{base}_div"))

    s.add(Column("root", children=children, align="stretch"), *components)
    return s.build()
