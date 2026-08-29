"""F092 diag: push demo surfaces into a running Nous instance's DB.

Loads the real Settings so agent_id and DB target match the server exactly
(an env mismatch produces "browser shows nothing" with zero errors). The
server's SSE catch-up poll delivers these cross-process writes to any
connected companion within ~2s — which is precisely the path this probes.

Usage:
    uv run python scripts/diag/a2ui_push_demo.py [approval|review|findings|all|resolve <surface_id>]
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
