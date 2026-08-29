"""F092 diag: push demo surfaces into a running Nous instance's DB.

Loads the real Settings so agent_id and DB target match the server exactly
(an env mismatch produces "browser shows nothing" with zero errors). The
server's SSE catch-up poll delivers these cross-process writes to any
connected companion within ~2s — which is precisely the path this probes.

Usage:
    uv run python scripts/diag/a2ui_push_demo.py \
        [approval|review|findings|all|gallery|sweep|graph|dag|resolve <surface_id>]

The Phase-2 demos (sweep, graph, dag) source REAL rows from the connected DB
when any exist, so the resolve buttons and node expansion hit live data; they
fall back to synthetic ids (rendering works, round-trips error inline).
"""

from __future__ import annotations

import asyncio
import sys

from nous.a2ui.builders import TEMPLATES
from nous.a2ui.service import SurfaceService
from nous.config import Settings
from nous.storage.database import Database

DEMOS = {
    "approval": (
        "approval_gate",
        {
            "title": "Force-push required on feat/f092-a2ui-companion",
            "summary": (
                "Branch diverged from origin after an interactive rebase. "
                "Completing the merge requires a force-push to the remote branch."
            ),
            "risk": "Irreversible. Overwrites 3 remote commits. No other collaborators on this branch.",
            "recommendation": "force_push",
            "options": [
                {"id": "force_push", "label": "Force-push"},
                {"id": "merge_commit", "label": "Abandon rebase, merge instead"},
                {"id": "abort", "label": "Stop and leave it to me"},
            ],
        },
    ),
    "review": (
        "action_review",
        {
            "title": "Retried premarket compose and sent digest",
            "did": "Re-ran the premarket DAG compose node after it failed validation, then sent the digest.",
            "why": (
                "Fix node classified the failure as validation_failed; dispatcher rule maps "
                "that to retry_as_is. Retry succeeded on attempt 1."
            ),
            "cost": "~40s compute. One email sent.",
            "compensation": {
                "revertible": False,
                "handler": None,
                "note": "Email already delivered - a correction can be sent instead.",
            },
        },
    ),
    "findings": (
        "heartbeat_findings",
        {
            "findings": [
                {
                    "fingerprint": "demo-disk-91-slash",
                    "message": "Disk usage 91% on / - cleanup or expand before it pages.",
                    "urgency": "high",
                    "check": "health",
                },
                {
                    "fingerprint": "demo-broker-emails",
                    "message": "3 unread emails from the broker since 06:00.",
                    "urgency": "normal",
                    "check": "email",
                },
            ],
        },
    ),
}


