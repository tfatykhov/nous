"""F092 Phase 2: DAG monitor surface.

Snapshot of a DAG's nodes/edges as a wave-layout graph, with retry buttons
for failed nodes and a cancel action. Live updating happens by re-pushing
with the same ``dedup_key`` (update-in-place); orchestrator-tick push is
escalation-integration work, deliberately not wired here.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..dsl import Button, Column, DagGraph, Row, Surface, Text, event


def dag_monitor(params: dict[str, Any]) -> Any:
    dag_id = str(params["dag_id"])
    name = str(params.get("name") or dag_id[:8])
    status = str(params.get("status", "running"))
    nodes = params.get("nodes", [])
    edges = params.get("edges", [])
    title = params.get("title") or f"DAG {name} — {status}"

    failed = [n["name"] for n in nodes if n.get("status") == "failed"]
    allowed = ["dag.cancel"] + (["dag.retry"] if failed else [])

    s = Surface(
        kind="dag_monitor",
        origin="dag",
        title=title,
        priority=int(params.get("priority", 1 if failed else 0)),
        allowed_actions=allowed,
        expires_in=timedelta(hours=float(params.get("expires_hours", 48))),
    )
    s.data(
        {
            "dag_id": dag_id,
            "banner": "",
            "nodes": [
                {"name": n["name"], "status": n.get("status", "pending"), "node_type": n.get("node_type", "")}
                for n in nodes
            ],
            "edges": [{"from": e["from"], "to": e["to"]} for e in edges],
        }
    )

    children = ["heading", "banner", "graph"]
    components: list[dict] = [
        Text("heading", f"## {title}"),
        Text("banner", {"path": "/banner"}, variant="caption"),
        DagGraph("graph", nodes={"path": "/nodes"}, edges={"path": "/edges"}),
    ]
    retry_ids = []
    for i, node_name in enumerate(failed):
        rid = f"retry_{i}"
        retry_ids.append(rid)
        components += [
            Button(rid, child=f"{rid}_l", variant="primary", action=event("dag.retry", {"node": node_name})),
            Text(f"{rid}_l", f"Retry {node_name}"),
        ]
    if retry_ids:
        children.append("retries")
        components.append(Row("retries", children=retry_ids))
    children.append("cancel")
    components += [
        Button("cancel", child="cancel_l", variant="borderless", action=event("dag.cancel", {})),
        Text("cancel_l", "Cancel DAG"),
    ]

    s.add(Column("root", children=children, align="stretch"), *components)
    return s.build()
