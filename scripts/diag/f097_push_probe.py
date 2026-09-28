"""F097 push probe: is the FCM leg actually configured, and can it mint a token?

Push fails silently by design — a lost push loses only a pointer to a surface
that is already durable, so the send path logs a warning and moves on. That is
right for production and useless for an operator who wants to know whether the
thing works. This is the one place that says so out loud.

It checks, in order:

  1. The two file settings point at readable files.
  2. ``google-services.json`` contains a client for the configured package, and
     its four public values pass the same validation the app re-runs before it
     initialises Firebase (a truncated API key otherwise fails far away, inside
     Firebase Installations, on FCM's own thread, at every process start).
  3. The service account can actually mint an FCM OAuth token. This is the
     blocking call the server wraps in a thread; here it runs inline.
  4. Optionally, with ``--send <installation_id>``, one real test push.

Without ``--send`` nothing leaves the machine except the OAuth token request.

Usage:
  DB_HOST=localhost uv run python -m scripts.diag.f097_push_probe
  DB_HOST=localhost uv run python -m scripts.diag.f097_push_probe --send <id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from nous.a2ui.push import PushService
from nous.config import Settings
from nous.storage.database import Database

OK = "  ok   "
BAD = " FAIL  "
SKIP = " skip  "


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send",
        metavar="INSTALLATION_ID",
        help="also send one real test push to this installation",
    )
    args = parser.parse_args()

    settings = Settings()
    failures = 0

    print("F097 push configuration\n")
    print(f"  package        {settings.a2ui_android_package}")
    print(f"  push enabled   {settings.a2ui_push_enabled}")

    for label, value in (
        ("service account", settings.a2ui_fcm_service_account_file),
        ("google-services", settings.a2ui_fcm_google_services_file),
    ):
        if not (value or "").strip():
            print(f"{BAD} {label}: not configured")
            failures += 1
        elif not Path(value).is_file():
            print(f"{BAD} {label}: no file at {value}")
            failures += 1
        else:
            print(f"{OK} {label}: {value}")

    if failures:
        print("\nPush is not configured. The companion still works; phones are")
        print("simply never notified, and GET /a2ui/push/config says as much.")
        return 1

    database = Database(settings)
    service = PushService(database, settings)

    payload = service.client_config_payload()
    if not payload.get("enabled"):
        print(f"{BAD} client config: {payload.get('reason')}")
        return 1
    print(f"{OK} client config: project {payload['project_id']}, app {payload['application_id']}")
    print(f"         api key {payload['api_key'][:6]}… sender {payload['sender_id']}")

    try:
        token = await service._access_token()
    except Exception as exc:
        print(f"{BAD} OAuth mint: {type(exc).__name__}: {exc}")
        print("\nThe service-account file is readable but did not yield a token.")
        print("Check that it belongs to this Firebase project and that the")
        print("Firebase Cloud Messaging API is enabled for it.")
        return 1
    print(f"{OK} OAuth mint: token acquired ({len(token)} chars)")

    listing = await service.list_installations()
    installs = listing["installations"]
    print(f"\n{len(installs)} registered installation(s):")
    for row in installs:
        state = "has token" if row["has_token"] else f"NO TOKEN ({row['last_error']})"
        notif = "notifications on" if row["notifications_enabled"] else "notifications OFF"
        print(f"  {row['installation_id']}  {row['name'] or '(unnamed)'}  {state}  {notif}")
    if not installs:
        print("  (none — open the Android app once while on the tailnet)")

    if args.send:
        status, result = await service.send_test(args.send)
        marker = OK if status == 200 else BAD
        print(f"\n{marker} test send -> HTTP {status}: {result}")
        if status != 200:
            return 1
    else:
        print(f"\n{SKIP} test send (pass --send <installation_id> to try one)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