def _gallery_surface():
    """Inline gallery exercising every renderer adapter (browser-gate aid).

    Built directly through the DSL — deliberately NOT a registered
    push_surface template, so the agent-facing template set stays curated.
    """
    from datetime import timedelta

    from nous.a2ui.dsl import BuiltSurface

    components = [
        {
            "id": "root",
            "component": "Column",
            "children": [
                "md",
                "tiles",
                "kv",
                "div1",
                "tf_short",
                "tf_long",
                "cb",
                "picker",
                "slider",
                "dt",
                "tabs",
                "modal",
                "icon_row",
                "img",
                "submit",
            ],
            "align": "stretch",
        },
        {
            "id": "md",
            "component": "Text",
            "text": (
                "# Adapter gallery\n\nRenders **every** wave-2 adapter: *inputs*, "
                "`code`, a [link](https://a2ui.org) and\n\n- one list item\n- another"
            ),
        },
        {"id": "tiles", "component": "Row", "children": ["t1", "t2"]},
        {
            "id": "t1",
            "component": "StatTile",
            "label": "Facts",
            "value": "12,431",
            "delta": "+38 today",
            "intent": "good",
        },
        {"id": "t2", "component": "StatTile", "label": "Brier", "value": "0.022", "intent": "neutral"},
        {"id": "kv", "component": "KeyValueTable", "rows": {"path": "/kv"}},
        {"id": "div1", "component": "Divider"},
        {"id": "tf_short", "component": "TextField", "label": "Short text", "value": {"path": "/form/name"}},
        {
            "id": "tf_long",
            "component": "TextField",
            "label": "Long text",
            "variant": "longText",
            "value": {"path": "/form/notes"},
        },
        {"id": "cb", "component": "CheckBox", "label": "Subscribe", "value": {"path": "/form/subscribe"}},
        {
            "id": "picker",
            "component": "ChoicePicker",
            "label": "Contact method",
            "variant": "mutuallyExclusive",
            "options": [
                {"label": "Email", "value": "email"},
                {"label": "Phone", "value": "phone"},
                {"label": "SMS", "value": "sms"},
            ],
            "value": {"path": "/form/preference"},
        },
        {
            "id": "slider",
            "component": "Slider",
            "label": "Confidence",
            "min": 0,
            "max": 100,
            "steps": 20,
            "value": {"path": "/form/confidence"},
        },
        {
            "id": "dt",
            "component": "DateTimeInput",
            "enableDate": True,
            "label": "Due date",
            "value": {"path": "/form/due"},
        },
        {
            "id": "tabs",
            "component": "Tabs",
            "tabs": [
                {"title": "First", "child": "tab_a"},
                {"title": "Second", "child": "tab_b"},
            ],
        },
        {"id": "tab_a", "component": "Text", "text": "Tab A content"},
        {"id": "tab_b", "component": "Text", "text": "Tab B content"},
        {"id": "modal", "component": "Modal", "trigger": "modal_t", "content": "modal_c"},
        {"id": "modal_t", "component": "Text", "text": "Open modal"},
        {"id": "modal_c", "component": "Text", "text": "Hello from the modal."},
        {"id": "icon_row", "component": "Row", "children": ["i1", "i2", "i3"]},
        {"id": "i1", "component": "Icon", "name": "check"},
        {"id": "i2", "component": "Icon", "name": "warning"},
        {"id": "i3", "component": "Icon", "name": "mail"},
        {
            "id": "img",
            "component": "Image",
            "url": "/dashboard/v2/favicon.svg",
            "description": "Nous favicon",
            "variant": "icon",
        },
        {
            "id": "submit",
            "component": "Button",
            "child": "submit_l",
            "variant": "primary",
            "action": {
                "event": {
                    "name": "gallery.submit",
                    "context": {
                        "name": {"path": "/form/name"},
                        "subscribe": {"path": "/form/subscribe"},
                        "preference": {"path": "/form/preference"},
                        "confidence": {"path": "/form/confidence"},
                    },
                }
            },
        },
        {"id": "submit_l", "component": "Text", "text": "Submit (will 501 — no handler)"},
    ]
    return BuiltSurface(
        kind="gallery",
        origin="manual",
        title="Adapter gallery",
        catalog_id="https://nous.fatykhov.us/a2ui/v1.0/nous-core/catalog.json",
        priority=0,
        allowed_actions=["gallery.submit"],
        components=components,
        data_model={
            "kv": [
                {"key": "branch", "value": "feat/f092-a2ui-companion"},
                {"key": "migration", "value": "071"},
            ],
            "form": {
                "name": "",
                "notes": "",
                "subscribe": True,
                "preference": ["email"],
                "confidence": 60,
                "due": "",
            },
        },
        expires_in=timedelta(hours=12),
    )


async def _sweep_params(database, settings) -> dict:
    """Real unreviewed decisions when the DB has them; synthetic otherwise."""
    from sqlalchemy import text

    async with database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, description, confidence, stakes, category "
                    "FROM brain.decisions WHERE agent_id = :aid "
                    "AND (outcome IS NULL OR outcome = 'pending') "
                    "ORDER BY created_at DESC LIMIT 3"
                ),
                {"aid": settings.agent_id},
            )
        ).all()
    if rows:
        return {
            "decisions": [
                {
                    "id": str(r[0]),
                    "description": r[1],
                    "confidence": float(r[2] or 0),
                    "stakes": r[3] or "",
                    "category": r[4] or "",
                }
                for r in rows
            ]
        }
    return {
        "decisions": [
            {
                "id": "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8",
                "description": "SYNTHETIC: ship the eval harness behind a flag (resolve will error)",
                "confidence": 0.8,
                "stakes": "medium",
                "category": "architecture",
            }
        ]
    }


