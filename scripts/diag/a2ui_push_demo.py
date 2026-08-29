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
