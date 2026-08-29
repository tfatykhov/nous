"""F092: pre-execution escalation surface (spec Appendix A).

The blocking path: something irreversible wants to happen and renders as a
recommendation-plus-options card BEFORE execution. Options become one
Button each (the basic catalog requires literal ``variant`` enums, so a
List template over options — which the spec example uses — cannot vary the
recommended option's styling; per-option buttons can).

SCOPE (v1, deliberate): choosing an option records a durable, audited
decision and resolves the card — it does NOT invoke an executor. The spec's
worked example resumes a blocked DAG node; that callback plumbing ships
with the escalation integration, and the pushing agent is responsible for
holding the operation and reading the recorded choice until then. The
push_surface tool description says the same to the model.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..dsl import ApprovalPanel, Button, Column, Surface, Text, event
from ._shared import _validated_trace_id


def approval_gate(params: dict[str, Any]) -> Any:
    title = params["title"]
    options = params["options"]
    if not options:
        raise ValueError("approval_gate requires at least one option")
    trace_id = _validated_trace_id(params.get("trace_id"))
    recommendation = params.get("recommendation") or options[0]["id"]

    s = Surface(
        kind="approval_gate",
        origin="escalation",
        title=title,
        priority=2,
        trace_id=trace_id,
        allowed_actions=["approval.choose", "approval.defer"],
        expires_in=timedelta(hours=float(params.get("expires_hours", 24))),
    )
    s.data(
        {
            "summary": params.get("summary", ""),
            "risk": params.get("risk", ""),
            "recommendation": recommendation,
            # Authoritative record of what was offered — the approval.choose
            # handler validates the submitted optionId against THIS (the
            # buttons carry literal ids, but the server must not trust the
            # client's copy of anything).
            "options": [{"id": o["id"], "label": o["label"]} for o in options],
        }
    )

    option_ids: list[str] = []
    components: list[dict] = []
    for i, opt in enumerate(options):
        btn_id = f"opt_{i}"
        label_id = f"opt_label_{i}"
        variant = "primary" if opt["id"] == recommendation else "default"
        label = opt["label"] + (" (recommended)" if opt["id"] == recommendation else "")
        components.append(
            Button(
                btn_id,
                child=label_id,
                variant=variant,
                action=event(
                    "approval.choose",
                    {"optionId": opt["id"], **({"traceId": trace_id} if trace_id else {})},
                ),
            )
        )
        components.append(Text(label_id, label))
        option_ids.append(btn_id)

    s.add(
        Column("root", children=["panel", *option_ids, "defer"], align="stretch"),
        ApprovalPanel(
            "panel",
            title=title,
            summary={"path": "/summary"},
            risk={"path": "/risk"},
            recommendation={"path": "/recommendation"},
        ),
        *components,
        Button(
            "defer",
            child="defer_label",
            variant="borderless",
            action=event("approval.defer", {"traceId": trace_id} if trace_id else {}),
        ),
        Text("defer_label", "Ask me later"),
    )
    return s.build()
