"""F092 Phase 2: decision sweep surface.

Unreviewed decisions as DecisionCards with per-card outcome buttons. Each
click is one audited ``decision.resolve`` action (validated server-side
against the surface's own decision list + ReviewInput's outcome Literal);
the surface resolves itself once the last pending decision is reviewed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..dsl import Button, Column, ConfidenceMeter, DecisionCard, Divider, Row, Surface, Text, event

_OUTCOME_BUTTONS = [
    ("success", "Success", "primary"),
    ("partial", "Partial", None),
    ("failure", "Failure", None),
    ("noise", "Noise", "borderless"),
]


def decision_sweep(params: dict[str, Any]) -> Any:
    decisions = params.get("decisions", [])
    title = params.get("title") or f"Decision sweep ({len(decisions)} pending)"

    s = Surface(
        kind="decision_sweep",
        origin="sweep",
        title=title,
        priority=int(params.get("priority", 1)),
        allowed_actions=["decision.resolve"],
        expires_in=timedelta(days=float(params.get("expires_days", 7))),
    )
    s.data({"decisions": {str(d["id"]): "pending" for d in decisions}})

    children: list[str] = ["header"]
    components: list[dict] = [Text("header", f"## {title}")]
    for i, decision in enumerate(decisions):
        did = str(decision["id"])
        base = f"d{i}"
        children.append(base)
        button_ids = []
        buttons: list[dict] = []
        for outcome, label, variant in _OUTCOME_BUTTONS:
            bid = f"{base}_{outcome}"
            button_ids.append(bid)
            buttons.append(
                Button(
                    bid,
                    child=f"{bid}_l",
                    variant=variant,
                    action=event("decision.resolve", {"decisionId": did, "outcome": outcome}),
                )
            )
            buttons.append(Text(f"{bid}_l", label))
        components += [
            Column(base, children=[f"{base}_card", f"{base}_meter", f"{base}_status", f"{base}_acts"]),
            DecisionCard(
                f"{base}_card",
                decisionId=did,
                description=decision.get("description", ""),
                confidence=float(decision.get("confidence", 0.0)),
                stakes=str(decision.get("stakes", "")),
                category=str(decision.get("category", "")),
                outcome={"path": f"/decisions/{did}"},
            ),
            ConfidenceMeter(f"{base}_meter", value=float(decision.get("confidence", 0.0))),
            Text(f"{base}_status", {"path": f"/decisions/{did}"}, variant="caption"),
            Row(f"{base}_acts", children=button_ids),
            *buttons,
        ]
        if i < len(decisions) - 1:
            div = f"{base}_div"
            children.append(div)
            components.append(Divider(div))

    if not decisions:
        children.append("empty")
        components.append(Text("empty", "Nothing pending review."))

    s.add(Column("root", children=children, align="stretch"), *components)
    return s.build()
