"""F092 Phase 2: memory graph explorer surface.

A MemoryGraph seeded with a focus node and its first neighborhood; tapping
a node in the renderer calls the agent-side ``expandGraphNode`` function
(POST /a2ui/call) and merges the returned nodes/edges into the local data
model. This is the surface that justified targeting protocol v1.0.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..dsl import Column, MemoryGraph, Surface, Text


def memory_graph(params: dict[str, Any]) -> Any:
    focus_id = str(params["node_id"])
    node_type = str(params.get("node_type", "fact"))
    label = str(params.get("label") or focus_id[:8])
    nodes = params.get("nodes") or [{"id": focus_id, "type": node_type, "label": label}]
    edges = params.get("edges") or []
    title = params.get("title") or f"Memory graph: {label[:60]}"

    s = Surface(
        kind="memory_graph",
        origin="manual",
        title=title,
        priority=0,
        # Read-only surface: expansion goes through /a2ui/call, not actions.
        allowed_actions=[],
        expires_in=timedelta(hours=float(params.get("expires_hours", 24))),
    )
    s.data({"nodes": nodes, "edges": edges, "focus": focus_id})
    s.add(
        Column("root", children=["heading", "hint", "graph"], align="stretch"),
        Text("heading", f"## {title}"),
        Text("hint", "Tap a node to expand its neighborhood.", variant="caption"),
        MemoryGraph("graph", nodes={"path": "/nodes"}, edges={"path": "/edges"}, focusNodeId=focus_id),
    )
    return s.build()
