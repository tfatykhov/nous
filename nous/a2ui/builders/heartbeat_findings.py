"""F092: heartbeat findings triage surface.

Findings grouped as cards with acknowledge / resolve / dismiss buttons,
delegating to the existing finding lifecycle (F034.1). Near-zero backend
work by design — thin presentation over an API that already exists.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..dsl import Button, Card, Column, Divider, Row, Surface, Text, event


def heartbeat_findings(params: dict[str, Any]) -> Any:
    findings = params.get("findings", [])
    title = params.get("title") or f"Heartbeat findings ({len(findings)})"

    s = Surface(
        kind="heartbeat_findings",
        origin="heartbeat",
        title=title,
        priority=int(params.get("priority", 1)),
        allowed_actions=[
            "heartbeat.acknowledge",
            "heartbeat.resolve",
            "heartbeat.dismiss",
        ],
        expires_in=timedelta(hours=float(params.get("expires_hours", 72))),
    )
    s.data({"findings": {f["fingerprint"]: f.get("status", "open") for f in findings}})

    children: list[str] = ["header"]
    components: list[dict] = [Text("header", f"## {title}")]
    for i, finding in enumerate(findings):
        fp = finding["fingerprint"]
        card_id = f"f{i}"
        children.append(card_id)
        row = [
            Card(card_id, child=f"f{i}_col"),
            Column(f"f{i}_col", children=[f"f{i}_msg", f"f{i}_meta", f"f{i}_acts"]),
            Text(f"f{i}_msg", finding.get("message", "")),
            Text(
                f"f{i}_meta",
                f"{finding.get('urgency', 'normal')} · {finding.get('check', '')} · {fp[:12]}",
                variant="caption",
            ),
            Row(f"f{i}_acts", children=[f"f{i}_ack", f"f{i}_res", f"f{i}_dis"]),
            Button(
                f"f{i}_ack",
                child=f"f{i}_ack_l",
                action=event("heartbeat.acknowledge", {"fingerprint": fp}),
            ),
            Text(f"f{i}_ack_l", "Acknowledge"),
            Button(
                f"f{i}_res",
                child=f"f{i}_res_l",
                variant="primary",
                action=event("heartbeat.resolve", {"fingerprint": fp}),
            ),
            Text(f"f{i}_res_l", "Resolve"),
            Button(
                f"f{i}_dis",
                child=f"f{i}_dis_l",
                variant="borderless",
                action=event("heartbeat.dismiss", {"fingerprint": fp}),
            ),
            Text(f"f{i}_dis_l", "Dismiss"),
        ]
        components.extend(row)
        if i < len(findings) - 1:
            div = f"f{i}_div"
            children.append(div)
            components.append(Divider(div))

    if not findings:
        children.append("empty")
        components.append(Text("empty", "No open findings."))

    s.add(Column("root", children=children, align="stretch"), *components)
    return s.build()