async def _graph_params(database, settings) -> dict:
    """A real fact that has at least one graph edge, else any fact, else synthetic."""
    from sqlalchemy import text

    async with database.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT f.id, f.content FROM heart.facts f "
                    "WHERE f.agent_id = :aid AND f.active AND EXISTS ("
                    "  SELECT 1 FROM brain.graph_edges e "
                    "  WHERE e.agent_id = :aid AND (e.source_id = f.id OR e.target_id = f.id)"
                    "  AND e.relation NOT IN ('supersedes', 'superseded_by')"
                    ") ORDER BY f.learned_at DESC LIMIT 1"
                ),
                {"aid": settings.agent_id},
            )
        ).first()
        if row is None:
            row = (
                await session.execute(
                    text(
                        "SELECT id, content FROM heart.facts "
                        "WHERE agent_id = :aid AND active ORDER BY learned_at DESC LIMIT 1"
                    ),
                    {"aid": settings.agent_id},
                )
            ).first()
    if row:
        return {"node_id": str(row[0]), "node_type": "fact", "label": (row[1] or "")[:80]}
    return {
        "node_id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
        "node_type": "fact",
        "label": "SYNTHETIC focus fact (expansion returns nothing)",
    }


DAG_DEMO_PARAMS = {
    "dag_id": "0e9d8c7b-6a5f-4e3d-8c2b-1a0f9e8d7c6b",
    "name": "nightly-audit",
    "status": "running",
    "nodes": [
        {"name": "collect", "status": "completed", "node_type": "subtask"},
        {"name": "classify", "status": "completed", "node_type": "subtask"},
        {"name": "analyze", "status": "failed", "node_type": "subtask"},
        {"name": "cross_check", "status": "running", "node_type": "check"},
        {"name": "report", "status": "pending", "node_type": "callback"},
    ],
    "edges": [
        {"from": "collect", "to": "analyze"},
        {"from": "classify", "to": "analyze"},
        {"from": "collect", "to": "cross_check"},
        {"from": "analyze", "to": "report"},
        {"from": "cross_check", "to": "report"},
    ],
}


async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    settings = Settings()
    database = Database(settings)
    await database.connect()
    service = SurfaceService(database, settings, heart=None)
    try:
        if which == "resolve":
            await service.resolve(sys.argv[2])
            print(f"resolved {sys.argv[2]}")
            return
        if which in ("sweep", "graph", "dag"):
            if which == "sweep":
                built = TEMPLATES["decision_sweep"](await _sweep_params(database, settings))
            elif which == "graph":
                built = TEMPLATES["memory_graph"](await _graph_params(database, settings))
            else:
                built = TEMPLATES["dag_monitor"](DAG_DEMO_PARAMS)
            surface_id = await service.push_built(built, dedup_key=f"demo:{which}", notify=False)
            print(f"pushed {which}: {surface_id}  ->  /companion#/s/{surface_id}")
            return
        if which == "gallery":
            built = _gallery_surface()
            built.validate()
            surface_id = await service.push_built(built, dedup_key="demo:gallery", notify=False)
            print(f"pushed gallery: {surface_id}  ->  /companion#/s/{surface_id}")
            return
        names = list(DEMOS) if which == "all" else [which]
        for name in names:
            template, params = DEMOS[name]
            surface_id = await service.push_built(
                TEMPLATES[template](params),
                dedup_key=f"demo:{name}",
                notify=False,
            )
            print(f"pushed {name}: {surface_id}  ->  /companion#/s/{surface_id}")
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
